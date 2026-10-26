"""The simulated fridge fetch: the scene, its reach, the shelf survey (real SAM 2.1), whole fetches in MuJoCo, and
the verify-and-retry loop catching a failed grasp. Nothing is mocked; MODEL_API_KEY is unset, so the Muse step
must say Setup required and the named item is used as given."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import mujoco
import numpy as np
import pytest

from hasami_core.fetch import FETCH_PARK_Q, HEAD, FridgeFetch, check_item, item_named
from hasami_core.session import ROOT, Disturb, Session
from hasami_perception import mask_iou
from hasami_perception.shelf import survey
from hasami_planner import SETUP_MESSAGE
from hasami_sim.arm import Arm
from hasami_sim.camera import capture
from hasami_sim.scene import BASKET, FRIDGE, ITEMS, FridgeSpec, SceneSpec, build, fridge_scene


@pytest.fixture(scope="module")
def fetch(session):
    """A fridge on its own Session (the shared one stays on the tabletop), borrowing the loaded SAM 2.1."""
    s = Session(connect_real=False, disturb=Disturb())
    s._sam = session.sam
    return FridgeFetch(s, seed=0)


def _parked(spec: SceneSpec) -> Arm:
    m = build(spec)
    arm = Arm(m, mujoco.MjData(m))
    for j, v in zip(["shoulder_pan", "shoulder_lift", "elbow_flex", "wrist_flex", "wrist_roll"], FETCH_PARK_Q):
        arm.data.qpos[m.jnt_qposadr[arm._jid(j)]] = v
        arm.data.ctrl[arm._aid(j)] = v
    mujoco.mj_forward(m, arm.data)
    arm.settle(300)
    return arm


def _clear_radius(arm: Arm, width: float) -> float:
    ax = arm.pan_axis
    return min(float(np.hypot(FRIDGE.front - ax[0], y - ax[1])) for y in (-FRIDGE.inner_half_w, FRIDGE.inner_half_w)) \
        - width / 2 - 0.025


def test_item_named_from_the_users_words():
    assert item_named("the juice carton") == "juice carton"
    assert item_named("Bring me the juice from the fridge!") == "juice carton"
    assert item_named("a can of coke please") == "can"
    assert item_named("the water bottle") == "bottle"
    assert item_named("the cheese") is None


@pytest.mark.parametrize("seed", [0, 1, 2])
def test_fridge_scene_builds_and_its_items_stand_on_the_shelf(seed):
    spec = fridge_scene(seed)
    assert spec.name == "fridge" and spec.fridge is not None and [c.name for c in spec.containers] == ["basket"]
    assert sorted(o.kind for o in spec.objects) == sorted(ITEMS)
    again = SceneSpec.from_dict(json.loads(json.dumps(spec.to_dict())))
    assert json.dumps(again.to_dict()) == json.dumps(spec.to_dict())
    assert isinstance(again.fridge, FridgeSpec) and again.fridge.shelf_z == FRIDGE.shelf_z
    arm = _parked(spec)
    for o in spec.objects:
        p = arm.object_pos(o.name)
        # resting on the shelf, inside the cabinet, upright, where the spec put it
        assert p[2] == pytest.approx(FRIDGE.shelf_z + o.height / 2, abs=0.002)
        assert FRIDGE.front < p[0] - o.dims[0] / 2 and p[0] + o.dims[0] / 2 < FRIDGE.back
        assert abs(p[1]) + o.dims[1] / 2 < FRIDGE.inner_half_w
        assert np.hypot(p[0] - o.x, p[1] - o.y) < 0.002
        bid = mujoco.mj_name2id(arm.model, mujoco.mjtObj.mjOBJ_BODY, o.name)
        assert arm.data.xmat[bid].reshape(3, 3)[2, 2] > 0.999
    # the head camera sees every item whole-ish; the parked arm hides none of them
    f = capture(arm.model, arm.data, HEAD)
    for o in spec.objects:
        bid = mujoco.mj_name2id(arm.model, mujoco.mjtObj.mjOBJ_BODY, o.name)
        assert np.isin(f.seg_geom, np.nonzero(arm.model.geom_bodyid == bid)[0]).sum() > 800
    # the overhead camera cannot see into the fridge (why the head camera exists), but sees the basket
    top = capture(arm.model, arm.data, "overhead")
    bb = mujoco.mj_name2id(arm.model, mujoco.mjtObj.mjOBJ_BODY, "basket")
    assert np.isin(top.seg_geom, np.nonzero(arm.model.geom_bodyid == bb)[0]).sum() > 2000
    for o in spec.objects:
        bid = mujoco.mj_name2id(arm.model, mujoco.mjtObj.mjOBJ_BODY, o.name)
        assert np.isin(top.seg_geom, np.nonzero(arm.model.geom_bodyid == bid)[0]).sum() == 0


def test_layouts_differ_by_seed_and_are_reproducible():
    a, b = fridge_scene(0).to_dict(), fridge_scene(0).to_dict()
    assert a == b
    orders = {tuple(o.kind for o in sorted(fridge_scene(s).objects, key=lambda o: o.y)) for s in range(6)}
    assert len(orders) >= 3


@pytest.mark.parametrize("seed", [0, 1])
def test_every_item_is_reachable_from_the_front(seed):
    """plan_front finds a tilt at which every pose of the fetch is solved; the approach points into the fridge
    and at most 40 degrees below level (from the front, not from above), and the jaw axis is level."""
    spec = fridge_scene(seed)
    arm = _parked(spec)
    for o in spec.objects:
        p = arm.object_pos(o.name)
        plan = arm.plan_front(p, o.grip_width, o.height, FRIDGE.shelf_z, (BASKET.x, BASKET.y), BASKET.floor_h,
                              _clear_radius(arm, o.grip_width))
        assert plan is not None, o.name
        assert 15 <= plan["pitch_deg"] <= 40
        a = plan["approach"]["approach"]
        assert a[0] > 0.8 and -np.sin(np.radians(41)) < a[2] < 0  # forward, tilted down by the plan's tilt only
        assert abs(plan["jaw_axis"][2]) < 1e-9 and abs(arm.jaw_axis(plan["q"]["approach"])[2]) < 0.06  # level within 3.5 deg
        # the pose in front of the item is outside it, the grasp pose has the tips at the item's far side of centre
        assert plan["poses"]["pre-grasp"][0] < p[0] - o.dims[0] / 2
        assert plan["poses"]["lower"][2] > BASKET.floor_h
        # the item comes back out past the cabinet's front corners before the carry swings it round
        w = plan["poses"]["withdraw"][:2] - arm.pan_axis
        assert np.linalg.norm(w) <= _clear_radius(arm, o.grip_width) + 0.012


def test_an_item_at_the_back_of_the_shelf_is_out_of_reach():
    spec = fridge_scene(0, items=["can"])
    spec.objects[0].x = FRIDGE.back - 0.03  # 30 mm from the back wall: past the SO-101's reach
    arm = _parked(spec)
    p = arm.object_pos("can")
    assert arm.plan_front(p, 0.030, 0.056, FRIDGE.shelf_z, (BASKET.x, BASKET.y), BASKET.floor_h,
                          _clear_radius(arm, 0.030)) is None


@pytest.mark.parametrize("seed", [0, 3])
def test_shelf_survey_finds_and_measures_each_item(fetch, seed):
    """From the head camera alone: the shelf height, one SAM 2.1 mask per item (IoU > 0.95 against the truth),
    centre within 2 mm, height within 3 mm, the right colour, and each passes only its own item's check."""
    fetch.load(seed)
    s = fetch.session
    frame = s.observe(HEAD)
    shelf, found = survey(frame, lambda point, box: s.sam.segment(frame.rgb, point=point, box=box))
    assert shelf.z == pytest.approx(FRIDGE.shelf_z, abs=0.002)
    assert len(found) == len(ITEMS)
    seen = set()
    for e in found:
        it = e["item"]
        name = next(n for n in ITEMS if not check_item(it, n))
        assert [n for n in ITEMS if not check_item(it, n)] == [name]  # exactly one item fits
        body = name.replace(" ", "_")
        assert mask_iou(e["mask"], s._truth_mask(frame, body)) > 0.95
        assert np.linalg.norm(np.array(it.center[:2]) - s.arm.object_pos(body)[:2]) < 0.002
        assert it.height == pytest.approx(ITEMS[name][1][2], abs=0.003)
        seen.add(name)
    assert seen == set(ITEMS)


@pytest.mark.parametrize("seed", [0, 1])
@pytest.mark.parametrize("item", list(ITEMS))
def test_fetch_puts_each_item_in_the_basket(fetch, no_model_key, seed, item):
    """A whole fetch in MuJoCo: found by the head camera and SAM 2.1, taken from the front, carried and lowered
    into the basket upright, the robot's own check agreeing with the physics, nothing else moved."""
    fetch.load(seed)
    events = []
    rec = fetch.run(f"the {item}", on_event=lambda k, d: events.append((k, d)))
    assert rec["ok"] and rec["placed"] == 1 and rec["task"] == "fridge fetch", rec["steps"][0]["result"]
    res = rec["steps"][0]["result"]
    assert res["in_basket"] and res["upright"] and res["attempts"] == 1 and res["disturbed"] == []
    assert res["object"] == f"{ITEMS[item][2]} {item}"
    att = rec["steps"][0]["attempts"][0]
    assert att["verify"]["ok"] and att["verify"]["physics_ok"] and att["verify"]["shelf_clear"]
    assert att["verify"]["in_basket_seen"] and att["grasp"]["approach"] == "front"
    assert [c["held"] for c in att["grasp_checks"]] == [True, True, True, True]
    assert att["pick"]["iou_vs_truth"] > 0.95 and att["localisation_error_mm"] < 2.0
    phases = [p["phase"] for p in att["phases"]]
    assert phases[:3] == ["pre-grasp", "approach", "close jaws"] and phases[-3:] == ["lower", "open jaws", "retreat"]
    assert all(p["ok"] for p in att["phases"])
    kinds = [k for k, _ in events]
    assert kinds[0] == "plan" and kinds[-2:] == ["result", "done"] and "verify" in kinds and "retry" not in kinds
    assert rec["verify"] == {"checks": 1, "agree": 1, "retries": 0}


def test_verify_catches_a_failed_grasp_and_retries(fetch, no_model_key):
    """Labelled injected fault: the first grasp stops 30 mm short, so the jaws close in front of the bottle. The
    gripper reading says empty, the step is retried from a new head-camera frame, and the second attempt
    puts the bottle in the basket."""
    fetch.load(0)
    fetch.session.disturb = Disturb(short_mm=30.0)
    events = []
    try:
        rec = fetch.run("the bottle", on_event=lambda k, d: events.append((k, d)))
    finally:
        fetch.session.disturb = Disturb()
    verifies = [d for k, d in events if k == "verify"]
    assert [v["attempt"] for v in verifies] == [1, 2]
    first, second = verifies
    assert first["injected"] == {"short_mm": 30.0} and first["grasp_held"] is False
    assert not first["ok"] and not first["physics_ok"] and first["agree"]
    assert second["injected"] == {} and second["ok"] and second["physics_ok"]
    retries = [d for k, d in events if k == "retry"]
    assert len(retries) == 1 and "empty grasp" in retries[0]["reason"]
    step = rec["steps"][0]
    assert step["attempts"][0]["note"] == retries[0]["reason"] and step["attempts"][0]["failure"] == "grasp"
    assert step["result"]["ok"] and step["result"]["attempts"] == 2 and step["result"]["in_basket"]
    assert rec["disturb"]["label"] == "injected disturbance (test)" and rec["verify"]["retries"] == 1


def test_without_a_key_muse_says_setup_required_and_an_unknown_item_stops_before_moving(fetch, no_model_key):
    fetch.load(0)
    und = fetch.understand("bring me the juice from the fridge")
    assert und == {"sentence": "bring me the juice from the fridge", "source": "user words", "muse": SETUP_MESSAGE,
                   "item": "juice carton"}
    before = fetch.arm.data.qpos.copy()
    with pytest.raises(ValueError, match="no fridge item named in 'the cheese'; the fridge holds: juice carton, can, bottle"):
        fetch.run("the cheese")
    assert np.array_equal(fetch.arm.data.qpos, before)
    assert fetch.session.last_run["error"].startswith("ValueError")


def test_an_item_that_is_not_there_is_not_fetched(fetch, no_model_key):
    """The scene has no bottle: the survey sees two things, neither passes the bottle's check, the arm stays."""
    s = fetch.session
    s.load_scene(fridge_scene(0, items=["juice carton", "can"]), camera=HEAD, park_q=FETCH_PARK_Q)
    before = {n: s.arm.object_pos(n) for n in s.movables}
    rec = fetch.run("the bottle")
    res = rec["steps"][0]["result"]
    assert not rec["ok"] and res["failure"] == "segment" and res["attempts"] == 1
    assert "no bottle on the shelf" in res["note"] and "looks orange, the bottle is green" in res["note"]
    assert "phases" not in rec["steps"][0]["attempts"][0]
    assert all(np.linalg.norm(s.arm.object_pos(n) - p) < 1e-3 for n, p in before.items())


def test_run_fetch_script_records_a_run(tmp_path):
    """`python scripts/run_fetch.py "the juice carton"` end to end (no video): prints the outcome and writes a run
    record in the tabletop runs' format with the step images next to it."""
    env = {k: v for k, v in os.environ.items() if k not in ("MODEL_API_KEY", "HASAMI_DISTURB")}
    env["HASAMI_RUNS_DIR"] = str(tmp_path)
    out = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_fetch.py"), "the juice carton", "--seed", "1",
                          "--no-video"], capture_output=True, text=True, env=env, timeout=600)
    assert out.returncode == 0, out.stdout + out.stderr
    assert f"Muse: {SETUP_MESSAGE}" in out.stdout and "OK: the orange juice carton is in the basket (upright)" in out.stdout
    recs = list(tmp_path.glob("*.json"))
    assert len(recs) == 1
    rec = json.loads(recs[0].read_text())
    for key in ("run_id", "started_at", "segmenter", "seed", "simulation", "scene", "plan", "initial_poses", "steps", "ok",
                "placed", "total", "finished_at", "final_poses", "timings", "iou", "verify"):
        assert key in rec
    assert rec["ok"] and rec["seed"] == 1 and rec["plan"]["source"] == "user words"
    imgs = rec["steps"][0]["attempts"][0]["images"]
    assert {"frame", "after", "pick", "pick_truth", "basket_frame", "basket", "basket_after"} <= set(imgs)
    assert all((tmp_path / rec["run_id"] / name).is_file() for name in imgs.values())
