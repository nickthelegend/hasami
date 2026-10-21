"""Benchmark task families: a seeded scene sampler, an instruction, the moves a perfect planner would make,
and a goal predicate checked against the physics state.

Families:
  all-to-tray      the classic scene (three 25 mm cubes, yaw +-40 deg): every cube into the tray
  sort-by-colour   4-8 mixed objects: one colour into the tray, another into the grey bin, the rest stay
  stack-two        put one cube on another
  clear-except-X   everything into the tray except one named object, which must not move
  tower-of-3       three cubes stacked into a tower in a named order
  sort-by-shape    the cylinders into the tray, the cubes into the grey bin, blocks and flat boxes stay
  line-up          three cubes of three sizes in a row on the table, smallest to largest, left to right as
                   the overhead camera sees it (+y to -y)
  pick-from-bin    three objects start inside the grey bin: take the named one out into the tray, the
                   other two stay in the bin

Goal predicates are pure functions of a World (object poses and sizes), so they are tested on hand-placed
poses (tests/test_tasks.py) as well as used on the simulator's state after a run.

The "clicks" planner here is a simulated-click oracle, labelled as such everywhere it is reported: it
knows from the task which object each step moves and roughly where it is (as a planner that understood
the instruction would), and clicks the middle of that object's colour blob in the rendered RGB image.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from hasami_perception.colour import blob_near, colour_mask
from hasami_sim.scene import (COLOURS, SIDE_BIN, SIDE_TRAY, TRAY, ContainerSpec, ObjectSpec, SceneSpec, classic,
                              place_objects)

MAX_YAW_BIN = np.radians(30.0)

FAMILIES = ["all-to-tray", "sort-by-colour", "stack-two", "clear-except-X", "tower-of-3", "sort-by-shape", "line-up",
            "pick-from-bin"]
# line-up: the row's three slots on the table (world m), smallest first; left to right in the overhead image
LINE_SLOTS = [(0.215, 0.065), (0.215, 0.0), (0.215, -0.065)]
ORACLE_LABEL = ("simulated-click oracle: the task says which object each step moves; the click is the middle "
                "of that object's colour blob in the rendered RGB image")


# ---- world state and goal predicates (pure) -----------------------------------------------------
@dataclass
class Body:
    pos: np.ndarray  # centre, m
    dims: tuple[float, float, float]  # full size x, y, z (cylinder: diameter, diameter, height)
    label: str = ""


@dataclass
class World:
    bodies: dict[str, Body]
    containers: dict[str, ContainerSpec] = field(default_factory=dict)

    def pos(self, name: str) -> np.ndarray:
        return np.asarray(self.bodies[name].pos, dtype=float)


def in_box(w: World, name: str, container: str) -> bool:
    """The object's centre is inside the container's walls and it rests low (not on a wall)."""
    c = w.containers[container]
    p = w.pos(name)
    return bool(abs(p[0] - c.x) < c.inner_half and abs(p[1] - c.y) < c.inner_half and p[2] < c.floor_h + 0.045)


def in_any_box(w: World, name: str) -> bool:
    return any(in_box(w, name, c) for c in w.containers)


def on_top(w: World, a: str, b: str, z_tol: float = 0.006) -> bool:
    """a rests on b: a's centre is within b's half-footprint (minus 2 mm) and one stacked height above b's."""
    pa, pb = w.pos(a), w.pos(b)
    ha, hb = w.bodies[a].dims[2], w.bodies[b].dims[2]
    reach = 0.5 * min(w.bodies[b].dims[0], w.bodies[b].dims[1]) - 0.002
    return bool(np.hypot(*(pa[:2] - pb[:2])) < reach and abs(pa[2] - (pb[2] + hb / 2 + ha / 2)) < z_tol)


def moved(w0: World, w1: World, name: str, tol: float = 0.01) -> bool:
    return bool(np.linalg.norm(w1.pos(name) - w0.pos(name)) > tol)


def upright_on_table(w: World, name: str, tol: float = 0.004) -> bool:
    return bool(abs(w.pos(name)[2] - w.bodies[name].dims[2] / 2) < tol)


def in_row(w: World, names: list[str], max_off: float = 0.010, max_gap: float = 0.09,
           max_tilt_deg: float = 30.0) -> dict[str, bool]:
    """Objects stand in a straight row on the table in this order along -y (left to right in the overhead
    image): every centre within `max_off` of the best-fit line, the line within `max_tilt_deg` of the y
    axis, neighbours apart (not touching, at most `max_gap` between centres), each upright on the table."""
    p = np.array([w.pos(n)[:2] for n in names])
    c = p.mean(axis=0)
    _, _, vt = np.linalg.svd(p - c)
    d = vt[0] if vt[0][1] < 0 else -vt[0]  # points along -y
    off = np.abs((p - c) @ np.array([-d[1], d[0]]))
    t = (p - c) @ d
    tilt = np.degrees(np.arccos(min(1.0, abs(d[1]))))
    out = {"on the table": all(upright_on_table(w, n) for n in names),
           "in a straight line": bool(off.max() < max_off and tilt < max_tilt_deg),
           "in order, left to right": bool(np.all(np.diff(t) > 0))}
    gaps = []
    for a, b in zip(names, names[1:]):
        dist = float(np.hypot(*(w.pos(a)[:2] - w.pos(b)[:2])))
        need = 0.5 * (max(w.bodies[a].dims[:2]) + max(w.bodies[b].dims[:2])) * 1.05
        gaps.append(need < dist < max_gap)
    out["apart, not touching"] = all(gaps)
    return out


# ---- tasks ----------------------------------------------------------------------------------------
@dataclass
class Task:
    family: str
    seed: int
    instruction: str
    scene: SceneSpec | None  # None: the classic scene, laid out by Session.reset(seed)
    moves: list[tuple[str, str]]  # (object, destination container or object), in order
    keep: list[str] = field(default_factory=list)  # objects that must not move
    params: dict = field(default_factory=dict)

    def goal(self, w0: World, w1: World) -> tuple[bool, dict]:
        """Is the goal met in w1 (w0: the start)? Returns (ok, per-condition detail)."""
        detail: dict[str, bool] = {}
        f = self.family
        if f in ("all-to-tray", "clear-except-X", "sort-by-colour", "sort-by-shape"):
            for obj, dest in self.moves:
                detail[f"{obj} in {dest}"] = in_box(w1, obj, dest)
            for obj in self.keep:
                detail[f"{obj} not moved"] = not moved(w0, w1, obj) and not in_any_box(w1, obj)
        elif f == "pick-from-bin":
            for obj, dest in self.moves:
                detail[f"{obj} in {dest}"] = in_box(w1, obj, dest)
            for obj in self.keep:
                if obj in self.params.get("in_bin", []):
                    detail[f"{obj} still in the bin"] = in_box(w1, obj, "bin") and not moved(w0, w1, obj, 0.015)
                else:
                    detail[f"{obj} not moved"] = not moved(w0, w1, obj)
        elif f == "line-up":
            row = in_row(w1, [obj for obj, _ in self.moves])
            detail.update({f"row: {k}": v for k, v in row.items()})
            for obj in self.keep:
                detail[f"{obj} not moved"] = not moved(w0, w1, obj)
        elif f in ("stack-two", "tower-of-3"):
            for obj, dest in self.moves:
                detail[f"{obj} on {dest}"] = on_top(w1, obj, dest)
            for obj in self.keep:
                detail[f"{obj} not moved"] = not moved(w0, w1, obj)
        else:
            raise ValueError(f"unknown family {f!r}")
        return all(detail.values()), detail


def _label(o: ObjectSpec) -> str:
    return f"{o.colour} {o.kind}"


def _names(objs: list[ObjectSpec]) -> list[ObjectSpec]:
    for i, o in enumerate(objs):
        o.name = f"obj{i}_{o.colour}_{o.kind.replace(' ', '_')}"
    return objs


def _layout(rng: np.random.Generator, objs: list[ObjectSpec], containers: list[ContainerSpec], seed: int,
            name: str) -> SceneSpec:
    if not place_objects(rng, objs, containers):
        raise ValueError(f"{name}: no layout for seed {seed}")
    return SceneSpec(_names(objs), containers, seed=seed, name=name)


def make_task(family: str, seed: int) -> Task:
    rng = np.random.default_rng([seed, FAMILIES.index(family) if family in FAMILIES else 99])
    if family == "all-to-tray":
        spec = classic(seed)
        return Task(family, seed, "put all the cubes in the tray", None,
                    [(o.name, "tray") for o in spec.objects], params={"scene": "classic"})

    if family == "sort-by-colour":
        colours = list(COLOURS)
        a, b = (str(c) for c in rng.choice(colours, size=2, replace=False))
        rest = [c for c in colours if c not in (a, b)]
        kinds = ["cube", "small cube", "big cube", "block", "cylinder"]
        n_a, n_b = int(rng.integers(1, 3)), int(rng.integers(1, 3))
        n_d = int(rng.integers(max(0, 4 - n_a - n_b), 8 - n_a - n_b + 1))
        cols = [a] * n_a + [b] * n_b + [str(rng.choice(rest)) for _ in range(n_d)]
        objs = [ObjectSpec("", str(rng.choice(kinds)), c, 0.0, 0.0) for c in cols]
        spec = _layout(rng, objs, [SIDE_TRAY, SIDE_BIN], seed, family)
        moves = [(o.name, "tray") for o in spec.objects if o.colour == a]
        moves += [(o.name, "bin") for o in spec.objects if o.colour == b]
        keep = [o.name for o in spec.objects if o.colour not in (a, b)]
        return Task(family, seed, f"put the {a} things in the tray and the {b} things in the grey bin; "
                    "leave everything else where it is", spec, moves, keep, {"tray": a, "bin": b})

    if family == "stack-two":
        cols = [str(c) for c in rng.choice(list(COLOURS), size=int(rng.integers(3, 6)), replace=False)]
        kinds = ["cube"] * 2 + [str(rng.choice(["small cube", "block", "cylinder", "flat box"])) for _ in cols[2:]]
        objs = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(kinds, cols)]
        spec = _layout(rng, objs, [SIDE_TRAY], seed, family)
        top, base = spec.objects[0], spec.objects[1]
        return Task(family, seed, f"put the {_label(top)} on the {_label(base)}", spec, [(top.name, base.name)],
                    [o.name for o in spec.objects if o.name != top.name])

    if family == "clear-except-X":
        cols = [str(c) for c in rng.choice(list(COLOURS), size=4, replace=False)]
        kinds = [str(rng.choice(["cube", "small cube", "block", "cylinder"])) for _ in cols]
        objs = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(kinds, cols)]
        spec = _layout(rng, objs, [SIDE_TRAY], seed, family)
        keep = spec.objects[int(rng.integers(0, len(spec.objects)))]
        return Task(family, seed, f"put everything in the tray except the {_label(keep)}", spec,
                    [(o.name, "tray") for o in spec.objects if o.name != keep.name], [keep.name])

    if family == "tower-of-3":
        cols = [str(c) for c in rng.choice(list(COLOURS), size=int(rng.integers(3, 6)), replace=False)]
        kinds = ["cube"] * 3 + [str(rng.choice(["small cube", "block", "cylinder"])) for _ in cols[3:]]
        objs = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(kinds, cols)]
        spec = _layout(rng, objs, [SIDE_TRAY], seed, family)
        a, b, c = spec.objects[:3]
        return Task(family, seed, f"build a tower: the {a.colour} cube at the bottom, then the {b.colour} cube, "
                    f"then the {c.colour} cube on top", spec, [(b.name, a.name), (c.name, b.name)],
                    [a.name] + [o.name for o in spec.objects[3:]])

    if family == "sort-by-shape":
        n_cyl, n_cube = int(rng.integers(1, 3)), int(rng.integers(1, 3))
        n_other = int(rng.integers(1, 3))
        kinds = (["cylinder"] * n_cyl + [str(rng.choice(["cube", "small cube", "big cube"])) for _ in range(n_cube)]
                 + [str(rng.choice(["block", "flat box"])) for _ in range(n_other)])
        cols = [str(c) for c in rng.choice(list(COLOURS), size=len(kinds), replace=len(kinds) > len(COLOURS))]
        objs = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(kinds, cols)]
        spec = _layout(rng, objs, [SIDE_TRAY, SIDE_BIN], seed, family)
        moves = [(o.name, "tray") for o in spec.objects if o.shape == "cylinder"]
        moves += [(o.name, "bin") for o in spec.objects if o.shape == "cube"]
        keep = [o.name for o in spec.objects if o.shape not in ("cylinder", "cube")]
        return Task(family, seed, "put the cylinders in the tray and the cubes in the grey bin; leave the blocks "
                    "and flat boxes where they are", spec, moves, keep, {"tray": "cylinder", "bin": "cube"})

    if family == "line-up":
        sizes = ["small cube", "cube", "big cube"]
        cols = [str(c) for c in rng.choice(list(COLOURS), size=3 + int(rng.integers(0, 3)), replace=False)]
        objs = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(sizes, cols[:3])]
        objs += [ObjectSpec("", str(rng.choice(["cylinder", "block"])), c, 0.0, 0.0) for c in cols[3:]]
        order = [int(i) for i in rng.permutation(len(objs))]  # sizes are not in layout order
        objs = [objs[i] for i in order]
        avoid = [(x, y, 0.03) for x, y in LINE_SLOTS]
        if not place_objects(rng, objs, [SIDE_TRAY], avoid=avoid):
            raise ValueError(f"{family}: no layout for seed {seed}")
        spec = SceneSpec(_names(objs), [SIDE_TRAY], seed=seed, name=family)
        cubes = sorted([o for o in spec.objects if o.shape == "cube"], key=lambda o: o.dims[0])
        moves = [(o.name, f"@{x:.3f},{y:.3f}") for o, (x, y) in zip(cubes, LINE_SLOTS)]
        keep = [o.name for o in spec.objects if o.shape != "cube"]
        return Task(family, seed, "line up the three cubes in a row on the table, smallest to largest, left to "
                    "right", spec, moves, keep, {"order": [o.name for o in cubes]})

    if family == "pick-from-bin":
        cols = [str(c) for c in rng.choice(list(COLOURS), size=3 + int(rng.integers(0, 3)), replace=False)]
        kinds = [str(rng.choice(["cube", "small cube", "block"])) for _ in cols]
        inside = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(kinds[:3], cols[:3])]
        d = 0.026  # quadrant centres: ~27 mm between neighbours' sides, ~20 mm to the walls
        spots = [(SIDE_BIN.x + sx * d, SIDE_BIN.y + sy * d) for sx, sy in ((-1, -1), (1, 1), (1, -1), (-1, 1))]
        for o, i in zip(inside, rng.permutation(4)[:3]):
            o.x, o.y = float(spots[i][0] + rng.uniform(-0.003, 0.003)), float(spots[i][1] + rng.uniform(-0.003, 0.003))
            o.yaw = float(rng.uniform(-MAX_YAW_BIN, MAX_YAW_BIN))
            o.base_z = SIDE_BIN.floor_h
        outside = [ObjectSpec("", k, c, 0.0, 0.0) for k, c in zip(kinds[3:], cols[3:])]
        if outside and not place_objects(rng, outside, [SIDE_TRAY, SIDE_BIN]):
            raise ValueError(f"{family}: no layout for seed {seed}")
        spec = SceneSpec(_names(inside + outside), [SIDE_TRAY, SIDE_BIN], seed=seed, name=family)
        target = spec.objects[int(rng.integers(0, 3))]
        keep = [o.name for o in spec.objects if o.name != target.name]
        return Task(family, seed, f"take the {_label(target)} out of the grey bin and put it in the tray; leave the "
                    "other things in the bin", spec, [(target.name, "tray")], keep,
                    {"in_bin": [o.name for o in spec.objects[:3] if o.name != target.name]})

    raise ValueError(f"unknown family {family!r}; known: {FAMILIES}")


# ---- the simulated-click oracle ---------------------------------------------------------------------
def container_spots(c: ContainerSpec) -> list[tuple[float, float]]:
    """Four separate drop spots inside a container (quadrant centres), in the order they are used."""
    d = min(c.inner_half / 2, 0.02)  # 20 mm from the centre keeps the fingers off the walls
    return [(c.x - d, c.y - d), (c.x + d, c.y + d), (c.x + d, c.y - d), (c.x - d, c.y + d)]


def oracle_click(frame, session, name: str) -> tuple[float, float]:
    """Where the oracle clicks for one object: the middle of its colour blob nearest the object's projected
    top centre. Falls back to the projected point if the blob is not visible."""
    o = session.scene.object(name)
    p = session.arm.object_pos(name)
    top = np.array([p[0], p[1], p[2] + o.height / 2])
    u, v = frame.world_to_pixel(top)
    try:
        cm = colour_mask(frame.rgb, o.colour)
    except KeyError:  # a twin's grey / white / dark object: no hue to search by, click the projection
        return round(u, 1), round(v, 1)
    c = blob_near(cm, (u, v), max_dist_px=25, min_pixels=20)
    return (round(c[0], 1), round(c[1], 1)) if c is not None else (round(u, 1), round(v, 1))


def oracle_steps(task: Task, session) -> list[dict]:
    """The click plan for a task in the session's current scene (the scene must already be loaded)."""
    frame = session.observe()
    if task.family == "all-to-tray":  # exactly the original demo's clicks (colour rules on the RGB image)
        from .clicks import simulated_click, simulated_drop_clicks

        labels = [session.objects[o] for o, _ in task.moves]
        drops = simulated_drop_clicks(frame.rgb, len(labels))
        return [{"pick": lab, "pick_point": list(simulated_click(frame.rgb, lab)), "place": "tray",
                 "place_point": list(d), "reason": "simulated-click oracle"} for lab, d in zip(labels, drops)]
    used: dict[str, int] = {}
    steps = []
    for obj, dest in task.moves:
        o = session.scene.object(obj)
        pick = oracle_click(frame, session, obj)
        if dest.startswith("@"):  # a spot on the bare table (line-up)
            x, y = (float(v) for v in dest[1:].split(","))
            place = frame.world_to_pixel(np.array([x, y, 0.0]))
            steps.append({"pick": _label(o), "pick_point": [round(pick[0], 1), round(pick[1], 1)], "place": "table",
                          "place_point": [round(float(place[0]), 1), round(float(place[1]), 1)],
                          "reason": "simulated-click oracle"})
            continue
        if dest in {c.name for c in session.scene.containers}:
            c = session.scene.container(dest)
            k = used.get(dest, 0)
            used[dest] = k + 1
            x, y = container_spots(c)[k % 4]
            place = frame.world_to_pixel(np.array([x, y, c.floor_h]))
        else:
            # Stacking: click the top of the base object. If an earlier step of this plan stacks the base
            # on another object, click where its top will be then (one level up, over that object).
            below = next((b for m, b in task.moves if m == dest), None)
            if below is not None:
                bp = session.arm.object_pos(below)
                h = session.scene.object(below).height + session.scene.object(dest).height
                place = frame.world_to_pixel(np.array([bp[0], bp[1], h]))
            else:
                place = oracle_click(frame, session, dest)
        steps.append({"pick": _label(o), "pick_point": [round(pick[0], 1), round(pick[1], 1)],
                      "place": session.objects[dest],
                      "place_point": [round(float(place[0]), 1), round(float(place[1]), 1)],
                      "reason": "simulated-click oracle"})
    return steps


SCRIPTED_LABEL = ("scripted Muse-format plan: written from the task by code (no language model); each step "
                  "names the object by colour and shape, and its hint pixel is moved a fixed distance in a random "
                  "direction from the object's colour-blob centre, as a planner's pixel might miss")


def scripted_steps(task: Task, session, hint_px: float = 20.0, seed: int = 0) -> list[dict]:
    """The Muse plan format (label + hint pixel per pick and place) written from the task: the oracle's clicks,
    each pick hint moved `hint_px` in a random direction. Places keep their spots (a container or a stacking
    base is named, so grounding can still find it; a table spot is named 'the table')."""
    rng = np.random.default_rng([seed, int(hint_px * 10), 31])
    base = oracle_steps(task, session)
    out = []
    for (obj, dest), st in zip(task.moves, base):
        a = rng.uniform(0, 2 * np.pi)
        u, v = st["pick_point"]
        o = session.scene.object(obj)
        place = "the table" if dest.startswith("@") else session.objects[dest]
        out.append({"pick": f"{o.colour} {o.kind}", "pick_point": [round(u + hint_px * np.cos(a), 1),
                                                                   round(v + hint_px * np.sin(a), 1)],
                    "place": place, "place_point": st["place_point"],
                    "reason": f"scripted from the task; pick hint {hint_px:g} px off"})
    return out


def world_from(session) -> World:
    """The physics state as a World (ground truth: for goal checks only)."""
    bodies = {o.name: Body(session.arm.object_pos(o.name), o.dims, o.label) for o in session.scene.objects}
    return World(bodies, {c.name: c for c in session.scene.containers})


def setup_task(task: Task, session) -> dict:
    """Load the task's scene into the session (classic: Session.reset(seed))."""
    if task.scene is None:
        return session.reset(task.seed)
    return session.load_scene(task.scene)


__all__ = ["FAMILIES", "LINE_SLOTS", "ORACLE_LABEL", "Body", "Task", "World", "container_spots", "in_any_box", "in_box",
           "in_row", "make_task", "moved", "on_top", "oracle_click", "oracle_steps", "scripted_steps", "setup_task",
           "upright_on_table", "SCRIPTED_LABEL",
           "world_from", "TRAY"]
