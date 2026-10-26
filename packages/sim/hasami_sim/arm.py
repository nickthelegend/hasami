"""SO-101 in MuJoCo: scene loading, inverse kinematics and a pick-and-place controller.

Everything here runs real rigid-body physics. Nothing is scripted to "teleport" an object:
a pick only succeeds if the jaws actually hold the cube while the arm lifts it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import mujoco
import numpy as np

ASSETS = Path(__file__).parent / "assets" / "so101"
SCENE = ASSETS / "scene_hasami.xml"

ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
GRIPPER = "gripper"
GRIPPER_OPEN = 1.2
GRIPPER_PRE_GRASP = 0.5  # ~40 mm between the jaw tips: room for a 25 mm cube without a wide sweep
GRIPPER_CLOSED = -0.17
DOWN = np.array([0.0, 0.0, -1.0])


@dataclass
class StepLog:
    """One phase of a pick-and-place, for the dashboard and the tests."""

    phase: str
    target: list[float]
    reached: list[float]
    error_mm: float
    ok: bool
    note: str = ""


@dataclass
class Arm:
    model: mujoco.MjModel
    data: mujoco.MjData
    frames: list[np.ndarray] = field(default_factory=list)
    record_camera: str | None = None
    record_every: int = 20
    record_size: tuple[int, int] = (640, 480)  # (width, height) of recorded frames
    # If set, recorded frames go here (e.g. a video writer) instead of piling up in `frames`.
    frame_sink: Callable[[np.ndarray], None] | None = None
    # If set, called with (arm joint targets, gripper target) in radians as the controller streams them,
    # every `command_every` physics steps and at the end of each move. A real SO-101 mirrors the sim
    # through this (see hasami_robot.RealMirror).
    command_hook: Callable[[np.ndarray, float], None] | None = None
    command_every: int = 17  # ~30 Hz of simulated time at a 2 ms timestep
    _renderer: mujoco.Renderer | None = None
    _gap_table: tuple[np.ndarray, np.ndarray] | None = None
    _steps: int = 0
    clearance: float = 0.005  # gap left on the fixed-finger side; the moving jaw closes it

    @classmethod
    def load(cls, scene: Path = SCENE) -> "Arm":
        model = mujoco.MjModel.from_xml_path(str(scene))
        data = mujoco.MjData(model)
        arm = cls(model, data)
        arm.home()
        return arm

    # ---- ids -------------------------------------------------------------------------
    def _jid(self, name: str) -> int:
        return mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_JOINT, name)

    def _aid(self, name: str) -> int:
        return mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_ACTUATOR, name)

    @property
    def site(self) -> int:
        return mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_SITE, "gripperframe")

    @property
    def arm_qadr(self) -> list[int]:
        return [self.model.jnt_qposadr[self._jid(j)] for j in ARM_JOINTS]

    @property
    def arm_dadr(self) -> list[int]:
        return [self.model.jnt_dofadr[self._jid(j)] for j in ARM_JOINTS]

    # ---- state -----------------------------------------------------------------------
    def home(self) -> None:
        q = {"shoulder_pan": 0.0, "shoulder_lift": -1.2, "elbow_flex": 1.1, "wrist_flex": 1.2, "wrist_roll": 0.0}
        for j, v in q.items():
            self.data.qpos[self.model.jnt_qposadr[self._jid(j)]] = v
            self.data.ctrl[self._aid(j)] = v
        self.data.qpos[self.model.jnt_qposadr[self._jid(GRIPPER)]] = GRIPPER_OPEN
        self.data.ctrl[self._aid(GRIPPER)] = GRIPPER_OPEN
        mujoco.mj_forward(self.model, self.data)
        self.settle(300)

    def gripper_pos(self) -> np.ndarray:
        return self.data.site_xpos[self.site].copy()

    def object_pos(self, name: str) -> np.ndarray:
        bid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, name)
        if bid < 0:
            raise KeyError(f"no object named {name!r} in the scene")
        return self.data.xpos[bid].copy()

    def object_yaw(self, name: str) -> float:
        bid = mujoco.mj_name2id(self.model, mujoco.mjtObj.mjOBJ_BODY, name)
        w, x, y, z = self.data.xquat[bid]
        return float(np.arctan2(2 * (w * z + x * y), 1 - 2 * (y * y + z * z)))

    # ---- simulation ------------------------------------------------------------------
    def step(self, n: int = 1) -> None:
        for _ in range(n):
            mujoco.mj_step(self.model, self.data)
            self._steps += 1
            if self.record_camera and self._steps % self.record_every == 0:
                img = self.render(self.record_camera, *self.record_size)
                if self.frame_sink is not None:
                    self.frame_sink(img)
                else:
                    self.frames.append(img)

    def settle(self, n: int = 200) -> None:
        self.step(n)

    def _command(self, force: bool = False) -> None:
        if self.command_hook is None or not (force or self._steps % self.command_every == 0):
            return
        aids = [self._aid(j) for j in ARM_JOINTS]
        self.command_hook(self.data.ctrl[aids].copy(), float(self.data.ctrl[self._aid(GRIPPER)]))

    def render(self, camera: str, width: int = 640, height: int = 480) -> np.ndarray:
        if self._renderer is None or self._renderer.width != width or self._renderer.height != height:
            self._renderer = mujoco.Renderer(self.model, height=height, width=width)
        self._renderer.update_scene(self.data, camera=camera)
        return self._renderer.render().copy()

    # ---- kinematics ------------------------------------------------------------------
    def ik(self, target: np.ndarray, approach: np.ndarray = DOWN, roll: float | None = None,
           iters: int = 400, tol: float = 1e-4, q0: np.ndarray | None = None) -> tuple[np.ndarray, float]:
        """Damped least squares on the gripper site: position plus the approach axis (site x).

        Works on a scratch copy of the state, so the live simulation is untouched. Starts from the live arm
        angles, or from `q0` if given.
        Returns (arm joint angles, final position error in metres).
        """
        m, d = self.model, mujoco.MjData(self.model)
        d.qpos[:] = self.data.qpos
        qadr, dadr = self.arm_qadr, self.arm_dadr
        if q0 is not None:
            d.qpos[qadr] = q0
        lo = np.array([m.jnt_range[self._jid(j)][0] for j in ARM_JOINTS])
        hi = np.array([m.jnt_range[self._jid(j)][1] for j in ARM_JOINTS])
        if roll is not None:
            d.qpos[qadr[4]] = float(np.clip(roll, lo[4], hi[4]))
        jacp = np.zeros((3, m.nv))
        jacr = np.zeros((3, m.nv))
        err = np.inf
        for _ in range(iters):
            mujoco.mj_kinematics(m, d)
            mujoco.mj_comPos(m, d)
            pos = d.site_xpos[self.site]
            xax = d.site_xmat[self.site].reshape(3, 3)[:, 0]
            e_pos = target - pos
            e_ax = approach - xax
            err = float(np.linalg.norm(e_pos))
            if err < tol and np.linalg.norm(e_ax) < 2e-3:
                break
            mujoco.mj_jacSite(m, d, jacp, jacr, self.site)
            jp = jacp[:, dadr]
            jr = jacr[:, dadr]
            skew = np.array([[0, -xax[2], xax[1]], [xax[2], 0, -xax[0]], [-xax[1], xax[0], 0]])
            j_ax = -skew @ jr
            # The roll joint does not move the site or its x axis; leave it to the caller.
            j = np.vstack([jp, 0.25 * j_ax])
            e = np.concatenate([e_pos, 0.25 * e_ax])
            dq = j.T @ np.linalg.solve(j @ j.T + 1e-4 * np.eye(6), e)
            dq[4] = 0.0
            q = d.qpos[qadr] + np.clip(dq, -0.2, 0.2)
            d.qpos[qadr] = np.clip(q, lo, hi)
        return d.qpos[qadr].copy(), err

    def jaw_axis(self, q: np.ndarray) -> np.ndarray:
        """World direction from the fixed finger to the moving jaw (site z) for arm angles q."""
        d = mujoco.MjData(self.model)
        d.qpos[:] = self.data.qpos
        d.qpos[self.arm_qadr] = q
        mujoco.mj_kinematics(self.model, d)
        return d.site_xmat[self.site].reshape(3, 3)[:, 2].copy()

    def grasp_pose(self, center: np.ndarray, yaw: float, half: float,
                   period: float | None = np.pi / 2) -> tuple[np.ndarray, float]:
        """Site target and wrist roll that put an object of half-width `half` (across the jaws) between them.

        The fixed finger's inner face is the site's z = 0 plane and the moving jaw closes onto it, so the
        object's centre must sit `half` along site z. Wrist roll turns site z about the vertical approach
        axis. `yaw` is a direction the jaw axis may point along; `period` says which other directions are
        equivalent: pi/2 for a square footprint (any face), pi for an elongated one (`yaw` is then across
        its narrow side), None for a round one (any direction: keep the current roll).
        """
        q0, _ = self.ik(center + np.array([0, 0, 0.05]), roll=0.0)
        lo, hi = self.model.jnt_range[self._jid("wrist_roll")]

        def heading(r: float) -> float:
            q = q0.copy()
            q[4] = r
            z = self.jaw_axis(q)
            return float(np.arctan2(z[1], z[0]))

        best = None
        if period is not None:
            a0, a1 = heading(0.0), heading(0.1)
            sign = np.sign(np.angle(np.exp(1j * (a1 - a0)))) or 1.0
            n = int(round(2 * np.pi / period))
            for k in range(-n, n + 1):
                want = yaw + k * period
                r = float(np.angle(np.exp(1j * (want - a0)))) / sign
                for r2 in (r, r + 2 * np.pi * sign, r - 2 * np.pi * sign):
                    if lo <= r2 <= hi and (best is None or abs(r2) < abs(best)):
                        best = r2
        roll = float(best if best is not None else 0.0)
        q = q0.copy()
        q[4] = roll
        z = self.jaw_axis(q)
        z[2] = 0.0
        z /= np.linalg.norm(z) or 1.0
        return center - z * (half + self.clearance), roll

    def jaw_dir(self, site_target: np.ndarray, roll: float) -> np.ndarray:
        """Horizontal unit vector from the fixed finger towards the moving jaw at a grasp pose."""
        q, _ = self.ik(site_target, roll=roll)
        z = self.jaw_axis(q)
        z[2] = 0.0
        return z / (np.linalg.norm(z) or 1.0)

    # ---- gripper state (what the STS3215 reports: its position) --------------------------------------
    def gripper_angle(self) -> float:
        return float(self.data.qpos[self.model.jnt_qposadr[self._jid(GRIPPER)]])

    def jaw_gap(self, angle: float | None = None) -> float:
        """Gap between the jaws (m) at a gripper angle: the moving jaw's collision hull measured along the
        fixed finger's normal (site z), over the fingertip zone. Computed once from the model's kinematics."""
        if self._gap_table is None:
            m = self.model
            d = mujoco.MjData(m)
            d.qpos[:] = self.data.qpos
            jb = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "moving_jaw_so101_v1")
            g = next(i for i in range(m.ngeom) if m.geom_bodyid[i] == jb and m.geom_type[i] == mujoco.mjtGeom.mjGEOM_MESH
                     and m.geom_group[i] == 3)
            mid = m.geom_dataid[g]
            verts = m.mesh_vert[m.mesh_vertadr[mid]:m.mesh_vertadr[mid] + m.mesh_vertnum[mid]]
            qa = m.jnt_qposadr[self._jid(GRIPPER)]
            angles = np.linspace(GRIPPER_CLOSED, GRIPPER_OPEN, 60)
            gaps = []
            for a in angles:
                d.qpos[qa] = a
                mujoco.mj_kinematics(m, d)
                w = d.geom_xpos[g] + verts @ d.geom_xmat[g].reshape(3, 3).T
                sframe = (w - d.site_xpos[self.site]) @ d.site_xmat[self.site].reshape(3, 3)
                zone = (sframe[:, 0] > -0.035) & (sframe[:, 0] < 0.002)
                gaps.append(float(sframe[zone, 2].min()) if zone.any() else np.nan)
            self._gap_table = (angles, np.maximum.accumulate(np.nan_to_num(np.array(gaps), nan=0.0)))
        a = self.gripper_angle() if angle is None else angle
        return float(np.interp(a, *self._gap_table))

    def angle_for_gap(self, gap: float) -> float:
        """Gripper angle at which the jaws are `gap` apart (inverse of jaw_gap)."""
        self.jaw_gap(GRIPPER_OPEN)
        angles, gaps = self._gap_table  # type: ignore[misc]
        return float(np.interp(gap, gaps, angles))

    def grasp_held(self, width: float) -> tuple[bool, float]:
        """Is something of `width` between the jaws? Only the gripper position is used (the real STS3215
        reports it too). With nothing between them the jaws close to their stop (a 2.5 mm reading);
        a held object stops them early (measured readings: 14-25 mm for the 20-30 mm objects here; the
        hinged jaw meets a round object nearer its tip, so a cylinder reads lowest). A reading under
        max(30% of the width, empty + 3 mm) means the jaws closed past the object: an empty grasp."""
        gap = self.jaw_gap()
        empty = self.jaw_gap(GRIPPER_CLOSED)
        return bool(gap >= max(0.3 * width, empty + 0.003)), gap

    def move_to(self, q: np.ndarray, steps: int = 600) -> None:
        """Interpolate the position targets from the current command to q (minimum-jerk)."""
        aids = [self._aid(j) for j in ARM_JOINTS]
        start = self.data.ctrl[aids].copy()
        for i in range(1, steps + 1):
            s = i / steps
            s = 10 * s**3 - 15 * s**4 + 6 * s**5
            self.data.ctrl[aids] = start + s * (q - start)
            self.step(1)
            self._command()
        self._command(force=True)
        self.settle(150)

    def set_gripper(self, value: float, steps: int = 400) -> None:
        aid = self._aid(GRIPPER)
        start = float(self.data.ctrl[aid])
        for i in range(1, steps + 1):
            self.data.ctrl[aid] = start + (value - start) * i / steps
            self.step(1)
            self._command()
        self._command(force=True)
        self.settle(150)

    # ---- skills ----------------------------------------------------------------------
    def go(self, phase: str, target: np.ndarray, roll: float | None, log: list[StepLog], steps: int = 600,
           tol_mm: float = 4.0, segments: int = 1, approach: np.ndarray = DOWN) -> bool:
        """Move the gripper site to target, closing the loop on the measured position.

        The STS3215 position servos are force-limited, so the arm sags under gravity and lands a few
        millimetres off. After each move the measured error is added to the aim point and the IK is
        solved again, up to three times. `approach` is the gripper's pointing direction (straight down for
        the tabletop; a front grasp points it forward).
        """
        target = np.asarray(target, float)
        if segments > 1:  # straight vertical-ish lines: pass through intermediate IK waypoints first
            start = self.gripper_pos()
            for k in range(1, segments):
                q, _ = self.ik(start + (target - start) * k / segments, approach=approach, roll=roll)
                self.move_to(q, max(120, steps // segments))
        aim = target.copy()
        ik_err = 0.0
        for attempt in range(4):
            q, ik_err = self.ik(aim, approach=approach, roll=roll)
            self.move_to(q, steps if attempt == 0 else 300)
            reached = self.gripper_pos()
            err = target - reached
            if np.linalg.norm(err) * 1000 < tol_mm:
                break
            aim = aim + err
        reached = self.gripper_pos()
        err_mm = float(np.linalg.norm(reached - target) * 1000)
        ok = err_mm < 8.0
        log.append(StepLog(phase, [round(float(v), 4) for v in target], [round(float(v), 4) for v in reached],
                           round(err_mm, 1), ok, "" if ok else f"off by {err_mm:.1f} mm (IK residual {ik_err * 1000:.1f} mm)"))
        return ok

    def pick_place(self, pick_xy: tuple[float, float], place_xy: tuple[float, float], yaw: float = 0.0,
                   object_height: float = 0.025, hover: float = 0.065,
                   on_log: Callable[[StepLog], None] | None = None, open_to: float = GRIPPER_OPEN,
                   grip_width: float | None = None, yaw_period: float | None = np.pi / 2,
                   place_surface_z: float | None = None, pick_surface_z: float = 0.0,
                   jaw_offset: float = 0.0, slip: bool = False) -> "PickPlaceLog":
        """Pick the object at pick_xy (centre, from perception) and put it down at place_xy.

        `on_log`, if given, is called with each StepLog as soon as its phase is final.
        `open_to` is how far the jaws open before the grasp and on release. Fully open (the default)
        the moving jaw sweeps about 100 mm sideways as it closes; GRIPPER_PRE_GRASP leaves about 40 mm
        between the jaws, enough for the objects here, so neighbouring objects stay out of its path.
        `grip_width` is the object's width across the jaws (default: object_height, i.e. a cube);
        `yaw_period` as in grasp_pose.
        `place_surface_z` is the measured height of the surface under the place point (table, tray
        floor, the top of another object). With it, the object is lowered until its bottom is 4 mm above
        that surface before the jaws open, so it can be stacked. Without it (legacy), the object is
        released from 35 mm.
        After closing, and again after the lift and after the carry, the gripper's own position says
        whether the object is still held (`grasp_held`); an empty grasp stops the move.
        `jaw_offset` and `slip` are injected disturbances (HASAMI_DISTURB) for testing recovery: the
        grasp point shifted towards the moving jaw so the fixed finger lands on the object, and the jaws
        briefly opening during the carry.
        """
        log: list[StepLog] = _NotifyList(on_log) if on_log else []
        out = PickPlaceLog()
        self._pick_place(log, out, pick_xy, place_xy, yaw, object_height, hover, open_to,
                         grip_width if grip_width is not None else object_height, yaw_period, place_surface_z,
                         pick_surface_z, jaw_offset, slip)
        if isinstance(log, _NotifyList):
            log.flush()
        out.extend(log)
        return out

    def _check_grip(self, log: list[StepLog], out: "PickPlaceLog", phase: str, width: float) -> bool:
        held, gap = self.grasp_held(width)
        out.held, out.jaw_gap_mm = held, round(gap * 1000, 1)
        out.gripper_angle = round(self.gripper_angle(), 4)
        out.checks.append({"after": phase, "held": held, "jaw_gap_mm": out.jaw_gap_mm,
                           "object_width_mm": round(width * 1000, 1)})
        if not held:
            pos = [round(float(v), 4) for v in self.gripper_pos()]
            log.append(StepLog("grasp check", pos, pos, 0.0, False,
                               f"empty grasp after '{phase}': jaws closed to {gap * 1000:.1f} mm, "
                               f"object is {width * 1000:.0f} mm"))
        return held

    def _pick_place(self, log: list[StepLog], out: "PickPlaceLog", pick_xy, place_xy, yaw: float,
                    object_height: float, hover: float, open_to: float, width: float,
                    yaw_period: float | None, place_surface_z: float | None, pick_surface_z: float,
                    jaw_offset: float, slip: bool) -> None:
        half_h = object_height / 2
        center = np.array([pick_xy[0], pick_xy[1], pick_surface_z + half_h])
        site, roll_pick = self.grasp_pose(center, yaw, width / 2, yaw_period)
        if jaw_offset:
            site = site + self.jaw_dir(site + np.array([0, 0, 0.05]), roll_pick) * jaw_offset
        # The fixed fingertip reaches ~6 mm below the site: keep it off the surface.
        grasp_z = max(pick_surface_z + half_h - 0.004, pick_surface_z + 0.0075)
        grasp = np.array([site[0], site[1], grasp_z])
        below = grasp_z - pick_surface_z  # site height above the held object's bottom
        if place_surface_z is None:
            lower_z = hover - 0.03
            carry = hover
        else:
            lower_z = place_surface_z + below + 0.004
            carry = max(hover, place_surface_z + below + 0.015, pick_surface_z + object_height + 0.03)
        above = np.array([site[0], site[1], carry])
        drop_center = np.array([place_xy[0], place_xy[1], (place_surface_z or 0.0) + half_h])
        drop_site, roll_place = self.grasp_pose(drop_center, 0.0, width / 2, yaw_period)
        self.set_gripper(open_to, 200)
        if not self.go("above object", above, roll_pick, log):
            return
        if not self.go("descend", grasp, roll_pick, log, steps=500):
            self.go("back off", above, roll_pick, log)
            return
        self.set_gripper(GRIPPER_CLOSED)
        log.append(StepLog("close jaws", [round(float(v), 4) for v in grasp], [round(float(v), 4) for v in self.gripper_pos()], 0.0, True))
        if not self._check_grip(log, out, "close jaws", width):
            self.set_gripper(open_to, 200)
            self.go("back off", above, roll_pick, log)
            return
        if not self.go("lift", above, roll_pick, log, steps=900):
            self._let_go(log, open_to, roll_pick)
            return
        if slip:  # injected: the jaws let go for a moment just after the lift, as a slipping grip would
            out.injected.append("slip")
            self.set_gripper(GRIPPER_PRE_GRASP, 60)
            self.set_gripper(GRIPPER_CLOSED, 200)
        if not self._check_grip(log, out, "lift", width):
            self.set_gripper(open_to, 200)
            return
        if not self.go("above target", np.array([drop_site[0], drop_site[1], carry]), roll_place, log, steps=900):
            self._let_go(log, open_to, roll_place)
            return
        if not self._check_grip(log, out, "above target", width):
            self.set_gripper(open_to, 200)
            return
        lowered = self.go("lower", np.array([drop_site[0], drop_site[1], lower_z]), roll_place, log, steps=900, segments=3)
        # Release by opening just past the object (8 mm beyond its width or the held reading, if larger):
        # the moving jaw swings on a hinge, and a full opening can knock a stacked object off.
        release = min(open_to, self.angle_for_gap(max((out.jaw_gap_mm or 0) / 1000, width) + 0.008))
        self.set_gripper(release)
        log.append(StepLog("open jaws", [round(float(v), 4) for v in drop_site], [round(float(v), 4) for v in self.gripper_pos()],
                           0.0, lowered, "" if lowered else "released where the lowering stopped"))
        # The object is already released; a sloppy retreat is logged but does not fail the run.
        self.go("retreat", np.array([drop_site[0], drop_site[1], max(carry, lower_z + 0.03) + 0.02]), roll_place, log,
                segments=4)
        log[-1].ok = True
        out.completed = lowered
        out.released = True
        self.settle(400)

    # ---- front grasps (a fridge shelf: in from the front, never from above) ----------------------------
    @property
    def pan_axis(self) -> np.ndarray:
        """(x, y) of the shoulder-pan axis: the arm's plane always contains it, so a straight-in approach
        is radial from here."""
        d = mujoco.MjData(self.model)
        mujoco.mj_kinematics(self.model, d)
        return d.xanchor[self._jid("shoulder_pan")][:2].copy()

    def _solve(self, target: np.ndarray, pitch: float, azimuth: float, roll: float, q0: np.ndarray | None,
               seeds: list[np.ndarray] = ()) -> tuple[np.ndarray, float, float, np.ndarray]:
        """IK for a front pose: the site at `target`, the gripper pointing `pitch` rad below level. Its heading is
        `azimuth` to start with, then whatever the arm's plane allows: the site sits a few millimetres off the
        roll axis, so with the jaws level the plane through the pan axis is turned a little. Tries q0, then each
        seed. Returns (angles, position error m, tilt error deg, approach used)."""
        best = None
        for s in ([q0] if q0 is not None else []) + list(seeds):
            az, q = azimuth, s
            for _ in range(5):
                a = np.array([np.cos(az) * np.cos(pitch), np.sin(az) * np.cos(pitch), -np.sin(pitch)])
                q, err = self.ik(target, approach=a, roll=roll, q0=q, iters=400)
                d = mujoco.MjData(self.model)
                d.qpos[:] = self.data.qpos
                d.qpos[self.arm_qadr] = q
                mujoco.mj_kinematics(self.model, d)
                ax = d.site_xmat[self.site].reshape(3, 3)[:, 0]
                tilt = abs(float(np.degrees(np.arcsin(np.clip(-ax[2], -1.0, 1.0)) - pitch)))
                if err < 5e-4 and tilt < 1.0:
                    break
                az = float(np.arctan2(ax[1], ax[0]))
            if best is None or err * 1000 + tilt < best[1] * 1000 + best[2]:
                best = (q, err, tilt, a)
            if err < 1e-3 and tilt < 1.0:
                break
        return best  # type: ignore[return-value]

    def _level_roll(self, q: np.ndarray, side: np.ndarray) -> tuple[float, float]:
        """Wrist roll that turns the jaw axis (site z) level and along `side` (or against it), at arm angles q:
        (roll, alignment). The roll joint turns site z about the approach axis without moving the site."""
        lo, hi = self.model.jnt_range[self._jid("wrist_roll")]
        best = (0.0, -2.0, -2.0)
        for r in np.arange(lo, hi, 0.01):
            qq = q.copy()
            qq[4] = r
            z = self.jaw_axis(qq)
            for sign in (1.0, -1.0):
                align = float(sign * z @ side)
                score = align - 0.05 * abs(r)  # of the two level rolls, the one nearer zero
                if score > best[2]:
                    best = (float(r), align, score)
        return best[0], best[1]

    def plan_front(self, center: np.ndarray, width: float, height: float, bottom_z: float, place_xy, place_floor_z: float,
                   clear_radius: float, pitches=(15, 20, 25, 30, 35, 40), standoff: float = 0.07,
                   lift: float = 0.012, carry_bottom_z: float = 0.09) -> dict | None:
        """Plan a front fetch: the flattest downward tilt of the gripper (degrees, from level) at which every
        pose of the fetch has an IK solution: in front of the item, at it, lifted, withdrawn until the item is
        within `clear_radius` of the pan axis (out past the cabinet's corners), raised so its bottom is at
        `carry_bottom_z`, carried round the pan axis on an arc, over the place point, and lowered until its
        bottom is 4 mm above `place_floor_z`. The jaw axis is level (the jaws close on the item's sides) and the
        tilt stays fixed through the whole fetch, so the item stays upright in the jaws.
        Returns None if no tilt reaches every pose (the item is out of reach)."""
        axis = self.pan_axis
        c = np.asarray(center, float)
        r_item = c[:2] - axis
        a_h = r_item / np.linalg.norm(r_item)
        # How high on the item the jaws close: above its middle, so it hangs below the jaw axis and swings back
        # upright instead of tipping over in the jaws (gripped at its middle it balances on that axis).
        grasp_h = float(np.clip(0.7 * height, 0.025, height - 0.012))
        half = width / 2
        p_place = np.asarray(place_xy, float)
        b_h = (p_place - axis) / np.linalg.norm(p_place - axis)
        seeds = [np.array(s) for s in ([0.0, -0.4, 0.6, 0.9, 0.0], [0.0, -1.0, 1.3, 0.6, 0.0], [0.0, 0.0, 0.0, 1.2, 0.0],
                                       [0.0, -1.6, 1.5, 0.9, 0.0], [0.0, 0.3, -0.3, 1.0, 0.0])]
        az_item, az_place = float(np.arctan2(a_h[1], a_h[0])), float(np.arctan2(b_h[1], b_h[0]))
        for deg in pitches:
            p = np.radians(deg)
            tip = np.array([c[0], c[1], bottom_z + grasp_h]) + np.r_[a_h, 0.0] * 0.008  # tips just past the centre line
            q_g, err, tilt, a = self._solve(tip, p, az_item, 0.0, None, [np.r_[az_item, s[1:]] for s in seeds])
            if err > 0.0015 or tilt > 2.0:
                continue
            side = np.array([-a_h[1], a_h[0], 0.0])
            roll, align = self._level_roll(q_g, side)
            if align < 0.98:
                continue
            q_g[4] = roll
            z = self.jaw_axis(q_g)
            z[2] = 0.0
            z /= np.linalg.norm(z)
            grasp = tip - z * (half + self.clearance)
            pre = grasp - a * standoff
            lifted = grasp + np.array([0.0, 0.0, lift])
            back = max(0.0, float(np.linalg.norm(r_item)) - clear_radius)
            withdrawn = lifted - np.r_[a_h, 0.0] * back
            raised = withdrawn.copy()
            raised[2] = max(withdrawn[2], carry_bottom_z + grasp_h)
            # over the place point the jaw axis is level again, on the same side of the item (same roll)
            zb = np.array([-b_h[1], b_h[0], 0.0]) * float(np.sign(z @ side) or 1.0)
            place_tip = np.array([p_place[0], p_place[1], 0.0]) + np.r_[b_h, 0.0] * 0.008 - zb * (half + self.clearance)
            above = place_tip.copy()
            above[2] = carry_bottom_z + grasp_h
            lowered = place_tip.copy()
            lowered[2] = place_floor_z + 0.004 + grasp_h
            # the carry swings round the pan axis on an arc (radius and height blended), not along the chord,
            # which passes inside the radius a tilted gripper can reach
            r0, r1 = float(np.linalg.norm(raised[:2] - axis)), float(np.linalg.norm(above[:2] - axis))
            a0 = float(np.arctan2(*(raised[:2] - axis)[::-1]))
            a1 = float(np.arctan2(*(above[:2] - axis)[::-1]))
            arc = []
            for k in range(1, CARRY_STEPS):
                f = k / CARRY_STEPS
                ang, rad = a0 + f * float(np.angle(np.exp(1j * (a1 - a0)))), r0 + f * (r1 - r0)
                arc.append((f"carry {k}", np.array([axis[0] + rad * np.cos(ang), axis[1] + rad * np.sin(ang), above[2]]),
                            az_item + f * float(np.angle(np.exp(1j * (az_place - az_item))))))
            poses = [("pre-grasp", pre, az_item), ("approach", grasp, az_item), ("lift", lifted, az_item),
                     ("withdraw", withdrawn, az_item), ("rise", raised, az_item), *arc, ("above target", above, az_place),
                     ("lower", lowered, az_place)]
            q_prev, ok, sol, appr = q_g, True, {}, {}
            for name, target, az in poses:
                q, err, tilt, ax = self._solve(target, p, az, roll, q_prev, [q_g])
                if err > 0.0015 or tilt > 2.0:
                    ok = False
                    break
                sol[name], appr[name] = q, ax
                q_prev = q
            if not ok:
                continue
            return {"pitch_deg": deg, "roll": roll, "approach": appr, "grasp_h": grasp_h,
                    "poses": {n: t for n, t, _ in poses}, "q": sol, "jaw_axis": z, "a_h": a_h, "b_h": b_h,
                    "release_z": place_floor_z + 0.004 + height + 0.015}
        return None

    def fetch_front(self, plan: dict, width: float, height: float, on_log: Callable[[StepLog], None] | None = None,
                    short: float = 0.0, slip: bool = False) -> "PickPlaceLog":
        """Carry out a plan_front() plan with the physics: open the jaws a little wider than the item, go to the
        pose in front of it, move straight in, close, check the gripper reading, lift, slide straight back
        out, raise, carry over the place point, lower, open just past the item, rise clear of its top.

        The gripper's own position says whether the item is still held after closing, after the withdrawal
        and over the place point; an empty grasp stops the fetch. `short` and `slip` are injected
        disturbances (HASAMI_DISTURB): the grasp stops `short` metres before the item along the approach
        (a depth error: the jaws close in front of it), and the jaws let go for a moment after the lift."""
        log: list[StepLog] = _NotifyList(on_log) if on_log else []
        out = PickPlaceLog()
        try:
            self._fetch_front(log, out, plan, width, height, short, slip)
        finally:
            if isinstance(log, _NotifyList):
                log.flush()
        out.extend(log)
        return out

    def _fetch_front(self, log, out: "PickPlaceLog", plan: dict, width: float, height: float, short: float,
                     slip: bool) -> None:
        poses, roll, ax = plan["poses"], plan["roll"], plan["approach"]  # ax: each pose's own pointing direction

        def go(name: str, target=None, **kw) -> bool:
            return self.go(kw.pop("phase", name), poses[name] if target is None else target, roll, log,
                           approach=ax[name], **kw)

        open_to = min(GRIPPER_OPEN, self.angle_for_gap(width + 0.018))
        grasp = poses["approach"] - ax["approach"] * short
        if short:
            out.injected.append("short")
        # Turn to the item with the jaws open while still folded, then unfold to the pose in front of it.
        self.set_gripper(open_to, 200)
        q_pre = plan["q"]["pre-grasp"]
        turn = self.data.ctrl[[self._aid(j) for j in ARM_JOINTS]].copy()
        turn[0], turn[4] = q_pre[0], roll
        self.move_to(turn, 500)
        self.move_to(q_pre, 700)
        if not go("pre-grasp"):
            return
        if not go("approach", grasp, steps=500, segments=4):
            go("pre-grasp", phase="back off", segments=3)
            return
        self.set_gripper(GRIPPER_CLOSED)
        log.append(StepLog("close jaws", [round(float(v), 4) for v in grasp], [round(float(v), 4) for v in self.gripper_pos()],
                           0.0, True))
        if not self._check_grip(log, out, "close jaws", width):
            self.set_gripper(open_to, 200)
            go("pre-grasp", poses["pre-grasp"] - ax["pre-grasp"] * short, phase="back off", segments=3)
            return
        if not go("lift", steps=400):
            self._let_go(log, open_to, roll, ax["lift"])
            return
        if slip:
            out.injected.append("slip")
            self.set_gripper(min(open_to, self.angle_for_gap(width + 0.01)), 60)
            self.set_gripper(GRIPPER_CLOSED, 200)
        if not self._check_grip(log, out, "lift", width):
            self.set_gripper(open_to, 200)
            go("pre-grasp", phase="back off", segments=3)
            return
        for name, steps, seg in (("withdraw", 700, 3), ("rise", 500, 1)):
            if not go(name, steps=steps, segments=seg):
                self._let_go(log, open_to, roll, ax[name])
                return
        if not self._check_grip(log, out, "withdraw", width):
            self.set_gripper(open_to, 200)
            return
        for name in [n for n in poses if n.startswith("carry ")]:
            if not go(name, steps=300):
                self._let_go(log, open_to, roll, ax[name])
                return
        if not go("above target", steps=300):
            self._let_go(log, open_to, roll, ax["above target"])
            return
        if not self._check_grip(log, out, "above target", width):
            self.set_gripper(open_to, 200)
            return
        lowered = go("lower", steps=700, segments=3)
        release = min(open_to, self.angle_for_gap(max((out.jaw_gap_mm or 0) / 1000, width) + 0.010))
        self.set_gripper(release)
        log.append(StepLog("open jaws", [round(float(v), 4) for v in poses["lower"]],
                           [round(float(v), 4) for v in self.gripper_pos()], 0.0, lowered,
                           "" if lowered else "released where the lowering stopped"))
        top = poses["lower"].copy()
        top[2] = plan["release_z"]
        go("lower", top, phase="retreat", segments=3)
        log[-1].ok = True  # the item is already released; a sloppy retreat is logged but does not fail the fetch
        out.completed = lowered
        out.released = True
        self.settle(400)

    def _let_go(self, log: list[StepLog], open_to: float, roll: float | None, approach: np.ndarray = DOWN) -> None:
        """A move failed while holding the object: put it down where it is rather than carry it away."""
        self.set_gripper(open_to, 300)
        here = self.gripper_pos()
        log.append(StepLog("open jaws", [round(float(v), 4) for v in here], [round(float(v), 4) for v in here], 0.0, False,
                           "released after a failed move"))
        self.go("back off", here + np.array([0, 0, 0.03]), roll, log, approach=approach)
        self.settle(300)


CARRY_STEPS = 5  # the carry's arc in this many legs


class PickPlaceLog(list):
    """The phases of one pick-and-place (a list of StepLog) plus what the gripper reported.

    held: the last grasp check (None if the jaws never closed); jaw_gap_mm / gripper_angle: its reading;
    checks: every grasp check; completed: the object was lowered to the target and released; released: the
    jaws opened over the target (possibly higher than planned); injected: disturbances."""

    def __init__(self, *args):
        super().__init__(*args)
        self.held: bool | None = None
        self.jaw_gap_mm: float | None = None
        self.gripper_angle: float | None = None
        self.checks: list[dict] = []
        self.completed = False
        self.released = False
        self.injected: list[str] = []


class _NotifyList(list):
    """A list that reports each appended StepLog. The previous entry is reported when the next one
    arrives (and the last one on flush), because pick_place may still adjust an entry after appending."""

    def __init__(self, callback: Callable[[StepLog], None]):
        super().__init__()
        self._callback = callback

    def append(self, item: StepLog) -> None:
        if self:
            self._callback(self[-1])
        super().append(item)

    def flush(self) -> None:
        if self:
            self._callback(self[-1])


def in_tray(arm: Arm, name: str, tray: str = "tray", half: float = 0.044) -> bool:
    """Ground truth from the physics state: is the object's centre inside the tray walls and resting low?"""
    p = arm.object_pos(name)
    t = arm.object_pos(tray)
    return bool(abs(p[0] - t[0]) < half and abs(p[1] - t[1]) < half and p[2] < 0.04)


def in_container(arm: Arm, name: str, container) -> bool:
    """Ground truth from the physics state: is the object's centre inside a container's walls and resting
    low (below the wall tops)? `container` is a hasami_sim.scene.ContainerSpec."""
    p = arm.object_pos(name)
    t = arm.object_pos(container.name)
    lim = container.inner_half
    return bool(abs(p[0] - t[0]) < lim and abs(p[1] - t[1]) < lim and p[2] < container.floor_h + 0.045)
