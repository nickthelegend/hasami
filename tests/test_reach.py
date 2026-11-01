import json

import pytest

from hasami_walker.reach import ReachError, load_grid, plan_reach, save_grid


def grid():
    spots = []
    for x, y in ((10, 10), (90, 10), (10, 90), (90, 90)):
        q = [x / 10, y / 10, x / 20, y / 20, 0, 40]
        spots.append({"pixel": [x, y], "pre_grasp": q, "grasp": [v + 1 for v in q],
                      "lift": [v + 2 for v in q], "jaw_region": [x / 2, y / 2, x / 2 + 10, y / 2 + 10],
                      "mm_per_px": 0.2 + x / 1000})
    return {"version": 1, "resolution": [100, 100], "spots": spots}


def test_exact_spots_and_linear_between():
    g = grid()
    for s in g["spots"]:
        p = plan_reach(g, s["pixel"])
        assert p.grasp == pytest.approx(s["grasp"])
        assert p.mm_per_px == pytest.approx(s["mm_per_px"])
    p = plan_reach(g, (50, 50))
    assert p.pre_grasp[:2] == pytest.approx([5, 5])
    assert p.width_mm(100) == pytest.approx(25)


def test_outside_hull_and_bad_grid():
    with pytest.raises(ReachError, match="outside"):
        plan_reach(grid(), (99, 50))
    with pytest.raises(ReachError, match="resolution changed"):
        plan_reach(grid(), (50, 50), resolution=(200, 100))
    bad = grid()
    bad["spots"] = bad["spots"][:3]
    for s in bad["spots"]:
        s["pixel"][1] = 10
    with pytest.raises(ReachError, match="degenerate"):
        plan_reach(bad, (50, 10))


def test_json_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HASAMI_GRIDS_DIR", str(tmp_path))
    path = save_grid("shelf_1", grid())
    assert load_grid("shelf_1") == json.loads(path.read_text())
