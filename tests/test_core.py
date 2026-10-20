"""Session: randomised resets, a full clicked 3-step run checked from physics, the event stream, and the
run record."""

import json

import numpy as np
import pytest

from hasami_core import CUBES, SetupRequired, simulated_click, simulated_drop_clicks
from hasami_core.session import MAX_YAW, MIN_SEPARATION, REACH_X, REACH_Y
from hasami_sim.arm import in_tray

PHASES = ["above object", "descend", "close jaws", "lift", "above target", "lower", "open jaws", "retreat"]


def clicked_plan(session):
    frame = session.observe()
    picks = [simulated_click(frame.rgb, label) for label in ("red cube", "green cube", "blue cube")]
    drops = simulated_drop_clicks(frame.rgb, 3)
    return [{"pick_point": list(p), "place_point": list(d)} for p, d in zip(picks, drops)]


@pytest.mark.parametrize("seed", [3, 4, 5])
def test_reset_randomises_within_bounds(session, seed):
    out = session.reset(seed)
    assert out["seed"] == seed
    pos = {n: np.array(session.arm.object_pos(n)) for n in CUBES}
    for n in CUBES:
        x, y, z = pos[n]
        assert REACH_X[0] - 0.002 <= x <= REACH_X[1] + 0.002 and REACH_Y[0] - 0.002 <= y <= REACH_Y[1] + 0.002
        assert abs(z - 0.0125) < 0.002
        assert abs(session.arm.object_yaw(n)) <= MAX_YAW + 0.02
    for i, a in enumerate(CUBES):
        for b in CUBES[i + 1:]:
            assert np.linalg.norm(pos[a][:2] - pos[b][:2]) >= MIN_SEPARATION - 0.002
    assert np.allclose(session.arm.object_pos("tray")[:2], [0.17, 0.17])


def test_reset_is_reproducible(session):
    a = session.reset(7)["objects"]
    b = session.reset(7)["objects"]
    assert a == b


def test_full_clicked_run_places_all_three_cubes(session, tmp_path):
    session.reset(2)
    events = []
    rec = session.run(clicked_plan(session), on_event=lambda kind, data: events.append((kind, data)))

    # outcome, from physics
    assert rec["ok"] and rec["placed"] == 3, [s["result"] for s in rec["steps"]]
    for n in CUBES:
        assert in_tray(session.arm, n), f"{n} at {session.arm.object_pos(n)}"
    assert sorted(s["result"]["object"] for s in rec["steps"]) == ["blue cube", "green cube", "red cube"]
    for s in rec["steps"]:
        assert s["pick"]["iou_vs_truth"] >= 0.6
        assert s["result"]["localisation_error_mm"] <= 3.0
        assert s["result"]["disturbed"] == []

    # event order: plan, then per step segment/target (pick), segment/target (place), 8 phases, result; done
    kinds = [k for k, _ in events]
    per_step = ["segment", "target", "segment", "target"] + ["phase"] * len(PHASES) + ["result"]
    assert kinds == ["plan"] + per_step * 3 + ["done"]
    phases = [d["phase"] for k, d in events if k == "phase"]
    assert phases == PHASES * 3
    seg = next(d for k, d in events if k == "segment")
    assert seg["role"] == "pick" and seg["mask_png"] and seg["segmenter"].startswith("sam2.1")
    assert events[-1][1]["ok"] is True

    # the record
    path = session.record(rec, tmp_path)
    data = json.loads(path.read_text())
    assert path.name == f"{rec['run_id']}.json"
    for key in ("plan", "steps", "segmenter_name", "iou", "timings", "initial_poses", "final_poses", "seed"):
        assert key in data
    assert data["steps"][0]["phases"][0]["phase"] == "above object"
    assert data["iou"]["min"] >= 0.6
    assert data["steps"][0]["timings"]["pick_place_ms"] > 0


def test_meta_segmenter_without_key_is_setup_required(session, no_model_key):
    with pytest.raises(SetupRequired, match="MODEL_API_KEY"):
        session.segment(text="red cube", segmenter="meta")
    events = []
    with pytest.raises(SetupRequired):
        session.run([{"pick_point": [300, 200], "place_point": [180, 290]}], segmenter="meta",
                    on_event=lambda k, d: events.append(k))
    assert events == ["error"]


def test_local_segmenter_needs_a_point(session):
    with pytest.raises(ValueError, match="needs a point"):
        session.segment(text="red cube")
