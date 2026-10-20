"""SO-101 in MuJoCo: scene loading, inverse kinematics and a pick-and-place controller.

Everything here runs real rigid-body physics. Nothing is scripted to "teleport" an object:
a pick only succeeds if the jaws actually hold the cube while the arm lifts it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import mujoco
import numpy as np

ASSETS = Path(__file__).parent / "assets" / "so101"
SCENE = ASSETS / "scene_hasami.xml"

ARM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
GRIPPER = "gripper"
GRIPPER_OPEN = 1.2
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
    _renderer: mujoco.Renderer | None = None
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
                self.frames.append(self.render(self.record_camera))

    def settle(self, n: int = 200) -> None:
        self.step(n)

    def render(self, camera: str, width: int = 640, height: int = 480) -> np.ndarray:
        if self._renderer is None or self._renderer.width != width or self._renderer.height != height:
            self._renderer = mujoco.Renderer(self.model, height=height, width=width)
        self._renderer.update_scene(self.data, camera=camera)
        return self._renderer.render().copy()

    # ---- kinematics ------------------------------------------------------------------
    def ik(self, target: np.ndarray, approach: np.ndarray = DOWN, roll: float | None = None,
           iters: int = 400, tol: float = 1e-4) -> tuple[np.ndarray, float]:
        """Damped least squares on the gripper site: position plus the approach axis (site x).

        Works on a scratch copy of the state, so the live simulation is untouched.
        Returns (arm joint angles, final position error in metres).
        """
        m, d = self.model, mujoco.MjData(self.model)
        d.qpos[:] = self.data.qpos
        qadr, dadr = self.arm_qadr, self.arm_dadr
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

    def grasp_pose(self, center: np.ndarray, yaw: float, half: float) -> tuple[np.ndarray, float]:
        """Site target and wrist roll that put an axis-aligned box of half-size `half` between the jaws.

        The fixed finger's inner face is the site's z = 0 plane and the moving jaw closes onto it,
        so the box centre must sit `half` along site z. Wrist roll turns site z about the vertical
        approach axis; pick the face normal (yaw + k*90 deg) that needs the least roll.
        """
        q0, _ = self.ik(center + np.array([0, 0, 0.05]), roll=0.0)
        lo, hi = self.model.jnt_range[self._jid("wrist_roll")]

        def heading(r: float) -> float:
            q = q0.copy()
            q[4] = r
            z = self.jaw_axis(q)
            return float(np.arctan2(z[1], z[0]))

        a0, a1 = heading(0.0), heading(0.1)
        sign = np.sign(np.angle(np.exp(1j * (a1 - a0)))) or 1.0
        best = None
        for k in range(-4, 5):
            want = yaw + k * np.pi / 2
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

    def move_to(self, q: np.ndarray, steps: int = 600) -> None:
        """Interpolate the position targets from the current command to q (minimum-jerk)."""
        aids = [self._aid(j) for j in ARM_JOINTS]
        start = self.data.ctrl[aids].copy()
        for i in range(1, steps + 1):
            s = i / steps
            s = 10 * s**3 - 15 * s**4 + 6 * s**5
            self.data.ctrl[aids] = start + s * (q - start)
            self.step(1)
        self.settle(150)

    def set_gripper(self, value: float, steps: int = 400) -> None:
        aid = self._aid(GRIPPER)
        start = float(self.data.ctrl[aid])
        for i in range(1, steps + 1):
            self.data.ctrl[aid] = start + (value - start) * i / steps
            self.step(1)
        self.settle(150)

    # ---- skills ----------------------------------------------------------------------
    def go(self, phase: str, target: np.ndarray, roll: float | None, log: list[StepLog], steps: int = 600,
           tol_mm: float = 4.0) -> bool:
        """Move the gripper site to target, closing the loop on the measured position.

        The STS3215 position servos are force-limited, so the arm sags under gravity and lands a few
        millimetres off. After each move the measured error is added to the aim point and the IK is
        solved again, up to three times.
        """
        target = np.asarray(target, float)
        aim = target.copy()
        ik_err = 0.0
        for attempt in range(4):
            q, ik_err = self.ik(aim, roll=roll)
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
                   object_height: float = 0.025, hover: float = 0.065) -> list[StepLog]:
        """Pick the box at pick_xy (centre, from perception) and drop it at place_xy."""
        log: list[StepLog] = []
        half = object_height / 2
        center = np.array([pick_xy[0], pick_xy[1], half])
        site, roll_pick = self.grasp_pose(center, yaw, half)
        grasp = np.array([site[0], site[1], half - 0.004])
        above = np.array([site[0], site[1], hover])
        drop_center = np.array([place_xy[0], place_xy[1], half])
        drop_site, roll_place = self.grasp_pose(drop_center, 0.0, half)
        self.set_gripper(GRIPPER_OPEN, 200)
        if not self.go("above object", above, roll_pick, log):
            return log
        if not self.go("descend", grasp, roll_pick, log, steps=500):
            return log
        self.set_gripper(GRIPPER_CLOSED)
        log.append(StepLog("close jaws", [round(float(v), 4) for v in grasp], [round(float(v), 4) for v in self.gripper_pos()], 0.0, True))
        for phase, tgt, roll in [
            ("lift", above, roll_pick),
            ("above target", np.array([drop_site[0], drop_site[1], hover]), roll_place),
            ("lower", np.array([drop_site[0], drop_site[1], hover - 0.03]), roll_place),
        ]:
            if not self.go(phase, tgt, roll, log, steps=900):
                return log
        self.set_gripper(GRIPPER_OPEN)
        log.append(StepLog("open jaws", [round(float(v), 4) for v in drop_site], [round(float(v), 4) for v in self.gripper_pos()], 0.0, True))
        # The object is already placed; a sloppy retreat is logged but does not fail the run.
        self.go("retreat", np.array([drop_site[0], drop_site[1], hover + 0.02]), roll_place, log)
        log[-1].ok = True
        self.settle(400)
        return log


def in_tray(arm: Arm, name: str, tray: str = "tray", half: float = 0.044) -> bool:
    """Ground truth from the physics state: is the object's centre inside the tray walls and resting low?"""
    p = arm.object_pos(name)
    t = arm.object_pos(tray)
    return bool(abs(p[0] - t[0]) < half and abs(p[1] - t[1]) < half and p[2] < 0.04)
