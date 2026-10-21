"""Benchmark task families: reproducible tasks, and every goal predicate true on a hand-placed solved
arrangement and false on a broken one (pure, no physics)."""

import copy

import numpy as np
import pytest

from hasami_core.tasks import FAMILIES, LINE_SLOTS, Body, World, in_box, in_row, make_task, moved, on_top
from hasami_sim.scene import SIDE_TRAY, classic


def start_world(task) -> World:
    spec = task.scene or classic(task.seed)
    bodies = {o.name: Body(np.array([o.x, o.y, o.base_z + o.height / 2]), o.dims, o.label) for o in spec.objects}
    return World(bodies, {c.name: c for c in spec.containers})


def solved(task, w0: World) -> World:
    """Hand-place the goal: containers get one object per quadrant, stacks are centred."""
    w = copy.deepcopy(w0)
    used: dict[str, int] = {}
    for obj, dest in task.moves:
        b = w.bodies[obj]
        if dest.startswith("@"):
            x, y = (float(v) for v in dest[1:].split(","))
            b.pos = np.array([x, y, b.dims[2] / 2])
        elif dest in w.containers:
            c = w.containers[dest]
            k = used.get(dest, 0)
            used[dest] = k + 1
            dx, dy = [(-1, -1), (1, 1), (1, -1), (-1, 1)][k]
            b.pos = np.array([c.x + dx * 0.02, c.y + dy * 0.02, c.floor_h + b.dims[2] / 2])
        else:
            base = w.bodies[dest]
            b.pos = np.array([base.pos[0], base.pos[1], base.pos[2] + base.dims[2] / 2 + b.dims[2] / 2])
    return w


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_goal_true_when_solved_false_when_broken(family, seed):
    task = make_task(family, seed)
    w0 = start_world(task)
    ok, detail = task.goal(w0, w0)
    assert not ok, f"untouched scene already satisfies {task.instruction!r}"
    w1 = solved(task, w0)
    ok, detail = task.goal(w0, w1)
    assert ok, detail

    # break the last move: the object stays where it started
    obj, _ = task.moves[-1]
    w2 = copy.deepcopy(w1)
    w2.bodies[obj].pos = w0.bodies[obj].pos.copy()
    assert not task.goal(w0, w2)[0]

    # a kept object that was knocked 2 cm fails the goal
    if task.keep:
        w3 = copy.deepcopy(w1)
        w3.bodies[task.keep[0]].pos = w3.bodies[task.keep[0]].pos + np.array([0.02, 0, 0])
        assert not task.goal(w0, w3)[0]


@pytest.mark.parametrize("family", FAMILIES)
def test_tasks_are_reproducible_and_well_formed(family):
    a, b = make_task(family, 5), make_task(family, 5)
    assert a.instruction == b.instruction and a.moves == b.moves and a.keep == b.keep
    assert (a.scene is None) == (family == "all-to-tray")
    if a.scene is not None:
        assert a.scene.to_dict() == b.scene.to_dict()
        names = {o.name for o in a.scene.objects}
        assert {m for m, _ in a.moves} <= names and set(a.keep) <= names
        assert 3 <= len(a.scene.objects) <= 8
    assert 1 <= len(a.moves) <= 6  # within the plan schema's MAX_STEPS


def test_sort_by_colour_moves_the_two_colours_and_keeps_the_rest():
    for seed in range(8):
        t = make_task("sort-by-colour", seed)
        objs = {o.name: o for o in t.scene.objects}
        assert all(objs[o].colour == t.params[dest] for o, dest in t.moves)
        assert all(objs[o].colour not in (t.params["tray"], t.params["bin"]) for o in t.keep)
        assert 4 <= len(t.scene.objects) <= 8


def test_tower_order():
    t = make_task("tower-of-3", 3)
    (b, a), (c, b2) = t.moves
    assert b == b2 and a == t.keep[0]
    assert "at the bottom" in t.instruction


def test_predicates_on_single_poses():
    cube = (0.025, 0.025, 0.025)
    w = World({"a": Body(np.array([SIDE_TRAY.x + 0.03, SIDE_TRAY.y, 0.0185]), cube),
               "b": Body(np.array([0.22, 0.0, 0.0125]), cube),
               "c": Body(np.array([0.224, 0.003, 0.0375]), cube),
               "d": Body(np.array([SIDE_TRAY.x + SIDE_TRAY.half - 0.003, SIDE_TRAY.y, 0.0365]), cube)},  # on the wall
              {"tray": SIDE_TRAY})
    assert in_box(w, "a", "tray") and not in_box(w, "b", "tray") and not in_box(w, "d", "tray")
    assert on_top(w, "c", "b") and not on_top(w, "b", "c") and not on_top(w, "a", "b")
    w2 = copy.deepcopy(w)
    w2.bodies["c"].pos = np.array([0.235, 0.0, 0.0375])  # 15 mm off centre: hanging over the edge
    assert not on_top(w2, "c", "b")
    w2.bodies["b"].pos = np.array([0.225, 0.0, 0.0125])
    assert moved(w, w2, "b", tol=0.004) and not moved(w, w2, "b")


def test_sort_by_shape_moves_cylinders_and_cubes_and_keeps_the_rest():
    for seed in range(6):
        t = make_task("sort-by-shape", seed)
        objs = {o.name: o for o in t.scene.objects}
        assert all(objs[o].shape == ("cylinder" if dest == "tray" else "cube") for o, dest in t.moves)
        assert t.keep and all(objs[o].shape in ("block", "flat") for o in t.keep)
        assert {o for o, _ in t.moves} | set(t.keep) == set(objs)


def test_line_up_slots_are_free_and_sizes_ordered():
    for seed in range(6):
        t = make_task("line-up", seed)
        objs = {o.name: o for o in t.scene.objects}
        sizes = [objs[o].dims[0] for o, _ in t.moves]
        assert sizes == sorted(sizes) and len(set(sizes)) == 3
        for o in t.scene.objects:  # nothing starts on a slot
            assert all(np.hypot(o.x - x, o.y - y) > 0.03 for x, y in LINE_SLOTS)


def test_in_row_predicate():
    cube = lambda s: (s, s, s)  # noqa: E731
    w = World({"s": Body(np.array([0.215, 0.065, 0.010]), cube(0.02)),
               "m": Body(np.array([0.217, 0.0, 0.0125]), cube(0.025)),
               "l": Body(np.array([0.214, -0.065, 0.015]), cube(0.03))})
    assert all(in_row(w, ["s", "m", "l"]).values())
    assert not in_row(w, ["l", "m", "s"])["in order, left to right"]
    w2 = copy.deepcopy(w)
    w2.bodies["m"].pos = np.array([0.232, 0.0, 0.0125])  # 17 mm off the line
    assert not in_row(w2, ["s", "m", "l"])["in a straight line"]
    w3 = copy.deepcopy(w)
    w3.bodies["m"].pos = np.array([0.215, 0.044, 0.0125])  # touching the small cube
    assert not in_row(w3, ["s", "m", "l"])["apart, not touching"]
    w4 = copy.deepcopy(w)
    w4.bodies["l"].pos = np.array([0.214, -0.065, 0.021])  # lying on something / tipped
    assert not in_row(w4, ["s", "m", "l"])["on the table"]


def test_pick_from_bin_starts_in_the_bin():
    for seed in range(6):
        t = make_task("pick-from-bin", seed)
        w0 = start_world(t)
        (target, dest), = t.moves
        assert dest == "tray" and in_box(w0, target, "bin")
        assert len(t.params["in_bin"]) == 2 and all(in_box(w0, o, "bin") for o in t.params["in_bin"])


@pytest.mark.parametrize("family,seed", [("line-up", 0), ("pick-from-bin", 0), ("sort-by-shape", 0)])
def test_new_families_in_the_simulator(session, family, seed):
    """One seed of each new family run by the simulated-click oracle in MuJoCo (physics, SAM 2.1, verify
    and retry), goal checked on the physics state."""
    from hasami_core.tasks import oracle_steps, setup_task, world_from

    task = make_task(family, seed)
    try:
        setup_task(task, session)
        w0 = world_from(session)
        rec = session.run(oracle_steps(task, session))
        ok, detail = task.goal(w0, world_from(session))
        assert ok, (detail, [s["result"].get("note") for s in rec["steps"]])
    finally:
        session.reset(1)
