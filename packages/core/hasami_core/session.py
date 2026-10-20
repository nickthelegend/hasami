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

from hasami_perception import LocalSam2, MetaSam3, Target, locate, mask_iou
from hasami_planner import Plan, PlanStep, plan_from_clicks
from hasami_sim.arm import ARM_JOINTS, GRIPPER, GRIPPER_OPEN, GRIPPER_PRE_GRASP, Arm, StepLog, in_tray
from hasami_sim.camera import Frame, capture

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

    def __post_init__(self) -> None:
        self.reset(self.seed)

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

    def reset(self, seed: int | None = None) -> dict:
        """Put the cubes at random non-overlapping poses within reach (yaw within +-40 deg), tray fixed,
        arm parked. Retries a layout if any cube would be hidden from the overhead camera."""
        with self.lock:
            seed = int(seed) if seed is not None else secrets.randbelow(2**31)
            rng = np.random.default_rng(seed)
            m, d = self.arm.model, self.arm.data
            for _ in range(100):
                pts = _sample_layout(rng, len(CUBES))
                yaws = rng.uniform(-MAX_YAW, MAX_YAW, size=len(CUBES))
                mujoco.mj_resetData(m, d)
                self._set_arm(PARK_Q)
                for name, p, yaw in zip(CUBES, pts, yaws):
                    j = self._jid(name)
                    a, v = m.jnt_qposadr[j], m.jnt_dofadr[j]
                    d.qpos[a:a + 3] = [p[0], p[1], CUBE_SIZE / 2]
                    d.qpos[a + 3:a + 7] = [np.cos(yaw / 2), 0.0, 0.0, np.sin(yaw / 2)]
                    d.qvel[v:v + 6] = 0.0
                mujoco.mj_forward(m, d)
                self.arm.settle(300)
                if self._all_visible():
                    break
            self.seed = seed
            self.initial_poses = self.poses()
            self._snapshots.clear()
            return {"seed": seed, "objects": self.initial_poses}

    def _all_visible(self, min_pixels: int = 300) -> bool:
        f = capture(self.arm.model, self.arm.data)
        return all(self._truth_mask(f, n).sum() >= min_pixels for n in CUBES)

    def poses(self) -> dict:
        """Ground-truth poses from the physics state (reporting and scoring only)."""
        out = {}
        for name in OBJECTS:
            p = self.arm.object_pos(name)
            out[name] = {"label": OBJECTS[name], "pos": _r(p), "yaw": _r(self.arm.object_yaw(name))}
        return out

    def scene_objects(self) -> list[dict]:
        return [{"name": n, "label": label} for n, label in OBJECTS.items()]

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
        if body in OBJECTS:
            return body
        return "table" if body == "world" else ("arm" if body else None)

    def _score(self, seg: Segmentation, frame: Frame) -> None:
        if seg.point is not None:
            body = self.truth_at(frame, seg.point)
        else:  # text prompt: the known object the mask overlaps most
            ious = {n: mask_iou(seg.mask, self._truth_mask(frame, n)) for n in OBJECTS}
            body = max(ious, key=ious.get) if ious and max(ious.values()) > 0 else None
        if body in OBJECTS:
            seg.truth_label = OBJECTS[body]
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

    def _place_xy(self, frame: Frame, seg: Segmentation) -> tuple[np.ndarray, str]:
        """Where the picked object's centre should go, from the place prompt.

        - Clicked a container (a footprint larger than the object): the clicked spot, kept far enough
          inside the container's footprint that the object clears its walls.
        - Clicked bare table: the clicked spot on the table.
        - Text prompt (no click): the container's centre.
        """
        click = None if seg.point is None else frame.pixel_to_world(*seg.point)[:2]
        if seg.target is not None:
            vs, us = np.nonzero(seg.mask)
            fx, fy, cx, cy = frame.intrinsics()
            z = frame.depth[vs, us].astype(np.float64)
            pts = frame.cam_pos + np.stack([(us - cx) / fx * z, -(vs - cy) / fy * z, -z], axis=1) @ frame.cam_mat.T
            pts = pts[pts[:, 2] > 0.005]
            lo, hi = pts[:, :2].min(axis=0), pts[:, :2].max(axis=0)
            margin = CUBE_SIZE / 2 + 0.01
            if np.all(hi - lo > CUBE_SIZE + 2 * margin):
                base = click if click is not None else np.array(seg.target.center[:2])
                return np.clip(base, lo + margin, hi - margin), "inside container"
            return np.array(seg.target.center[:2]), "on top of object"
        if click is None:
            raise ValueError(f"could not locate the place target: {seg.target_error}")
        return click, "on table"

    # ---- running a plan ----------------------------------------------------------------
    def run(self, steps: Plan | list[dict] | list[PlanStep], on_event: EventFn | None = None,
            segmenter: str = "local", run_id: str | None = None) -> dict:
        """Execute a plan step by step. Emits plan, segment, target, phase, result, done (or error).

        Returns the run record (also what record() writes)."""
        emit = on_event or (lambda kind, data: None)
        plan = steps if isinstance(steps, Plan) else plan_from_clicks(
            [s.model_dump() if isinstance(s, PlanStep) else s for s in steps])
        run_id = run_id or new_run_id()
        rec: dict = {
            "run_id": run_id, "started_at": _now(), "segmenter": segmenter, "seed": self.seed,
            "plan": plan.model_dump(), "initial_poses": None, "steps": [], "ok": False,
        }
        t_run = time.perf_counter()
        with self.lock:
            rec["initial_poses"] = self.poses()
            try:
                seg_model = self.segmenter(segmenter)
                rec["segmenter_name"] = seg_model.name
                emit("plan", {"run_id": run_id, "source": plan.source, "model": plan.model,
                              "instruction": plan.instruction, "steps": [s.model_dump() for s in plan.steps]})
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
        ious = [s[r]["iou_vs_truth"] for s in rec["steps"] for r in ("pick", "place")
                if s.get(r) and s[r].get("iou_vs_truth") is not None]
        rec["iou"] = {"values": ious, "mean": round(float(np.mean(ious)), 4) if ious else None,
                      "min": round(float(np.min(ious)), 4) if ious else None}
        self.last_run = rec
        emit("done", {"run_id": run_id, "ok": rec["ok"], "placed": placed, "total": len(plan.steps),
                      "total_ms": rec["timings"]["total_ms"]})
        return rec

    def _run_step(self, i: int, step: PlanStep, segmenter: str, emit: EventFn) -> dict:
        t = {}
        t0 = time.perf_counter()
        self.park()
        frame = self.observe()
        t["observe_ms"] = (time.perf_counter() - t0) * 1000
        out: dict = {"step": i, "pick_label": step.pick, "place_label": step.place}

        use_text = segmenter == "meta"
        pick = self.segment(point=None if use_text else step.pick_point, text=step.pick if use_text else None,
                            segmenter=segmenter, frame=frame)
        t["segment_pick_ms"] = pick.latency_ms
        emit("segment", {"step": i, "role": "pick", **pick.to_dict()})
        out["pick"] = pick.to_dict(with_mask=False)
        if pick.target is None:
            return self._fail(out, i, emit, None, f"pick target not found: {pick.target_error}", t)
        emit("target", {"step": i, "role": "pick", "target": _target_dict(pick.target)})

        place = self.segment(point=None if use_text else step.place_point, text=step.place if use_text else None,
                             segmenter=segmenter, frame=frame)
        t["segment_place_ms"] = place.latency_ms
        emit("segment", {"step": i, "role": "place", **place.to_dict()})
        out["place"] = place.to_dict(with_mask=False)
        try:
            place_xy, how = self._place_xy(frame, place)
        except ValueError as e:
            return self._fail(out, i, emit, None, str(e), t)
        out["place_xy"], out["place_how"] = _r(place_xy), how
        emit("target", {"step": i, "role": "place", "target": _target_dict(place.target), "place_xy": _r(place_xy),
                        "how": how})

        # Which object will the arm actually pick? The cube nearest the located centre (physics, for scoring).
        obj = min(CUBES, key=lambda n: np.linalg.norm(self.arm.object_pos(n)[:2] - np.array(pick.target.center[:2])))
        before = {n: self.arm.object_pos(n) for n in CUBES}
        loc_err_mm = float(np.linalg.norm(before[obj][:2] - np.array(pick.target.center[:2])) * 1000)

        def on_log(log: StepLog) -> None:
            emit("phase", {"step": i, **asdict(log)})
            if self.snapshot_on_phase:
                self._render_png("overhead")
                self._render_png("demo")

        t1 = time.perf_counter()
        self.unpark()
        height = float(np.clip(pick.target.top_z, 0.015, 0.04))
        logs = self.arm.pick_place(pick.target.center[:2], place_xy, yaw=pick.target.yaw, object_height=height,
                                   on_log=on_log, open_to=GRIPPER_PRE_GRASP)
        t["pick_place_ms"] = (time.perf_counter() - t1) * 1000
        out["phases"] = [asdict(lg) for lg in logs]

        final = self.arm.object_pos(obj)
        intray = in_tray(self.arm, obj)
        err_mm = float(np.linalg.norm(final[:2] - place_xy) * 1000)
        disturbed = [OBJECTS[n] for n in CUBES if n != obj and np.linalg.norm(self.arm.object_pos(n)[:2] - before[n][:2]) > 0.005]
        motion_ok = all(lg.ok for lg in logs) and len(logs) >= 8
        to_tray = place.truth_label == "tray" or (use_text and "tray" in step.place.lower())
        ok = motion_ok and (intray if to_tray else err_mm < 15.0)
        result = {"step": i, "object": OBJECTS[obj], "in_tray": intray, "error_mm": round(err_mm, 1),
                  "ok": bool(ok), "localisation_error_mm": round(loc_err_mm, 2), "disturbed": disturbed,
                  "final_pos": _r(final)}
        if not motion_ok:
            failed = next((lg for lg in logs if not lg.ok), None)
            result["note"] = f"motion stopped at '{failed.phase}': {failed.note}" if failed else "motion incomplete"
        elif not ok:
            result["note"] = "object did not end up at the target"
        emit("result", result)
        out["result"] = result
        out["timings"] = {k: round(v, 1) for k, v in t.items()}
        return out

    def _fail(self, out: dict, i: int, emit: EventFn, obj: str | None, note: str, t: dict) -> dict:
        result = {"step": i, "object": obj, "in_tray": False, "error_mm": None, "ok": False, "note": note}
        emit("result", result)
        out["result"] = result
        out["phases"] = []
        out["timings"] = {k: round(v, 1) for k, v in t.items()}
        return out

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
