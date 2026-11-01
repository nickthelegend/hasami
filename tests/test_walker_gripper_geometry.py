import pytest

from hasami_walker.gripper_geometry import estimate_width


def test_width_estimate_needs_scale_and_checks_stroke():
    unknown = estimate_width(120)
    assert unknown["width_mm"] is None and unknown["fits_stroke"] is None and unknown["scale"] == "unknown"
    assert unknown["fits_open_gap"] is None
    measured = estimate_width(120, mm_per_pixel=0.5, open_gap_mm=50)
    assert measured["width_mm"] == 60 and measured["fits_stroke"] and not measured["fits_open_gap"]
    far = estimate_width(120, distance_mm=600, focal_px=600)
    assert far["width_mm"] == 120 and not far["fits_stroke"]
    with pytest.raises(ValueError):
        estimate_width(120, distance_mm=600)
