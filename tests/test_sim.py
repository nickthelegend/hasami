"""The simulated SO-101: inverse kinematics accuracy under real (sagging, force-limited) servos, and the
pick-and-place skill moving a cube into the tray by physics alone."""

import numpy as np
import pytest

from hasami_sim.arm import Arm, in_tray


@pytest.fixture(scope="module")
def arm():
    return Arm.load()


TARGETS = [
    (0.20, 0.00, 0.08),
    (0.25, 0.05, 0.05),
    (0.22, -0.08, 0.04),
    (0.17, 0.17, 0.07),
    (0.27, -0.02, 0.03),
]


@pytest.mark.parametrize("target", TARGETS)
def test_ik_reach_error_under_4mm(arm, target):
    log = []
    arm.go("reach", np.array(target), None, log)
    reached = arm.gripper_pos()
    err_mm = float(np.linalg.norm(reached - np.array(target)) * 1000)
    assert err_mm < 4.0, f"gripper site ended {err_mm:.2f} mm from {target}"
    assert log[-1].ok


def test_ik_solution_is_exact_in_kinematics(arm):
    q, err = arm.ik(np.array([0.22, 0.03, 0.06]))
    assert err < 1e-3
    assert q.shape == (5,)


def test_pick_place_puts_a_cube_in_the_tray():
    arm = Arm.load()
    cube = arm.object_pos("red_cube")
    tray = arm.object_pos("tray")
    log = arm.pick_place(cube[:2], tray[:2], yaw=arm.object_yaw("red_cube"))
    phases = [lg.phase for lg in log]
    assert phases == ["above object", "descend", "close jaws", "lift", "above target", "lower", "open jaws", "retreat"]
    assert all(lg.ok for lg in log)
    assert in_tray(arm, "red_cube"), f"red cube ended at {arm.object_pos('red_cube')}"
    # the other cubes were not touched
    assert not in_tray(arm, "green_cube") and not in_tray(arm, "blue_cube")


def test_pick_place_reports_each_phase_as_it_finishes():
    arm = Arm.load()
    seen = []
    cube = arm.object_pos("blue_cube")
    log = arm.pick_place(cube[:2], arm.object_pos("tray")[:2], yaw=arm.object_yaw("blue_cube"),
                         on_log=lambda lg: seen.append(lg.phase))
    assert seen == [lg.phase for lg in log]


# ---- shapes and stacking (procedural scenes; poses from physics, so this tests the arm, not perception) ----
def _scene_arm(objs):
    import mujoco

    from hasami_sim.scene import SIDE_TRAY, SceneSpec, build

    m = build(SceneSpec(objs, [SIDE_TRAY]))
    a = Arm(m, mujoco.MjData(m))
    a.home()
    return a


def _grasp_for(o, yaw):
    """Jaw yaw, period and width for an object of known shape (what perception's Target.grasp() derives)."""
    dx, dy, _ = o.dims
    if o.shape == "cylinder":
        return 0.0, None, o.grip_width
    if o.shape == "cube":
        return yaw, np.pi / 2, o.grip_width
    return (yaw + np.pi / 2 if dy < dx else yaw), np.pi, o.grip_width


@pytest.mark.parametrize("kind", ["small cube", "cube", "big cube", "block", "flat box", "cylinder"])
@pytest.mark.parametrize("yaw", [0.0, 0.6])
def test_each_shape_goes_into_the_tray(kind, yaw):
    from hasami_sim.arm import GRIPPER_PRE_GRASP, in_container
    from hasami_sim.scene import SIDE_TRAY, ObjectSpec

    o = ObjectSpec("o", kind, "red", 0.22, 0.03, yaw=yaw)
    arm = _scene_arm([o])
    jaw, period, width = _grasp_for(o, arm.object_yaw("o"))
    log = arm.pick_place(arm.object_pos("o")[:2], (SIDE_TRAY.x, SIDE_TRAY.y), yaw=jaw, object_height=o.height,
                         grip_width=width, yaw_period=period, place_surface_z=SIDE_TRAY.floor_h,
                         open_to=GRIPPER_PRE_GRASP)
    assert log.held and log.completed, [(lg.phase, lg.note) for lg in log if not lg.ok]
    assert in_container(arm, "o", SIDE_TRAY), arm.object_pos("o")


def test_place_height_from_the_surface_builds_a_tower_of_three():
    """The object is lowered until its bottom is 4 mm above the measured surface (here: the top of the
    object below), then released with a narrow opening, so it stays put."""
    from hasami_sim.arm import GRIPPER_PRE_GRASP
    from hasami_sim.scene import ObjectSpec

    objs = [ObjectSpec("a", "cube", "red", 0.22, 0.05, yaw=0.3), ObjectSpec("b", "cube", "blue", 0.24, -0.04, yaw=-0.2),
            ObjectSpec("c", "cube", "green", 0.18, -0.09, yaw=0.5)]
    arm = _scene_arm(objs)
    for top, base, surface in (("a", "b", 0.025), ("c", "a", 0.050)):
        log = arm.pick_place(arm.object_pos(top)[:2], arm.object_pos(base)[:2], yaw=arm.object_yaw(top),
                             object_height=0.025, grip_width=0.025, place_surface_z=surface, open_to=GRIPPER_PRE_GRASP)
        assert log.completed and log.held
    a, b, c = (arm.object_pos(n) for n in "abc")
    assert a[2] == pytest.approx(0.0375, abs=0.003) and c[2] == pytest.approx(0.0625, abs=0.003)
    assert np.linalg.norm(a[:2] - b[:2]) < 0.008 and np.linalg.norm(c[:2] - a[:2]) < 0.008
