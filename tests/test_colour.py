"""Image-only helpers: colour names from HSV, same-colour search, blobs, and footprint size/shape -> grasp
axes. Pure functions on synthetic arrays."""

import numpy as np
import pytest

from hasami_perception.colour import (blob_near, colour_mask, colour_word, components, name_colour, rgb_to_hsv,
                                      similar_mask)
from hasami_perception.locate import Target, footprint_extent, footprint_yaw, is_round
from hasami_sim.scene import COLOURS

TABLE = (199, 189, 173)


def _img(h=60, w=80):
    img = np.zeros((h, w, 3), np.uint8)
    img[:] = TABLE
    return img


def test_hsv_of_primaries():
    h, s, v = rgb_to_hsv(np.array([[255, 0, 0], [0, 255, 0], [0, 0, 255]], np.uint8))
    assert h.tolist() == pytest.approx([0, 120, 240]) and s.tolist() == [1, 1, 1] and v.tolist() == [1, 1, 1]


@pytest.mark.parametrize("name", list(COLOURS))
def test_scene_colours_are_named_lit_and_shaded(name):
    rgb = np.array(COLOURS[name]) * 255
    for light in (1.0, 0.75, 0.5):
        px = (rgb * light).astype(np.uint8).reshape(1, 1, 3)
        assert colour_mask(px, name)[0, 0], (name, light)
        assert name_colour(px) == name
    assert not colour_mask(np.array(TABLE, np.uint8).reshape(1, 1, 3), name)[0, 0]


def test_colour_word():
    assert colour_word("the Red cube") == "red" and colour_word("purple block") == "purple"
    assert colour_word("clicked object") is None


def test_components_and_blob_near():
    img = _img()
    img[5:15, 5:15] = (220, 40, 40)  # red, 100 px
    img[40:52, 60:72] = (220, 40, 40)  # red, 144 px
    img[30:40, 20:30] = (40, 60, 220)  # blue
    red = colour_mask(img, "red")
    comps = components(red)
    assert [len(c) for c in comps] == [144, 100]
    assert blob_near(red, (10, 10), min_pixels=20) == pytest.approx((9.5, 9.5), abs=0.6)
    assert blob_near(red, (70, 50), min_pixels=20) == pytest.approx((65.5, 45.5), abs=0.6)
    assert blob_near(red, (40, 30), max_dist_px=5, min_pixels=20) is None
    excl = np.zeros_like(red)
    excl[40:52, 60:72] = True
    assert blob_near(red, (70, 50), min_pixels=20, exclude=excl) == pytest.approx((9.5, 9.5), abs=0.6)
    same = similar_mask(img, np.array([200, 30, 30]))
    assert same.sum() == 244
    with pytest.raises(ValueError):
        similar_mask(img, np.array(TABLE))


def _grid(step=0.0012):
    return np.mgrid[-0.03:0.03:step, -0.03:0.03:step].reshape(2, -1).T


def _box(dx, dy, yaw):
    g = _grid()
    c, s = np.cos(yaw), np.sin(yaw)
    r = g @ np.array([[c, -s], [s, c]])
    return g[(np.abs(r[:, 0]) <= dx / 2) & (np.abs(r[:, 1]) <= dy / 2)]


@pytest.mark.parametrize("yaw", [0.0, 0.3, -0.6])
def test_footprint_shape_and_grasp_axes(yaw):
    disc = _grid()[np.hypot(*_grid().T) <= 0.012]
    assert is_round(disc)
    sq = _box(0.025, 0.025, yaw)
    blk = _box(0.036, 0.022, yaw)
    assert not is_round(sq) and not is_round(blk)

    def target(xy, rnd=False):
        y = footprint_yaw(xy)
        return Target([0, 0, 0.0125], 0.025, y, len(xy), [0, 0], footprint_extent(xy, y, 0.0012), rnd)

    jaw, period, width = target(disc, True).grasp()
    assert period is None and width == pytest.approx(0.024, abs=0.0025)
    jaw, period, width = target(sq).grasp()
    assert period == pytest.approx(np.pi / 2) and width == pytest.approx(0.025, abs=0.002)
    jaw, period, width = target(blk).grasp()
    assert period == pytest.approx(np.pi) and width == pytest.approx(0.022, abs=0.002)
    # the jaws close across the narrow side: perpendicular to the block's long axis (yaw), mod 180 deg
    d = np.degrees(np.angle(np.exp(2j * (jaw - (yaw + np.pi / 2)))) / 2)
    assert abs(d) < 3.0
