"""SO-101 adapter: the sim -> LeRobot conversion math, and SetupRequired without hardware. No arm here."""

import math

import mujoco
import pytest

from hasami_robot import (LEROBOT_KEYS, SETUP_MESSAGE, SIM_JOINTS, SIM_RANGES_RAD, SO101, SetupRequired,
                          lerobot_to_sim, sim_to_lerobot, so101_status)
from hasami_sim.arm import ASSETS, GRIPPER_CLOSED, GRIPPER_OPEN

EXACT = "Setup required: HASAMI_SO101_PORT (SO-101 follower arm on USB) and `pip install lerobot[feetech]`"


def test_keys_follow_lerobot_names():
    action = sim_to_lerobot([0, 0, 0, 0, 0], 0.0)
    assert list(action) == ["shoulder_pan.pos", "shoulder_lift.pos", "elbow_flex.pos", "wrist_flex.pos",
                            "wrist_roll.pos", "gripper.pos"]
    assert LEROBOT_KEYS["gripper"] == "gripper.pos"


def test_radians_to_degrees():
    action = sim_to_lerobot([0.0, math.pi / 2, -math.pi / 4, 0.5, -1.0], GRIPPER_CLOSED)
    assert action["shoulder_pan.pos"] == pytest.approx(0.0)
    assert action["shoulder_lift.pos"] == pytest.approx(90.0)
    assert action["elbow_flex.pos"] == pytest.approx(-45.0)
    assert action["wrist_flex.pos"] == pytest.approx(math.degrees(0.5))
    assert action["wrist_roll.pos"] == pytest.approx(math.degrees(-1.0))


def test_clips_to_joint_range():
    action = sim_to_lerobot([5.0, -5.0, 0, 0, 0], 0.0)
    assert action["shoulder_pan.pos"] == pytest.approx(math.degrees(SIM_RANGES_RAD["shoulder_pan"][1]))
    assert action["shoulder_lift.pos"] == pytest.approx(math.degrees(SIM_RANGES_RAD["shoulder_lift"][0]))


def test_gripper_maps_range_to_0_100():
    lo, hi = SIM_RANGES_RAD["gripper"]
    assert sim_to_lerobot([0] * 5, lo)["gripper.pos"] == pytest.approx(0.0)
    assert sim_to_lerobot([0] * 5, hi)["gripper.pos"] == pytest.approx(100.0)
    assert sim_to_lerobot([0] * 5, hi + 1)["gripper.pos"] == 100.0
    closed = sim_to_lerobot([0] * 5, GRIPPER_CLOSED)["gripper.pos"]
    opened = sim_to_lerobot([0] * 5, GRIPPER_OPEN)["gripper.pos"]
    assert 0 <= closed < 1 and 70 < opened < 73


def test_signs_offsets_and_roundtrip():
    q = [0.3, -0.7, 1.1, -0.2, 2.0]
    signs = {"wrist_roll": -1.0}
    offsets = {"shoulder_pan": 2.5}
    action = sim_to_lerobot(q, 0.6, signs, offsets)
    assert action["wrist_roll.pos"] == pytest.approx(-math.degrees(2.0))
    assert action["shoulder_pan.pos"] == pytest.approx(math.degrees(0.3) + 2.5)
    back, g = lerobot_to_sim(action, signs, offsets)
    assert back == pytest.approx(q)
    assert g == pytest.approx(0.6)


def test_ranges_match_the_mjcf():
    model = mujoco.MjModel.from_xml_path(str(ASSETS / "scene_hasami.xml"))
    for name in SIM_JOINTS + ["gripper"]:
        jid = mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)
        assert tuple(model.jnt_range[jid]) == pytest.approx(SIM_RANGES_RAD[name], abs=1e-4)


def test_wrong_number_of_joints():
    with pytest.raises(ValueError):
        sim_to_lerobot([0, 0, 0], 0.0)


def test_setup_required_without_port(monkeypatch):
    monkeypatch.delenv("HASAMI_SO101_PORT", raising=False)
    with pytest.raises(SetupRequired) as e:
        SO101()
    assert str(e.value) == EXACT == SETUP_MESSAGE
    st = so101_status()
    assert st == {"connected": False, "port": None, "setup": EXACT}


def test_setup_required_when_port_missing(monkeypatch, tmp_path):
    port = str(tmp_path / "tty.usbmodem-not-here")
    monkeypatch.setenv("HASAMI_SO101_PORT", port)
    with pytest.raises(SetupRequired, match="HASAMI_SO101_PORT"):
        SO101()
    assert so101_status()["port"] == port


def test_limit_step_caps_each_joint():
    from hasami_robot import limit_step

    assert limit_step(None, [1.0, -2.0], 0.1) == [1.0, -2.0]
    assert limit_step([0.0, 0.0, 0.0], [1.0, -0.05, -3.0], 0.1) == pytest.approx([0.1, -0.05, -0.1])


def test_sim_arm_streams_its_joint_targets_to_the_hook():
    import numpy as np

    from hasami_sim.arm import Arm

    arm = Arm.load()
    seen: list[tuple[np.ndarray, float]] = []
    arm.command_hook = lambda q, g: seen.append((q, g))
    goal = np.array([0.2, -1.0, 1.0, 1.0, 0.1])
    arm.move_to(goal, steps=170)
    assert len(seen) >= 10  # ~30 Hz of simulated time over 0.34 s, plus the final target
    assert seen[-1][0] == pytest.approx(goal)
    arm.set_gripper(GRIPPER_CLOSED, steps=100)
    assert seen[-1][1] == pytest.approx(GRIPPER_CLOSED)


def test_mirror_converts_and_limits_what_it_sends():
    """RealMirror against a recording stand-in for the follower (there is no arm on this machine): it checks
    the per-command step limit and the units handed to LeRobot, not the hardware."""
    from hasami_robot import RealMirror

    class Recorder:
        def __init__(self):
            self.actions = []

        def send_joint_targets(self, q, g):
            self.actions.append(sim_to_lerobot(q, g))

    rec = Recorder()
    mirror = RealMirror(rec, max_step_deg=6.0)  # type: ignore[arg-type]
    mirror([0, 0, 0, 0, 0], 0.0)
    mirror([1.0, 0, 0, 0, 0], GRIPPER_OPEN)  # a 57 deg jump arrives as 6 deg
    assert rec.actions[1]["shoulder_pan.pos"] == pytest.approx(6.0)
    assert 70 < rec.actions[1]["gripper.pos"] < 73
    assert mirror.sent == 2


def test_session_stays_sim_when_the_port_is_missing(monkeypatch, tmp_path):
    from hasami_core import Session, status

    monkeypatch.setenv("HASAMI_SO101_PORT", str(tmp_path / "tty.usbmodem-not-here"))
    s = Session(seed=1)
    assert s.arm_kind == "sim" and s.real is None and s.arm.command_hook is None
    assert s.real_setup == EXACT
    st = status(s)
    assert st["arm"] == "sim" and st["so101"]["connected"] is False and st["so101"]["setup"] == EXACT
