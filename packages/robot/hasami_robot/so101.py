"""The real SO-101 follower arm, driven through LeRobot.

The simulator works in joint radians (MuJoCo model `so101_new_calib.xml`). LeRobot's SO-101 follower
takes calibrated positions: degrees for the five arm joints (with `use_degrees=True`) and 0-100 for the
gripper. The "new calib" MJCF puts each joint's zero at the middle of its calibrated range, which is
what LeRobot's calibration does too, so the mapping is degrees(q) * sign + offset with sign = +1 and
offset = 0 by default.

Calibration offsets themselves live in LeRobot's calibration file, written by
`lerobot-calibrate --robot.type=so101_follower --robot.port=... --robot.id=...` to
`~/.cache/huggingface/lerobot/calibration/robots/so101_follower/<id>.json` (range_min, range_max and
homing_offset per motor). LeRobot applies them when it converts degrees to raw STS3215 steps; the
`signs`/`offsets_deg` here are only for a follower whose assembly differs from the model (check each
joint's direction once at low torque before running a plan).

Nothing here is exercised against hardware on this machine: without HASAMI_SO101_PORT, or without
LeRobot installed, or if the port does not exist, `SO101` raises SetupRequired.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

from hasami_perception import SetupRequired

SETUP_MESSAGE = ("Setup required: HASAMI_SO101_PORT (SO-101 follower arm on USB) and "
                 "`pip install lerobot[feetech]`")

SIM_JOINTS = ["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"]
LEROBOT_KEYS = {j: f"{j}.pos" for j in SIM_JOINTS} | {"gripper": "gripper.pos"}

# Joint ranges of so101_new_calib.xml in radians (tests check these against the MJCF).
SIM_RANGES_RAD = {
    "shoulder_pan": (-1.91986, 1.91986),
    "shoulder_lift": (-1.74533, 1.74533),
    "elbow_flex": (-1.69, 1.69),
    "wrist_flex": (-1.65806, 1.65806),
    "wrist_roll": (-2.74385, 2.84121),
    "gripper": (-0.17453, 1.74533),
}


def _gripper_pct(g_rad: float) -> float:
    lo, hi = SIM_RANGES_RAD["gripper"]
    return float(min(100.0, max(0.0, (g_rad - lo) / (hi - lo) * 100.0)))


def sim_to_lerobot(q_rad: Sequence[float], gripper_rad: float, signs: Mapping[str, float] | None = None,
                   offsets_deg: Mapping[str, float] | None = None) -> dict[str, float]:
    """Sim joint radians (5 arm joints, in SIM_JOINTS order) + gripper radians -> a LeRobot action dict.

    Arm joints are clipped to the model's range before conversion; the gripper maps its range to 0-100.
    """
    if len(q_rad) != len(SIM_JOINTS):
        raise ValueError(f"expected {len(SIM_JOINTS)} arm joint angles, got {len(q_rad)}")
    signs = signs or {}
    offsets_deg = offsets_deg or {}
    action: dict[str, float] = {}
    for name, q in zip(SIM_JOINTS, q_rad):
        lo, hi = SIM_RANGES_RAD[name]
        qc = min(hi, max(lo, float(q)))
        action[LEROBOT_KEYS[name]] = math.degrees(qc) * signs.get(name, 1.0) + offsets_deg.get(name, 0.0)
    action[LEROBOT_KEYS["gripper"]] = _gripper_pct(gripper_rad)
    return action


def lerobot_to_sim(action: Mapping[str, float], signs: Mapping[str, float] | None = None,
                   offsets_deg: Mapping[str, float] | None = None) -> tuple[list[float], float]:
    """Inverse of sim_to_lerobot (for reading the follower's observation back into the sim frame)."""
    signs = signs or {}
    offsets_deg = offsets_deg or {}
    q = [math.radians((float(action[LEROBOT_KEYS[n]]) - offsets_deg.get(n, 0.0)) / signs.get(n, 1.0))
         for n in SIM_JOINTS]
    lo, hi = SIM_RANGES_RAD["gripper"]
    g = lo + float(action[LEROBOT_KEYS["gripper"]]) / 100.0 * (hi - lo)
    return q, g


def so101_status() -> dict:
    """For /api/status. Never opens the serial port; only reports what is configured."""
    port = os.environ.get("HASAMI_SO101_PORT") or None
    out: dict = {"connected": False, "port": port}
    try:
        _check_setup(port)
    except SetupRequired as e:
        out["setup"] = str(e)
    return out


def _check_setup(port: str | None) -> None:
    if not port or not Path(port).exists():
        raise SetupRequired(SETUP_MESSAGE)
    try:
        _import_lerobot()
    except ImportError as e:
        raise SetupRequired(SETUP_MESSAGE) from e


def _import_lerobot():
    try:  # LeRobot >= 0.3
        from lerobot.robots.so101_follower import SO101Follower, SO101FollowerConfig
    except ImportError:  # older layout
        from lerobot.common.robots.so101_follower import SO101Follower, SO101FollowerConfig  # type: ignore
    return SO101Follower, SO101FollowerConfig


class SO101:
    """A connected SO-101 follower. Raises SetupRequired when the port or LeRobot is missing."""

    def __init__(self, port: str | None = None, robot_id: str = "hasami_follower",
                 signs: Mapping[str, float] | None = None, offsets_deg: Mapping[str, float] | None = None):
        port = port or os.environ.get("HASAMI_SO101_PORT") or None
        _check_setup(port)
        follower_cls, config_cls = _import_lerobot()
        try:
            cfg = config_cls(port=port, id=robot_id, use_degrees=True)
        except TypeError:  # versions without use_degrees report degrees already
            cfg = config_cls(port=port, id=robot_id)
        self.robot = follower_cls(cfg)
        self.robot.connect()
        self.port = port
        self.signs = dict(signs or {})
        self.offsets_deg = dict(offsets_deg or {})

    def send_joint_targets(self, q_rad: Sequence[float], gripper: float) -> dict[str, float]:
        """Send sim-frame joint targets (radians; gripper in radians too). Returns the action sent."""
        action = sim_to_lerobot(q_rad, gripper, self.signs, self.offsets_deg)
        self.robot.send_action(action)
        return action

    def read_joints(self) -> tuple[list[float], float]:
        obs = self.robot.get_observation()
        return lerobot_to_sim(obs, self.signs, self.offsets_deg)

    def close(self) -> None:
        self.robot.disconnect()


def limit_step(prev: Sequence[float] | None, target: Sequence[float], max_step: float) -> list[float]:
    """Move at most `max_step` (radians) per joint from `prev` towards `target`. A guard against a jump
    in the streamed targets (e.g. the sim being reset) reaching the real servos as one big move."""
    if prev is None:
        return [float(t) for t in target]
    return [float(p + max(-max_step, min(max_step, t - p))) for p, t in zip(prev, target)]


class RealMirror:
    """Mirror mode: stream the simulated arm's joint targets to a connected SO-101 follower.

    Plug an instance into `hasami_sim.arm.Arm.command_hook`. The sim stays the source of truth for
    planning, perception and success checks; the real arm repeats the commanded joint motion, at most
    `max_step_deg` per joint per command (~30 Hz). Written against LeRobot's follower API and the unit
    conversion tested in tests/test_robot.py, but never run on hardware.
    """

    def __init__(self, robot: SO101, max_step_deg: float = 6.0):
        self.robot = robot
        self.max_step = math.radians(max_step_deg)
        self._last: list[float] | None = None
        self.sent = 0

    def __call__(self, q_rad: Sequence[float], gripper_rad: float) -> None:
        q = limit_step(self._last, list(q_rad), self.max_step)
        self.robot.send_joint_targets(q, gripper_rad)
        self._last = q
        self.sent += 1
