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


def test_perception_disturbances_noisy_depth_miscalibrated_camera_and_light():
    """depth_mm, cam_mm and light make the camera less kind; what the pipeline sees changes by the stated
    amounts, reproducibly per scene, and the ground truth used for scoring does not."""
    d = Disturb.parse("depth_mm:2,cam_mm:3,light:0.3")
    assert d is not None and d.perception and not d.offset_mm
    assert d.to_dict() == {"depth_mm": 2.0, "cam_mm": 3.0, "light": 0.3, "offset_mm": 0.0, "slip": 0.0,
                           "label": "injected disturbance (test)"}
    with pytest.raises(ValueError):
        Disturb.parse("light:1.5")
    clean = Session(seed=4, connect_real=False, disturb=Disturb())
    noisy = Session(seed=4, connect_real=False, disturb=d)
    f0, f1 = clean.observe(), noisy.observe()
    noise = (f1.depth - f0.depth).astype(np.float64)
    assert 0.0015 < noise.std() < 0.0025 and abs(noise.mean()) < 2e-4
    assert np.linalg.norm(f1.cam_pos - f0.cam_pos) == pytest.approx(0.003, abs=1e-9)
    assert noisy._light["scale"] != 1.0 and 0.7 <= noisy._light["scale"] <= 1.3
    assert not np.array_equal(f0.rgb, f1.rgb)
    assert np.array_equal(f0.seg_geom, f1.seg_geom)  # the truth is untouched
    again = Session(seed=4, connect_real=False, disturb=d)
    assert again._light == noisy._light and np.array_equal(again._cam_err, noisy._cam_err)
    # a located cube is off by about the calibration error
    from hasami_perception import locate

    cube = noisy.arm.object_pos("red_cube")
    u, v = simulated_click(f1.rgb, "red cube")
    seg = noisy.sam.segment(f1.rgb, point=(u, v))
    t = locate(f1, seg)
    assert 0.0015 < np.linalg.norm(np.array(t.center[:2]) - cube[:2]) < 0.006
