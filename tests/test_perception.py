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


def test_sam_does_not_index_past_the_frame_edge(session):
    """LocalSam2.segment looks the clicked pixel up in each mask after rounding: 639.6 must not become column 640
    (IndexError). The pixel is clamped to the mask, so even a caller that skips the bounds check gets a mask."""
    session.reset(SEEDS[0])
    rgb = session.observe().rgb
    h, w = rgb.shape[:2]
    mask = session.sam.segment(rgb, point=(w - 0.4, h - 0.4))
    assert mask.shape == (h, w)


def test_every_shape_iou_localisation_and_grasp_width(session):
    """Procedural scene with one object of each kind: SAM 2.1 at an oracle click, then mask + depth."""
    from hasami_core.tasks import oracle_click
    from hasami_sim.scene import SIDE_TRAY, ObjectSpec, SceneSpec

    kinds = [("small cube", "red"), ("cube", "green"), ("big cube", "blue"), ("block", "purple"),
             ("flat box", "orange"), ("cylinder", "yellow")]
    xy = [(0.24, -0.10), (0.25, -0.03), (0.25, 0.05), (0.19, -0.07), (0.19, 0.02), (0.20, 0.10)]
    objs = [ObjectSpec(f"o{i}", k, c, x, y, yaw=0.35 if k != "cylinder" else 0.0)
            for i, ((k, c), (x, y)) in enumerate(zip(kinds, xy))]
    try:
        session.load_scene(SceneSpec(objs, [SIDE_TRAY], seed=0, name="shapes"))
        frame = session.observe()
        for o in objs:
            seg = session.segment(point=oracle_click(frame, session, o.name), frame=frame)
            assert seg.truth_label == o.label
            assert seg.iou_vs_truth >= 0.6, (o.label, seg.iou_vs_truth)
            t = seg.target
            err = np.linalg.norm(np.array(t.center[:2]) - session.arm.object_pos(o.name)[:2]) * 1000
            assert err <= 3.0, (o.label, err)
            assert abs(t.top_z - o.height) < 0.003, (o.label, t.top_z)
            assert t.round == (o.shape == "cylinder"), o.label
            _, period, width = t.grasp()
            assert abs(width - o.grip_width) < 0.004, (o.label, width)
            if o.shape in ("block", "flat"):
                assert period == pytest.approx(np.pi)
    finally:
        session.reset(1)
