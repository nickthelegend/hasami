"""A Hasami session: one simulated SO-101, its overhead camera, SAM, and the plan runner.

Perception sees RGB + depth only. The simulator's ground truth (segmentation, object poses) is used
for two things and nothing else: scoring perception (IoU, localisation error) and checking whether
a step really worked (is the cube in the tray, according to physics).
"""

from __future__ import annotations

import base64
import io
import json
import os
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import mujoco
import numpy as np
from PIL import Image

from hasami_perception import LocalSam2, MetaSam3, SetupRequired, Target, locate, mask_iou
from hasami_perception.colour import blob_near, colour_mask, colour_word, mean_colour, rgb_to_hsv, similar_mask
from hasami_planner import Plan, PlanStep, plan_from_clicks
from hasami_sim.arm import ARM_JOINTS, GRIPPER, GRIPPER_OPEN, GRIPPER_PRE_GRASP, Arm, StepLog, in_container
from hasami_sim.camera import Frame, capture
from hasami_sim.scene import SceneSpec, build, classic

# Scene objects: MuJoCo body name -> label used by the planner, the API and the dashboard.
OBJECTS: dict[str, str] = {"red_cube": "red cube", "green_cube": "green cube", "blue_cube": "blue cube",
                           "tray": "tray"}
CUBES = ["red_cube", "green_cube", "blue_cube"]
CONTAINERS = {"tray"}
CUBE_SIZE = 0.025

# Randomisation bounds for reset(): within reach, clear of the tray, cubes apart.
REACH_X = (0.18, 0.27)
REACH_Y = (-0.10, 0.10)
MAX_YAW = np.radians(40.0)
MIN_SEPARATION = 0.055  # m between cube centres (25 mm cubes plus room for the jaws)

# Where the arm waits between steps: folded back so the overhead camera sees the whole workspace.
PARK_Q = np.array([0.0, -1.5, 0.8, -0.6, 0.0])
# The ready pose pick_place was validated from (Arm.home); the arm passes through it to and from PARK_Q
# so the joint-space moves never sweep the jaws through the workspace.
READY_Q = np.array([0.0, -1.2, 1.1, 1.2, 0.0])

EventFn = Callable[[str, dict], None]

SIMULATION = "MuJoCo simulation, official SO-101 model"
DISTURB_ENV = "HASAMI_DISTURB"


@dataclass
class Disturb:
    """Injected disturbances for testing recovery (HASAMI_DISTURB=offset_mm:6,slip:0.2). Always labelled
    in events and run records.

    offset_mm: on each step's first attempt, the grasp point is shifted this far towards the moving jaw,
      so the fixed finger lands on the object (a localisation error of that size).
    slip: probability that the jaws let go for a moment just after the lift (a slipping grip)."""

    offset_mm: float = 0.0
    slip: float = 0.0

    @classmethod
    def parse(cls, text: str | None) -> "Disturb | None":
        if not text or not text.strip():
            return None
        out = cls()
        for part in text.split(","):
            key, _, val = part.partition(":")
            key = key.strip()
            if key not in ("offset_mm", "slip") or not val.strip():
                raise ValueError(f"{DISTURB_ENV}: expected offset_mm:<mm>,slip:<0-1>, got {part!r}")
            setattr(out, key, float(val))
        if not 0 <= out.slip <= 1:
            raise ValueError(f"{DISTURB_ENV}: slip is a probability in [0, 1]")
        return out if out.active else None

    @property
    def active(self) -> bool:
        return bool(self.offset_mm or self.slip)

    def to_dict(self) -> dict:
        return {"offset_mm": self.offset_mm, "slip": self.slip, "label": "injected disturbance (test)"}

ROOT = Path(__file__).resolve().parents[3]


def runs_dir() -> Path:
    """deployments/runs at the repo root, or HASAMI_RUNS_DIR."""
    return Path(os.environ.get("HASAMI_RUNS_DIR") or ROOT / "deployments" / "runs")


def png_bytes(img: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def mask_png_b64(mask: np.ndarray) -> str:
    return base64.b64encode(png_bytes(mask.astype(np.uint8) * 255)).decode()


def _r(v, n: int = 4):
    if isinstance(v, (list, tuple, np.ndarray)):
        return [_r(x, n) for x in v]
    return round(float(v), n)


@dataclass
class Segmentation:
    """One prompt -> one mask -> (maybe) a 3D target, plus how it scores against ground truth."""

    mask: np.ndarray
    segmenter: str
    latency_ms: float
    point: list[float] | None = None
    text: str | None = None
    target: Target | None = None
    target_error: str | None = None
    truth_label: str | None = None  # what the simulator says is under the prompt (scoring only)
    iou_vs_truth: float | None = None

    def to_dict(self, with_mask: bool = True) -> dict:
        out: dict = {
            "segmenter": self.segmenter,
            "latency_ms": round(self.latency_ms, 1),
            "point": self.point,
            "text": self.text,
            "mask_pixels": int(self.mask.sum()),
            "target": _target_dict(self.target),
            "truth_label": self.truth_label,
            "iou_vs_truth": None if self.iou_vs_truth is None else round(self.iou_vs_truth, 4),
        }
        if self.target_error:
            out["target_error"] = self.target_error
        if with_mask:
            out["mask_png"] = mask_png_b64(self.mask)
        return out


def _target_dict(t: Target | None) -> dict | None:
    if t is None:
        return None
    return {"center": _r(t.center), "top_z": _r(t.top_z), "yaw": _r(t.yaw), "pixels": t.pixels,
            "centroid_px": _r(t.centroid_px, 1)}


@dataclass
class Session:
    arm: Arm = field(default_factory=Arm.load)
    seed: int | None = None
    snapshot_on_phase: bool = False  # refresh the cached camera PNGs at every phase (for the server)
    lock: threading.RLock = field(default_factory=threading.RLock)
    _sam: LocalSam2 | None = None
    _sam_lock: threading.Lock = field(default_factory=threading.Lock)
    _snapshots: dict[str, bytes] = field(default_factory=dict)
    initial_poses: dict = field(default_factory=dict)
    last_run: dict | None = None
    # Real SO-101 in mirror mode (HASAMI_SO101_PORT): None unless a follower actually connected.
    real: object | None = None
    real_setup: str | None = None
    connect_real: bool = True
    scene: SceneSpec = field(default_factory=classic)
    disturb: Disturb | None = None  # None: read HASAMI_DISTURB
    max_retries: int = 2  # re-segment and retry a step that fails its checks, up to this many times
    _rng: np.random.Generator = field(default_factory=np.random.default_rng)

    def __post_init__(self) -> None:
        if self.disturb is None:
            self.disturb = Disturb.parse(os.environ.get(DISTURB_ENV))
        if self.connect_real:
            self._connect_real_arm()
        self.reset(self.seed)

    def _connect_real_arm(self) -> None:
        """With HASAMI_SO101_PORT set, connect the LeRobot follower and mirror every joint target the
        simulated arm is commanded to it. Without the port (or LeRobot, or the device) the session stays
        sim-only and `real_setup` says what is missing. Untested on hardware."""
        from hasami_robot import SO101, RealMirror

        if not os.environ.get("HASAMI_SO101_PORT"):
            return
        try:
            robot = SO101()
        except SetupRequired as e:
            self.real_setup = str(e)
            return
        except Exception as e:  # noqa: BLE001 - the arm is there but would not connect; say why
            self.real_setup = f"SO-101 did not connect: {type(e).__name__}: {e}"
            return
        self.real = robot
        self.arm.command_hook = RealMirror(robot)

    @property
    def arm_kind(self) -> str:
        """'so101' only while a real follower is connected and mirroring; otherwise 'sim'."""
        return "so101" if self.real is not None else "sim"

    # ---- models ------------------------------------------------------------------------
    @property
    def sam(self) -> LocalSam2:
        """SAM 2.1, loaded on first use and then kept for the life of the process."""
        with self._sam_lock:
            if self._sam is None:
                self._sam = LocalSam2()
            return self._sam

    @property
    def sam_loaded(self) -> bool:
        return self._sam is not None

    def segmenter(self, name: str):
        if name == "local":
            return self.sam
        if name == "meta":
            return MetaSam3()  # raises SetupRequired without MODEL_API_KEY
        raise ValueError(f"unknown segmenter {name!r}; use 'local' or 'meta'")

    # ---- scene -------------------------------------------------------------------------
    def _jid(self, name: str) -> int:
        return mujoco.mj_name2id(self.arm.model, mujoco.mjtObj.mjOBJ_JOINT, name)

    def _set_arm(self, q: np.ndarray, gripper: float = GRIPPER_OPEN) -> None:
        m, d = self.arm.model, self.arm.data
        for j, v in zip(ARM_JOINTS, q):
            d.qpos[m.jnt_qposadr[self._jid(j)]] = v
            d.ctrl[self.arm._aid(j)] = v
        d.qpos[m.jnt_qposadr[self._jid(GRIPPER)]] = gripper
        d.ctrl[self.arm._aid(GRIPPER)] = gripper

    @property
    def objects(self) -> dict[str, str]:
        """Body name -> label for every object and container in the current scene."""
        return self.scene.labels

    @property
    def movables(self) -> list[str]:
        return [o.name for o in self.scene.objects]

    def reset(self, seed: int | None = None) -> dict:
        """The classic scene: three cubes at random non-overlapping poses within reach (yaw within
        +-40 deg), tray fixed, arm parked. Retries a layout if any cube would be hidden from the overhead
        camera. (Procedural scenes: load_scene.)"""
        with self.lock:
            if self.scene.name != "classic":
                self._swap_arm(Arm.load())
                self.scene = classic()
            seed = int(seed) if seed is not None else secrets.randbelow(2**31)
            rng = np.random.default_rng(seed)
            m, d = self.arm.model, self.arm.data
            cubes = self.movables
            for _ in range(100):
                pts = _sample_layout(rng, len(cubes))
                yaws = rng.uniform(-MAX_YAW, MAX_YAW, size=len(cubes))
                mujoco.mj_resetData(m, d)
                self._set_arm(PARK_Q)
                for name, p, yaw in zip(cubes, pts, yaws):
                    j = self._jid(name)
                    a, v = m.jnt_qposadr[j], m.jnt_dofadr[j]
                    d.qpos[a:a + 3] = [p[0], p[1], CUBE_SIZE / 2]
                    d.qpos[a + 3:a + 7] = [np.cos(yaw / 2), 0.0, 0.0, np.sin(yaw / 2)]
                    d.qvel[v:v + 6] = 0.0
                mujoco.mj_forward(m, d)
                self.arm.settle(300)
                if self._all_visible():
                    break
            return self._after_reset(seed)

    def load_scene(self, spec: SceneSpec) -> dict:
        """Build a procedural scene (hasami_sim.scene) and park the arm in it. Raises ValueError if an
        object is hidden from the overhead camera."""
        with self.lock:
            model = build(spec)
            self._swap_arm(Arm(model, mujoco.MjData(model)))
            self.scene = spec
            m, d = self.arm.model, self.arm.data
            mujoco.mj_resetData(m, d)
            self._set_arm(PARK_Q)
            mujoco.mj_forward(m, d)
            self.arm.settle(300)
            if not self._all_visible(min_pixels=120):
                raise ValueError(f"an object in scene {spec.name!r} (seed {spec.seed}) is hidden from the overhead camera")
            return self._after_reset(spec.seed if spec.seed is not None else 0)

    def _swap_arm(self, arm: Arm) -> None:
        arm.command_hook = self.arm.command_hook if hasattr(self, "arm") and self.arm is not None else None
        self.arm = arm

    def _after_reset(self, seed: int) -> dict:
        self.seed = seed
        self._rng = np.random.default_rng([seed, 7])
        for o in self.scene.objects:  # keep the spec's poses in step with the physics
            p = self.arm.object_pos(o.name)
            o.x, o.y, o.yaw = float(p[0]), float(p[1]), float(self.arm.object_yaw(o.name))
        self.initial_poses = self.poses()
        self._snapshots.clear()
        return {"seed": seed, "scene": self.scene.name, "objects": self.initial_poses}

    def _all_visible(self, min_pixels: int = 300) -> bool:
        f = capture(self.arm.model, self.arm.data)
        return all(self._truth_mask(f, n).sum() >= min_pixels for n in self.movables)

    def poses(self) -> dict:
        """Ground-truth poses from the physics state (reporting and scoring only)."""
        out = {}
        for name, label in self.objects.items():
            p = self.arm.object_pos(name)
            out[name] = {"label": label, "pos": _r(p), "yaw": _r(self.arm.object_yaw(name))}
        return out

    def scene_objects(self) -> list[dict]:
        return [{"name": n, "label": label} for n, label in self.objects.items()]

    def park(self) -> None:
        """Move (with the servos, not a teleport) to the parked pose so the camera has a clear view."""
        self.arm.move_to(READY_Q, 600)
        self.arm.move_to(PARK_Q, 600)

    def unpark(self) -> None:
        self.arm.move_to(READY_Q, 600)

    # ---- camera ------------------------------------------------------------------------
    def observe(self, camera: str = "overhead") -> Frame:
        with self.lock:
            return capture(self.arm.model, self.arm.data, camera)

    def _render_png(self, camera: str) -> bytes:
        # A fresh renderer per call: a GL context cached by one thread hangs when another thread
        # (the server's request and run threads) renders with it.
        r = mujoco.Renderer(self.arm.model, height=480, width=640)
        try:
            r.update_scene(self.arm.data, camera=camera)
            png = png_bytes(r.render())
        finally:
            r.close()
        self._snapshots[camera] = png
        return png

    def frame_png(self, camera: str = "overhead") -> tuple[bytes, bool]:
        """PNG of the current frame. While a run holds the scene, returns the latest snapshot the run
        rendered instead of waiting. Returns (png, live)."""
        if camera not in ("overhead", "demo"):
            raise KeyError(f"no camera named {camera!r}; use 'overhead' or 'demo'")
        if self.lock.acquire(blocking=False):
            try:
                return self._render_png(camera), True
            finally:
                self.lock.release()
        snap = self._snapshots.get(camera)
        if snap is None:
            with self.lock:
                return self._render_png(camera), True
        return snap, False

    # ---- ground truth (scoring only) ---------------------------------------------------
    def _body_of_geom(self, gid: int) -> str | None:
        if gid < 0:
            return None
        return mujoco.mj_id2name(self.arm.model, mujoco.mjtObj.mjOBJ_BODY, int(self.arm.model.geom_bodyid[gid]))

    def _truth_mask(self, frame: Frame, body: str) -> np.ndarray:
        bid = mujoco.mj_name2id(self.arm.model, mujoco.mjtObj.mjOBJ_BODY, body)
        geoms = np.nonzero(self.arm.model.geom_bodyid == bid)[0]
        return np.isin(frame.seg_geom, geoms)

    def truth_at(self, frame: Frame, point: tuple[float, float]) -> str | None:
        u, v = int(round(point[0])), int(round(point[1]))
        h, w = frame.seg_geom.shape
        if not (0 <= u < w and 0 <= v < h):
            return None
        body = self._body_of_geom(int(frame.seg_geom[v, u]))
        if body in self.objects:
            return body
        return "table" if body == "world" else ("arm" if body else None)

    def _score(self, seg: Segmentation, frame: Frame) -> None:
        if seg.point is not None:
            body = self.truth_at(frame, seg.point)
        else:  # text prompt: the known object the mask overlaps most
            ious = {n: mask_iou(seg.mask, self._truth_mask(frame, n)) for n in self.objects}
            body = max(ious, key=ious.get) if ious and max(ious.values()) > 0 else None
        if body in self.objects:
            seg.truth_label = self.objects[body]
            seg.iou_vs_truth = mask_iou(seg.mask, self._truth_mask(frame, body))
        else:
            seg.truth_label = body

    # ---- perception --------------------------------------------------------------------
    def segment(self, point: tuple[float, float] | None = None, text: str | None = None,
                segmenter: str = "local", frame: Frame | None = None) -> Segmentation:
        with self.lock:
            if segmenter == "local" and point is None:
                raise ValueError("the local segmenter (SAM 2.1) needs a point [u, v]; text prompts need "
                                 "segmenter 'meta' (SAM 3.1)")
            if segmenter == "meta" and not text:
                raise ValueError("the meta segmenter (SAM 3.1) needs a text prompt, e.g. 'red cube'")
            seg_model = self.segmenter(segmenter)
            frame = frame or self.observe()
            t0 = time.perf_counter()
            if segmenter == "local":
                mask = seg_model.segment(frame.rgb, point=(float(point[0]), float(point[1])))  # type: ignore[index]
            else:
                mask = seg_model.segment(frame.rgb, text=text)
            seg = Segmentation(mask=mask, segmenter=seg_model.name, latency_ms=(time.perf_counter() - t0) * 1000,
                               point=None if point is None else [float(point[0]), float(point[1])], text=text)
            try:
                seg.target = locate(frame, mask)
            except ValueError as e:
                seg.target_error = str(e)
            self._score(seg, frame)
            return seg

    def _surface_z(self, frame: Frame, xy, radius: float = 0.008) -> float:
        """Height of whatever surface is under (x, y), from depth: table, tray floor, top of an object."""
        u, v = frame.world_to_pixel(np.array([xy[0], xy[1], 0.0]))
        h, w = frame.depth.shape
        u0, u1 = int(max(0, u - 14)), int(min(w, u + 15))
        v0, v1 = int(max(0, v - 14)), int(min(h, v + 15))
        if u0 >= u1 or v0 >= v1:
            return 0.0
        vs, us = np.mgrid[v0:v1, u0:u1]
        us, vs = us.ravel(), vs.ravel()
        fx, fy, cx, cy = frame.intrinsics()
        z = frame.depth[vs, us].astype(np.float64)
        pts = frame.cam_pos + np.stack([(us - cx) / fx * z, -(vs - cy) / fy * z, -z], axis=1) @ frame.cam_mat.T
        near = np.hypot(pts[:, 0] - xy[0], pts[:, 1] - xy[1]) < radius
        return float(max(0.0, np.median(pts[near, 2]))) if near.any() else 0.0

    def _place_target(self, frame: Frame, seg: Segmentation, held_radius: float) -> tuple[np.ndarray, str, float]:
        """Where the picked object's centre should go, and the height of the surface it will rest on.

        - Clicked a container (a footprint wider than the object plus margins): the clicked spot, kept far
          enough inside the container's footprint that the object clears its walls; surface = its floor.
        - Clicked another object: on top of it (stacking); surface = its measured top.
        - Clicked bare table: the clicked spot on the table.
        - Text prompt (no click): the container's centre, or the object's top.
        """
        click = None if seg.point is None else frame.pixel_to_world(*seg.point)[:2]
        if seg.target is not None:
            vs, us = np.nonzero(seg.mask)
            fx, fy, cx, cy = frame.intrinsics()
            z = frame.depth[vs, us].astype(np.float64)
            pts = frame.cam_pos + np.stack([(us - cx) / fx * z, -(vs - cy) / fy * z, -z], axis=1) @ frame.cam_mat.T
            pts = pts[pts[:, 2] > 0.004]
            lo, hi = pts[:, :2].min(axis=0), pts[:, :2].max(axis=0)
            # Keep the object's centre far enough inside that the fingers (the fixed one is 13 mm thick,
            # beside the object) clear the walls.
            margin = held_radius + 0.02
            if np.all(hi - lo > 2 * margin + 0.01):
                base = click if click is not None else np.array(seg.target.center[:2])
                xy = np.clip(base, lo + margin, hi - margin)
                return xy, "inside container", self._surface_z(frame, xy)
            return np.array(seg.target.center[:2]), "on top of object", float(seg.target.top_z)
        if click is None:
            raise ValueError(f"could not locate the place target: {seg.target_error}")
        return click, "on table", self._surface_z(frame, click)

    # ---- running a plan ----------------------------------------------------------------
    def run(self, steps: Plan | list[dict] | list[PlanStep], on_event: EventFn | None = None,
            segmenter: str = "local", run_id: str | None = None) -> dict:
        """Execute a plan step by step. Emits plan, then per step segment, target, phase, verify (and retry
        with another round of those), result; then done (or error).

        Returns the run record (also what record() writes)."""
        emit = on_event or (lambda kind, data: None)
        plan = steps if isinstance(steps, Plan) else plan_from_clicks(
            [s.model_dump() if isinstance(s, PlanStep) else s for s in steps])
        run_id = run_id or new_run_id()
        rec: dict = {
            "run_id": run_id, "started_at": _now(), "segmenter": segmenter, "seed": self.seed,
            "simulation": SIMULATION, "scene": self.scene.name,
            "plan": plan.model_dump(), "initial_poses": None, "steps": [], "ok": False,
        }
        if self.disturb is not None:
            rec["disturb"] = self.disturb.to_dict()
        t_run = time.perf_counter()
        with self.lock:
            rec["initial_poses"] = self.poses()
            try:
                seg_model = self.segmenter(segmenter)
                rec["segmenter_name"] = seg_model.name
                emit("plan", {"run_id": run_id, "source": plan.source, "model": plan.model,
                              "instruction": plan.instruction, "steps": [s.model_dump() for s in plan.steps],
                              "simulation": SIMULATION,
                              "disturb": None if self.disturb is None else self.disturb.to_dict()})
                for i, step in enumerate(plan.steps):
                    rec["steps"].append(self._run_step(i, step, segmenter, emit))
                self.park()
                if self.snapshot_on_phase:
                    self._render_png("overhead")
                    self._render_png("demo")
            except Exception as e:
                rec["error"] = f"{type(e).__name__}: {e}"
                rec["finished_at"] = _now()
                rec["timings"] = {"total_ms": round((time.perf_counter() - t_run) * 1000, 1)}
                rec["final_poses"] = self.poses()
                self.last_run = rec
                emit("error", {"run_id": run_id, "message": str(e), "type": type(e).__name__})
                raise
        placed = sum(1 for s in rec["steps"] if s["result"]["ok"])
        rec.update(ok=placed == len(plan.steps), placed=placed, total=len(plan.steps), finished_at=_now(),
                   final_poses=self.poses())
        rec["timings"] = {"total_ms": round((time.perf_counter() - t_run) * 1000, 1)}
        ious = [a[r]["iou_vs_truth"] for s in rec["steps"] for a in s["attempts"] for r in ("pick", "place")
                if a.get(r) and a[r].get("iou_vs_truth") is not None]
        rec["iou"] = {"values": ious, "mean": round(float(np.mean(ious)), 4) if ious else None,
                      "min": round(float(np.min(ious)), 4) if ious else None}
        checks = [a["verify"] for s in rec["steps"] for a in s["attempts"] if a.get("verify")]
        rec["verify"] = {"checks": len(checks), "agree": sum(1 for c in checks if c["agree"]),
                         "retries": sum(len(s["attempts"]) - 1 for s in rec["steps"])}
        self.last_run = rec
        emit("done", {"run_id": run_id, "ok": rec["ok"], "placed": placed, "total": len(plan.steps),
                      "total_ms": rec["timings"]["total_ms"], "retries": rec["verify"]["retries"]})
        return rec

    def _run_step(self, i: int, step: PlanStep, segmenter: str, emit: EventFn) -> dict:
        """One plan step: attempt it, check it from the gripper reading and the camera, and if a check
        fails re-find the object in the new image and try again (up to max_retries more times)."""
        out: dict = {"step": i, "pick_label": step.pick, "place_label": step.place, "attempts": []}
        pick_hint = [float(step.pick_point[0]), float(step.pick_point[1])]
        place_hint = [float(step.place_point[0]), float(step.place_point[1])]
        att: dict = {}
        for attempt in range(1, self.max_retries + 2):
            att = self._attempt(i, attempt, step, pick_hint, place_hint, segmenter, emit)
            out["attempts"].append(att["record"])
            if att["verified"] or attempt > self.max_retries:
                break
            hint, how = self._reground(att, step, pick_hint)
            emit("retry", {"step": i, "attempt": attempt + 1, "reason": att["reason"], "pick_point": hint,
                           "how": how})
            if hint is None:
                break
            pick_hint = hint
        last = out["attempts"][-1]
        for key in ("pick", "place", "place_xy", "place_how", "surface_z", "phases", "timings"):
            if key in last:
                out[key] = last[key]
        out.setdefault("phases", [])
        result = self._step_result(i, att, len(out["attempts"]))
        emit("result", result)
        out["result"] = result
        return out

    def _attempt(self, i: int, attempt: int, step: PlanStep, pick_hint: list[float], place_hint: list[float],
                 segmenter: str, emit: EventFn) -> dict:
        t: dict[str, float] = {}
        t0 = time.perf_counter()
        self.park()
        frame = self.observe()
        t["observe_ms"] = (time.perf_counter() - t0) * 1000
        rec: dict = {"attempt": attempt, "pick_point": _r(pick_hint, 1), "place_point": _r(place_hint, 1)}
        res: dict = {"frame": frame, "record": rec, "verified": False, "reason": None, "pick": None, "place": None,
                     "obj": None, "pp": None, "physics_ok": False}

        use_text = segmenter == "meta" and attempt == 1
        pick = self.segment(point=None if use_text else pick_hint, text=step.pick if use_text else None,
                            segmenter=segmenter if use_text else "local", frame=frame)
        t["segment_pick_ms"] = pick.latency_ms
        res["pick"] = pick
        emit("segment", {"step": i, "attempt": attempt, "role": "pick", **pick.to_dict()})
        rec["pick"] = pick.to_dict(with_mask=False)
        if pick.target is None:
            rec["timings"] = _round_t(t)
            return self._fail_attempt(res, "segment", f"pick target not found: {pick.target_error}")
        emit("target", {"step": i, "attempt": attempt, "role": "pick", "target": _target_dict(pick.target)})
        res["ref_colour"] = mean_colour(frame.rgb, pick.mask & self._raised(frame, pick.mask))

        use_text = segmenter == "meta"
        place = self.segment(point=None if use_text else place_hint, text=step.place if use_text else None,
                             segmenter=segmenter, frame=frame)
        t["segment_place_ms"] = place.latency_ms
        res["place"] = place
        emit("segment", {"step": i, "attempt": attempt, "role": "place", **place.to_dict()})
        rec["place"] = place.to_dict(with_mask=False)
        jaw_yaw, period, width = pick.target.grasp()
        held_radius = 0.5 * float(max(pick.target.extent or [CUBE_SIZE]))
        try:
            place_xy, how, surface = self._place_target(frame, place, held_radius)
        except ValueError as e:
            rec["timings"] = _round_t(t)
            return self._fail_attempt(res, "place", str(e))
        rec.update(place_xy=_r(place_xy), place_how=how, surface_z=_r(surface),
                   grasp={"jaw_yaw": _r(jaw_yaw), "period": None if period is None else _r(period),
                          "width_mm": round(width * 1000, 1)})
        emit("target", {"step": i, "attempt": attempt, "role": "place", "target": _target_dict(place.target),
                        "place_xy": _r(place_xy), "how": how, "surface_z": _r(surface)})

        # Scoring only: which object will the arm actually pick? The one nearest the located centre.
        obj = min(self.movables, key=lambda n: np.linalg.norm(self.arm.object_pos(n)[:2] - np.array(pick.target.center[:2])))
        before = {n: self.arm.object_pos(n) for n in self.movables}
        res.update(obj=obj, before=before, place_xy=place_xy, how=how, surface=surface)
        rec["localisation_error_mm"] = round(float(np.linalg.norm(before[obj][:2] - np.array(pick.target.center[:2])) * 1000), 2)

        injected: dict = {}
        offset = 0.0
        if self.disturb is not None and self.disturb.offset_mm and attempt == 1:
            offset = self.disturb.offset_mm / 1000
            injected["offset_mm"] = self.disturb.offset_mm
        slip = bool(self.disturb is not None and self.disturb.slip and self._rng.random() < self.disturb.slip)
        if slip:
            injected["slip"] = True
        rec["injected"] = injected

        def on_log(log: StepLog) -> None:
            emit("phase", {"step": i, "attempt": attempt, **asdict(log)})
            if self.snapshot_on_phase:
                self._render_png("overhead")
                self._render_png("demo")

        t1 = time.perf_counter()
        self.unpark()
        height = float(np.clip(pick.target.top_z, 0.012, 0.045))
        pp = self.arm.pick_place(pick.target.center[:2], place_xy, yaw=jaw_yaw, object_height=height, on_log=on_log,
                                 open_to=GRIPPER_PRE_GRASP, grip_width=width, yaw_period=period,
                                 place_surface_z=surface, jaw_offset=offset, slip=slip)
        t["pick_place_ms"] = (time.perf_counter() - t1) * 1000
        res["pp"] = pp
        rec["phases"] = [asdict(lg) for lg in pp]
        rec["grasp_checks"] = pp.checks

        # Check the step the way a real robot could: the gripper's own position, then the camera.
        t2 = time.perf_counter()
        self.park()
        after = self.observe()
        res["after"] = after
        v = self._verify_image(after, pick, place_xy, surface, height, res["ref_colour"])
        t["verify_ms"] = (time.perf_counter() - t2) * 1000
        v.update(grasp_held=pp.held, jaw_gap_mm=pp.jaw_gap_mm, completed=pp.completed)
        v["ok"] = bool(pp.completed and pp.held is not False and v["pick_clear"] and v["place_raised"])
        physics_ok = self._physics_ok(obj, place_xy, how, surface, height)
        v["physics_ok"] = physics_ok  # ground truth, for scoring the check (never used to decide)
        v["agree"] = v["ok"] == physics_ok
        res["physics_ok"] = physics_ok
        rec["verify"] = v
        rec["timings"] = _round_t(t)
        emit("verify", {"step": i, "attempt": attempt, **v, "injected": injected})
        if not physics_ok:
            rec["failure"] = "grasp" if pp.held is not True else "place"
        res["verified"] = v["ok"]
        if not v["ok"]:
            if pp.held is False:
                res["reason"] = next((c for c in pp.checks if not c["held"]), {}).get("after", "")
                res["reason"] = f"empty grasp (gripper reading after '{res['reason']}')"
            elif not pp.completed:
                failed = next((lg for lg in pp if not lg.ok), None)
                res["reason"] = f"motion stopped at '{failed.phase}': {failed.note}" if failed else "motion incomplete"
            elif not v["pick_clear"]:
                res["reason"] = "the object is still at the pick spot in the image"
            else:
                res["reason"] = "no raised object of the right colour at the place spot in the image"
        return res

    def _fail_attempt(self, res: dict, failure: str, reason: str) -> dict:
        res["record"]["failure"] = failure
        res["record"]["note"] = reason
        res["reason"] = reason
        return res

    def _raised(self, frame: Frame, mask: np.ndarray, min_height: float = 0.005) -> np.ndarray:
        """Mask pixels that sit above the table (drops shadow and table pixels a mask leaks onto)."""
        vs, us = np.nonzero(mask)
        out = np.zeros_like(mask, dtype=bool)
        if len(us) == 0:
            return out
        fx, fy, cx, cy = frame.intrinsics()
        z = frame.depth[vs, us].astype(np.float64)
        pz = (frame.cam_pos + np.stack([(us - cx) / fx * z, -(vs - cy) / fy * z, -z], axis=1) @ frame.cam_mat.T)[:, 2]
        keep = pz > min_height
        out[vs[keep], us[keep]] = True
        return out

    def _verify_image(self, frame: Frame, pick: Segmentation, place_xy: np.ndarray, surface: float,
                      height: float, ref_colour: np.ndarray) -> dict:
        """Did the step work, judged from the camera alone (RGB + depth, SAM 2.1 point prompts)?

        pick_clear: no raised mask left where the object was (SAM at the original pick pixel).
        place_raised: a raised mask of the object's colour at the place spot, its top where the object's
        top should be (SAM at the place spot's pixel)."""
        assert pick.target is not None
        sam = self.sam
        out: dict = {}
        still = False
        m1 = sam.segment(frame.rgb, point=tuple(pick.target.centroid_px))
        try:
            t1 = locate(frame, m1)
            still = (np.linalg.norm(np.array(t1.center[:2]) - np.array(pick.target.center[:2])) < 0.010
                     and abs(t1.top_z - pick.target.top_z) < 0.006)
        except ValueError:
            pass
        out["pick_clear"] = not still
        exp_top = surface + height
        u, v = frame.world_to_pixel(np.array([place_xy[0], place_xy[1], exp_top]))
        w, h = frame.size
        raised = False
        if 0 <= u < w and 0 <= v < h:
            m2 = sam.segment(frame.rgb, point=(u, v))
            try:
                t2 = locate(frame, m2)
                near = np.linalg.norm(np.array(t2.center[:2]) - place_xy) < 0.02
                level = abs(t2.top_z - exp_top) < 0.008
                same = _hue_close(mean_colour(frame.rgb, m2 & self._raised(frame, m2)), ref_colour)
                raised = bool(near and level and same)
                out["place_seen"] = {"center": _r(t2.center), "top_z": _r(t2.top_z), "near": bool(near),
                                     "level": bool(level), "same_colour": bool(same)}
            except ValueError as e:
                out["place_seen"] = {"error": str(e)}
        out["place_raised"] = raised
        out["place_px"] = _r([u, v], 1)
        return out

    def _container_at(self, xy) -> object | None:
        for c in self.scene.containers:
            if abs(xy[0] - c.x) < c.half and abs(xy[1] - c.y) < c.half:
                return c
        return None

    def _physics_ok(self, obj: str, place_xy, how: str, surface: float, height: float) -> bool:
        """Ground truth (scoring only): did the object end up where the step meant it to go?"""
        final = self.arm.object_pos(obj)
        cont = self._container_at(place_xy)
        if cont is not None:
            return in_container(self.arm, obj, cont)
        err = float(np.linalg.norm(final[:2] - np.asarray(place_xy)))
        if how == "on top of object":
            return err < 0.012 and abs(final[2] - (surface + height / 2)) < 0.006
        return err < 0.015 and final[2] < height / 2 + 0.006

    def _reground(self, att: dict, step: PlanStep, hint: list[float]) -> tuple[list[float] | None, str]:
        """Where to prompt SAM for the retry, from the newest image only: the blob of the picked object's
        colour nearest the previous pick pixel; else the blob of the colour the label names; else the
        same pixel again."""
        frame: Frame = att.get("after") or self.observe()
        ref = att.get("ref_colour")
        if ref is not None:
            try:
                c = blob_near(similar_mask(frame.rgb, ref), tuple(hint), max_dist_px=140)
                if c is not None:
                    return [c[0], c[1]], "same-colour blob nearest the previous pick pixel"
            except ValueError:
                pass
        word = colour_word(step.pick)
        if word:
            c = blob_near(colour_mask(frame.rgb, word), tuple(hint), max_dist_px=140)
            if c is not None:
                return [c[0], c[1]], f"'{word}' blob nearest the previous pick pixel"
        if att.get("pick") is not None and att["pick"].target is not None:
            return list(hint), "same pixel"
        return None, "nothing of the object's colour near the pick pixel"

    def _step_result(self, i: int, att: dict, attempts: int) -> dict:
        rec = att["record"]
        obj = att.get("obj")
        if obj is None:
            return {"step": i, "object": None, "object_name": None, "in_tray": False, "error_mm": None, "ok": False,
                    "note": att["reason"], "attempts": attempts, "verified": False,
                    "failure": rec.get("failure", "segment")}
        final = self.arm.object_pos(obj)
        cont = self._container_at(att["place_xy"])
        inside = in_container(self.arm, obj, cont) if cont is not None else any(
            in_container(self.arm, obj, c) for c in self.scene.containers)
        err_mm = float(np.linalg.norm(final[:2] - att["place_xy"]) * 1000)
        disturbed = [self.objects[n] for n in self.movables
                     if n != obj and np.linalg.norm(self.arm.object_pos(n)[:2] - att["before"][n][:2]) > 0.005]
        pp = att["pp"]
        ok = bool(pp is not None and pp.completed and att["physics_ok"])
        result = {"step": i, "object": self.objects[obj], "object_name": obj, "in_tray": inside,
                  "error_mm": round(err_mm, 1), "ok": ok, "localisation_error_mm": rec.get("localisation_error_mm"),
                  "disturbed": disturbed, "final_pos": _r(final), "attempts": attempts,
                  "verified": bool(att["verified"]), "failure": None if ok else rec.get("failure", "place")}
        if not ok:
            result["note"] = att["reason"] or "object did not end up at the target"
        return result

    # ---- records -----------------------------------------------------------------------
    def record(self, run: dict, out_dir: Path | str | None = None) -> Path:
        """Write the run record to deployments/runs/<run_id>.json (run_id is a UTC timestamp)."""
        out = Path(out_dir) if out_dir else runs_dir()
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{run['run_id']}.json"
        path.write_text(json.dumps(run, indent=2, default=_json_default) + "\n")
        return path


def _sample_layout(rng: np.random.Generator, n: int) -> list[np.ndarray]:
    """n cube centres inside the reach box, at least MIN_SEPARATION apart (restarts a stuck layout)."""
    while True:
        pts: list[np.ndarray] = []
        for _ in range(200):
            p = np.array([rng.uniform(*REACH_X), rng.uniform(*REACH_Y)])
            if all(np.linalg.norm(p - q) >= MIN_SEPARATION for q in pts):
                pts.append(p)
                if len(pts) == n:
                    return pts


def _round_t(t: dict) -> dict:
    return {k: round(v, 1) for k, v in t.items()}


def _hue_close(a: np.ndarray, b: np.ndarray, tol: float = 25.0) -> bool:
    """Same colour by hue (both saturated), or both unsaturated."""
    ha, sa, _ = rgb_to_hsv(np.asarray(a, dtype=np.float64).reshape(1, 3) / 255.0)
    hb, sb, _ = rgb_to_hsv(np.asarray(b, dtype=np.float64).reshape(1, 3) / 255.0)
    if sa[0] < 0.3 or sb[0] < 0.3:
        return bool(sa[0] < 0.3 and sb[0] < 0.3)
    return bool(abs(((ha[0] - hb[0]) + 180) % 360 - 180) <= tol)


def _json_default(o):
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


_last_id = {"v": ""}
_id_lock = threading.Lock()


def new_run_id() -> str:
    """A UTC timestamp, unique within the process (e.g. 20261021T031502123Z)."""
    with _id_lock:
        while True:
            now = datetime.now(timezone.utc)
            rid = now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"
            if rid != _last_id["v"]:
                _last_id["v"] = rid
                return rid
            time.sleep(0.001)


def list_runs(out_dir: Path | str | None = None) -> list[dict]:
    """Summaries of recorded runs, newest first."""
    d = Path(out_dir) if out_dir else runs_dir()
    out = []
    for p in sorted(d.glob("*.json"), reverse=True):
        try:
            r = json.loads(p.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"run_id": r.get("run_id", p.stem), "started_at": r.get("started_at"), "ok": r.get("ok"),
                    "placed": r.get("placed"), "total": r.get("total"), "segmenter": r.get("segmenter_name"),
                    "source": (r.get("plan") or {}).get("source"), "instruction": (r.get("plan") or {}).get("instruction"),
                    "iou_mean": (r.get("iou") or {}).get("mean"), "error": r.get("error"),
                    "video": r.get("video"), "file": p.name})
    return out
