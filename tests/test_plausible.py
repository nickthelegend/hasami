"""The target check before any move (hasami_perception.plausible) and what the session does when it fails:
a boxed re-prompt, then a colour / raised-blob search, then the step is skipped as 'implausible' without
moving the arm.

The leak is the one the noisy-camera sweep found: SAM 2.1 at a pixel a tipped object has left returns the
whole-table hypothesis, and on noisy depth the "target" is the table's middle, 594 mm wide. Some tests here
inject that leaked mask into the real SAM 2.1 (labelled fault injection, like HASAMI_DISTURB); one replays
the real case from the sweep with no injection at all."""

import numpy as np
import pytest

from hasami_core import Session, simulated_click, simulated_drop_clicks
from hasami_core.session import Disturb
from hasami_perception import Target, locate
from hasami_perception.plausible import check_target, edge_pixels, grow_box, priors_for

CUBE = (0.025, 0.025, 0.025)
GRIP = 0.0407  # the pre-grasp jaw opening (Arm.jaw_gap(GRIPPER_PRE_GRASP))


def _priors():
    return priors_for([CUBE, CUBE, CUBE], GRIP, [(0.17, 0.17, 0.05)])


def _table_leak(shape=(480, 640), width_px=594) -> np.ndarray:
    """A synthetic mask 594 px wide (about 594 mm at the overhead camera's ~1 mm/px) running off the frame."""
    m = np.zeros(shape, bool)
    u0 = (shape[1] - width_px) // 2
    m[40:shape[0], u0:u0 + width_px] = True
    return m


def test_synthetic_594mm_target_is_rejected():
    t = Target(center=[0.249, -0.013, 0.003], top_z=0.006, yaw=0.0, pixels=1768, centroid_px=[320, 260],
               extent=[0.594, 0.798])
    reasons = check_target(t, _table_leak(), _priors())
    text = "; ".join(reasons)
    assert "594 mm across its narrow side" in text  # wider than the open jaws
    assert "798 mm long" in text  # longer than any object here
    assert "top 6 mm" in text  # a table, not an object
    assert "frame edge" in text  # the mask ran off the frame


def test_normal_cube_passes():
    t = Target(center=[0.235, 0.075, 0.0125], top_z=0.025, yaw=0.3, pixels=450, centroid_px=[300, 200],
               extent=[0.026, 0.027])
    mask = np.zeros((480, 640), bool)
    mask[190:215, 290:315] = True
    assert check_target(t, mask, _priors()) == []


def test_tipped_and_leaning_objects_pass_wrong_places_fail():
    pri = priors_for([(0.024, 0.024, 0.040), CUBE], GRIP)
    lying = Target(center=[0.22, 0.0, 0.012], top_z=0.024, yaw=0.0, pixels=600, centroid_px=[0, 0],
                   extent=[0.041, 0.025])  # a cylinder on its side
    assert check_target(lying, None, pri) == []
    leaning = Target(center=[0.22, 0.0, 0.015], top_z=0.03, yaw=0.0, pixels=600, centroid_px=[0, 0],
                     extent=[0.025, 0.035])  # a cube leaning on an edge shows its diagonal
    assert check_target(leaning, None, pri) == []
    far = Target(center=[0.40, 0.0, 0.0125], top_z=0.025, yaw=0.0, pixels=450, centroid_px=[0, 0],
                 extent=[0.026, 0.026])
    assert any("outside the workspace" in r for r in check_target(far, None, pri))
    speck = Target(center=[0.22, 0.0, 0.003], top_z=0.007, yaw=0.0, pixels=12, centroid_px=[0, 0],
                   extent=[0.004, 0.006])
    assert any("top 7 mm" in r for r in check_target(speck, None, pri))
    # inside a container's footprint counts as the workspace even outside the reach annulus
    pri_c = priors_for([CUBE], GRIP, [(0.10, -0.21, 0.065)])
    in_bin = Target(center=[0.06, -0.26, 0.019], top_z=0.031, yaw=0.0, pixels=450, centroid_px=[0, 0],
                    extent=[0.026, 0.026])
    assert check_target(in_bin, None, pri_c) == []


def test_edge_pixels_and_grow_box():
    m = np.zeros((10, 12), bool)
    m[3:5, 4:6] = True
    assert edge_pixels(m) == 0
    assert grow_box(m, 2) == (2.0, 1.0, 7.0, 6.0)
    m[0, :] = True
    assert edge_pixels(m) == 12


def test_located_leak_on_a_real_frame_is_rejected_and_a_real_cube_mask_passes(session):
    """On a real (noisy) overhead frame: a mask of the bare table locates to a target hundreds of mm wide
    (noise lifts a few hundred table pixels over the 5 mm cut) and is rejected; the ground-truth mask of a cube locates to a plausible target."""
    session.reset(1)
    session.disturb = Disturb(depth_mm=2.0)
    try:
        frame = session.observe()
    finally:
        session.disturb = None
    # the table's pixels (the simulator's segmentation builds the synthetic leak; perception never sees it)
    table_geoms = np.nonzero(session.arm.model.geom_bodyid == 0)[0]
    leak = np.isin(frame.seg_geom, table_geoms)
    t = locate(frame, leak)
    assert max(t.extent) > 0.3
    assert check_target(t, leak, session.priors)
    cube = session._truth_mask(frame, "red_cube")
    assert check_target(locate(frame, cube), cube, session.priors) == []


def _one_step(session: Session, label: str = "red cube", point=None) -> list[dict]:
    frame = session.observe()
    return [{"pick": label, "pick_point": list(point or simulated_click(frame.rgb, label)),
             "place": "tray", "place_point": list(simulated_drop_clicks(frame.rgb, 1)[0])}]


def _inject_leak(monkeypatch, session, when):
    """Fault injection: the real SAM 2.1 returns the whole frame (the table leak) for prompts where
    when(point, box) is true. Every other prompt goes to SAM unchanged."""
    real = session.sam.segment
    calls = []

    def segment(rgb, *, point=None, text=None, box=None):
        calls.append({"point": point, "box": box})
        if when(point, box):
            return np.ones(rgb.shape[:2], bool)
        return real(rgb, point=point, text=text, box=box)

    monkeypatch.setattr(session.sam, "segment", segment)
    return calls


def test_leaked_pick_is_retried_with_a_box_and_succeeds(session, monkeypatch):
    session.reset(2)
    steps = _one_step(session)
    state = {"first": True}

    def first_plain_prompt(point, box):
        hit = state["first"] and box is None
        state["first"] = False
        return hit

    _inject_leak(monkeypatch, session, first_plain_prompt)
    rec = session.run(steps)
    a = rec["steps"][0]["attempts"][0]
    check = a["target_check"]
    assert check["ok"] and check["source"] == "tighter prompt (box)"
    assert check["tries"][0]["rejected"] and check["tries"][1]["rejected"] == []
    assert check["tries"][1]["box"] is not None
    assert a["pick"]["truth_label"] == "red cube"
    assert rec["steps"][0]["result"]["ok"], rec["steps"][0]["result"]


def test_leaked_pick_falls_back_to_the_colour_blob(session, monkeypatch):
    session.reset(2)
    steps = _one_step(session)
    monkeypatch.setattr(session, "max_retries", 0)  # the verify prompts leak too; one attempt is enough here
    _inject_leak(monkeypatch, session, lambda point, box: True)  # every SAM prompt leaks (a box cuts it)
    rec = session.run(steps)
    check = rec["steps"][0]["attempts"][0]["target_check"]
    sources = [t["source"] for t in check["tries"]]
    assert sources[:2] == ["first", "tighter prompt (box)"]
    assert check["ok"] and check["source"] == "'red' blob (the blob itself)", check["tries"]
    assert rec["steps"][0]["result"]["object"] == "red cube"


def test_no_plausible_target_skips_the_step_without_moving_the_arm(session, monkeypatch):
    """The old code sent the arm to the leaked target's centre; now the step is skipped before any move."""
    session.reset(2)
    frame = session.observe()
    # a click on bare table far from every object, every SAM prompt leaking: nothing plausible to find
    steps = _one_step(session, "red cube", point=(600.0, 40.0))
    _inject_leak(monkeypatch, session, lambda point, box: True)
    before = {n: session.arm.object_pos(n).copy() for n in session.movables}
    events = []
    rec = session.run(steps, on_event=lambda k, d: events.append((k, d)))
    step = rec["steps"][0]
    assert len(step["attempts"]) == 1  # skipped, not retried into the same leak
    r = step["result"]
    assert not r["ok"] and r["failure"] == "implausible"
    assert r["note"].startswith("skipped: implausible target")
    assert not [d for k, d in events if k == "phase" and d.get("step") == 0]  # no pick-and-place phase ran
    assert "phases" not in step["attempts"][0]
    for n, p in before.items():
        assert np.linalg.norm(session.arm.object_pos(n) - p) < 1e-3, n
    assert frame is not None


def test_noisy_camera_case_from_the_sweep_is_skipped_not_moved(session):
    """tower-of-3 seed 0 under depth_mm:2,cam_mm:3,light:0.3 (the 20261021T170928Z sweep), real SAM, no
    injection: the orange cube falls off the tower, SAM at its old pixel returns the table (594 mm wide),
    and the arm used to be sent there ('IK residual 326 mm'). Now that attempt is skipped."""
    from hasami_core.tasks import make_task, oracle_steps, setup_task

    session.disturb = Disturb(depth_mm=2.0, cam_mm=3.0, light=0.3)
    try:
        task = make_task("tower-of-3", 0)
        setup_task(task, session)
        rec = session.run(oracle_steps(task, session))
    finally:
        session.disturb = None
        session.reset(1)
    notes = [a.get("phases") and next((p["note"] for p in a["phases"] if not p["ok"]), "") or ""
             for s in rec["steps"] for a in s["attempts"]]
    assert not any("IK residual" in n and float(n.split("IK residual ")[1].split()[0]) > 100 for n in notes), notes
    checks = [a["target_check"] for s in rec["steps"] for a in s["attempts"] if "target_check" in a]
    rejected = [c for c in checks if c["tries"][0]["rejected"]]
    assert rejected and any(e > 500 for c in rejected for e in c["tries"][0].get("extent_mm", []))
    assert rec["steps"][1]["result"]["failure"] == "implausible"


@pytest.mark.parametrize("seed", [1])
def test_clean_runs_pass_the_check_first_time(session, seed):
    session.reset(seed)
    frame = session.observe()
    steps = [{"pick": lab, "pick_point": list(simulated_click(frame.rgb, lab)), "place": "tray",
              "place_point": list(d)} for lab, d in zip(["red cube"], simulated_drop_clicks(frame.rgb, 1))]
    rec = session.run(steps)
    c = rec["steps"][0]["attempts"][0]["target_check"]
    assert c["ok"] and c["source"] == "first" and c["tries"][0]["rejected"] == []
