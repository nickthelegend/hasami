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
