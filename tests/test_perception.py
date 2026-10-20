"""Perception against the simulator's ground truth: SAM 2.1 masks (IoU), mask + depth localisation (mm),
and footprint yaw (degrees), on randomised scenes. The pipeline sees only RGB + depth; ground truth
is used here to score it."""

import numpy as np
import pytest

from hasami_core import CUBES, OBJECTS, simulated_click
from hasami_perception import locate, mask_iou

SEEDS = [11, 23]


def _fold(yaw: float) -> float:
    return (yaw + np.pi / 4) % (np.pi / 2) - np.pi / 4


@pytest.mark.parametrize("seed", SEEDS)
def test_sam_mask_localisation_and_yaw_vs_ground_truth(session, seed):
    session.reset(seed)
    frame = session.observe()
    for name in CUBES:
        label = OBJECTS[name]
        click = simulated_click(frame.rgb, label)
        mask = session.sam.segment(frame.rgb, point=click)
        truth = session._truth_mask(frame, name)
        iou = mask_iou(mask, truth)
        assert iou >= 0.6, f"{label}: IoU {iou:.3f}"

        target = locate(frame, mask)
        true_pos = session.arm.object_pos(name)
        err_mm = float(np.linalg.norm(np.array(target.center[:2]) - true_pos[:2]) * 1000)
        assert err_mm <= 3.0, f"{label}: centre off by {err_mm:.2f} mm"
        assert abs(target.top_z - 0.025) < 0.003, f"{label}: top at {target.top_z:.4f} m"

        true_yaw = _fold(session.arm.object_yaw(name))
        d = abs(np.degrees(np.angle(np.exp(1j * 4 * (target.yaw - true_yaw))) / 4))
        assert d <= 3.0, f"{label}: yaw off by {d:.2f} deg"


def test_yaws_are_actually_randomised(session):
    yaws = []
    for seed in SEEDS:
        session.reset(seed)
        yaws += [abs(np.degrees(session.arm.object_yaw(n))) for n in CUBES]
    assert max(yaws) > 10, "reset() should rotate the cubes"


def test_click_on_bare_table_has_no_target(session):
    session.reset(SEEDS[0])
    seg = session.segment(point=(600, 40))
    assert seg.target is None
    assert "above the table" in seg.target_error
    assert seg.truth_label == "table"
