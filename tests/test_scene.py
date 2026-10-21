"""Procedural scenes (mujoco.MjSpec): what gets built, reproducible sampling, and layouts inside the reach
annulus and clear of the containers."""

import mujoco
import numpy as np
import pytest

from hasami_sim.scene import (COLOURS, REACH_R, SHAPES, SIDE_BIN, SIDE_TRAY, ContainerSpec, ObjectSpec, SceneSpec,
                              build, classic, footprint_radius, sample_scene)


def _geom(m, name):
    return mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_GEOM, name)


def test_build_adds_objects_and_containers_with_their_sizes():
    spec = SceneSpec([ObjectSpec("a", "block", "purple", 0.22, 0.0, yaw=0.3),
                      ObjectSpec("b", "cylinder", "yellow", 0.25, 0.08)], [SIDE_TRAY, SIDE_BIN])
    m = build(spec)
    ga, gb = _geom(m, "a"), _geom(m, "b")
    assert m.geom_type[ga] == mujoco.mjtGeom.mjGEOM_BOX
    assert m.geom_size[ga] == pytest.approx(np.array(SHAPES["block"][1]) / 2)
    assert m.geom_type[gb] == mujoco.mjtGeom.mjGEOM_CYLINDER
    assert m.geom_size[gb][:2] == pytest.approx([0.012, 0.020])
    assert m.geom_rgba[ga][:3] == pytest.approx(COLOURS["purple"])
    for c in ("tray", "bin"):
        assert mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, c) >= 0
        assert _geom(m, f"{c}_floor") >= 0 and _geom(m, f"{c}_wall_n") >= 0
    d = mujoco.MjData(m)
    mujoco.mj_forward(m, d)
    bid = mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, "a")
    assert d.xpos[bid] == pytest.approx([0.22, 0.0, 0.0125])
    assert spec.labels == {"a": "purple block", "b": "yellow cylinder", "tray": "tray", "bin": "grey bin"}


def test_spec_round_trips_through_a_dict():
    spec = sample_scene(4, containers=[SIDE_TRAY, SIDE_BIN])
    again = SceneSpec.from_dict(spec.to_dict())
    assert again.to_dict() == spec.to_dict()


@pytest.mark.parametrize("seed", range(6))
def test_sampled_layouts_are_reproducible_reachable_and_apart(seed):
    a = sample_scene(seed, containers=[SIDE_TRAY, SIDE_BIN])
    b = sample_scene(seed, containers=[SIDE_TRAY, SIDE_BIN])
    assert a.to_dict() == b.to_dict()
    assert 4 <= len(a.objects) <= 8
    for i, o in enumerate(a.objects):
        assert REACH_R[0] <= np.hypot(o.x, o.y) <= REACH_R[1]
        for c in a.containers:
            assert max(abs(o.x - c.x), abs(o.y - c.y)) > c.half + footprint_radius(o)
        for p in a.objects[i + 1:]:
            assert np.hypot(o.x - p.x, o.y - p.y) >= footprint_radius(o) + footprint_radius(p) + 0.02
        if o.shape == "cylinder":
            assert o.yaw == 0.0


def test_eight_objects_fit():
    spec = sample_scene(0, n=8, containers=[SIDE_TRAY, SIDE_BIN])
    assert len(spec.objects) == 8
    m = build(spec)
    assert m.nbody > 8


def test_classic_matches_the_original_scene():
    spec = classic()
    assert [o.label for o in spec.objects] == ["red cube", "green cube", "blue cube"]
    assert spec.containers[0] == ContainerSpec("tray", "tray", 0.17, 0.17)
