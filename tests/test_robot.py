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
