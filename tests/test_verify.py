"""Grasp verification and retry: the gripper's own position says whether the jaws hold something, the
camera says whether the object left the pick spot and arrived at the place spot, and a failed step is
re-segmented from the new image and retried. Disturbances are injected (and labelled) to force failures."""

import numpy as np
import pytest

from hasami_core import Session, simulated_click, simulated_drop_clicks
from hasami_core.session import Disturb
from hasami_sim.arm import GRIPPER_CLOSED, GRIPPER_PRE_GRASP, Arm


def test_disturb_parse():
    assert Disturb.parse(None) is None and Disturb.parse("") is None
    d = Disturb.parse("offset_mm:6,slip:0.2")
    assert d is not None and d.offset_mm == 6.0 and d.slip == 0.2
    assert d.to_dict()["label"] == "injected disturbance (test)"
    assert Disturb.parse("offset_mm:0") is None
    with pytest.raises(ValueError):
        Disturb.parse("wobble:3")
    with pytest.raises(ValueError):
        Disturb.parse("slip:2")


def test_gripper_reading_tells_held_from_empty():
    arm = Arm.load()
    # nothing between the jaws: they close to their stop
    arm.set_gripper(GRIPPER_CLOSED)
    held, gap = arm.grasp_held(0.025)
    assert not held and gap < 0.004
    # a real pick: the 25 mm cube stops the jaws early
    cube = arm.object_pos("red_cube")
    arm.set_gripper(GRIPPER_PRE_GRASP, 200)
    log = arm.pick_place(cube[:2], arm.object_pos("tray")[:2], yaw=arm.object_yaw("red_cube"),
                         open_to=GRIPPER_PRE_GRASP, place_surface_z=0.006)
    assert log.held is True and log.completed
    assert [c["after"] for c in log.checks] == ["close jaws", "lift", "above target"]
    assert all(c["jaw_gap_mm"] > 12 for c in log.checks)


def _one_step(session: Session, label: str = "red cube") -> list[dict]:
    frame = session.observe()
    return [{"pick": label, "pick_point": list(simulated_click(frame.rgb, label)),
             "place": "tray", "place_point": list(simulated_drop_clicks(frame.rgb, 1)[0])}]


def test_offset_fails_the_first_attempt_and_the_retry_succeeds(session):
    session.reset(2)
    session.disturb = Disturb(offset_mm=6.0)
    events = []
    try:
        rec = session.run(_one_step(session), on_event=lambda k, d: events.append((k, d)))
    finally:
        session.disturb = None
    verifies = [d for k, d in events if k == "verify"]
    assert [v["attempt"] for v in verifies] == [1, 2]
    first, second = verifies
    assert first["injected"] == {"offset_mm": 6.0} and not first["ok"] and not first["physics_ok"]
    assert second["injected"] == {} and second["ok"] and second["physics_ok"]
    retries = [d for k, d in events if k == "retry"]
    assert len(retries) == 1 and "descend" in retries[0]["reason"] and retries[0]["attempt"] == 2
    step = rec["steps"][0]
    assert len(step["attempts"]) == 2 and step["result"]["ok"] and step["result"]["attempts"] == 2
    assert step["result"]["in_tray"] and step["result"]["object"] == "red cube"
    assert rec["disturb"]["label"] == "injected disturbance (test)"
    assert rec["verify"] == {"checks": 2, "agree": 2, "retries": 1}


def test_a_grip_that_always_slips_fails_as_grasp_after_two_retries(session):
    session.reset(5)
    session.disturb = Disturb(slip=1.0)
    events = []
    try:
        rec = session.run(_one_step(session, "green cube"), on_event=lambda k, d: events.append((k, d)))
    finally:
        session.disturb = None
    verifies = [d for k, d in events if k == "verify"]
    assert len(verifies) == 3 and all(v["grasp_held"] is False and not v["ok"] for v in verifies)
    assert all(v["agree"] for v in verifies)  # the gripper reading and the physics agree: nothing held
    assert sum(1 for k, _ in events if k == "retry") == 2
    result = rec["steps"][0]["result"]
    assert not result["ok"] and result["failure"] == "grasp" and result["attempts"] == 3
    assert "empty grasp" in result["note"]
    # the dropped cube is back on the table, not in the tray
    assert not result["in_tray"] and np.asarray(result["final_pos"])[2] < 0.02
