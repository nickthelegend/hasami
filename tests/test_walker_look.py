"""hasami_walker.look on a synthetic fridge shelf: real SAM 2.1 weights (the shared `session` fixture), no Meta key."""
import numpy as np
import pytest

from hasami_walker.look import FindError, annotate, find


def shelf_frame():
    """640x480: a pale fridge interior, a shelf edge, an orange carton and a green bottle on it."""
    rgb = np.full((480, 640, 3), (226, 230, 232), np.uint8)
    rgb[300:312, :] = (170, 176, 180)                          # shelf edge
    rgb[300:, :] = (205, 210, 214)
    carton = np.zeros((480, 640), bool)
    carton[150:300, 360:450] = True
    rgb[carton] = (236, 128, 28)                                # orange carton
    rgb[150:162, 360:450] = (250, 248, 240)                     # its white cap band
    bottle = np.zeros((480, 640), bool)
    bottle[170:300, 150:200] = True
    rgb[bottle] = (40, 150, 70)                                 # green bottle
    return rgb, carton


def iou(a, b):
    return (a & b).sum() / max(1, (a | b).sum())


def test_colour_in_the_request_finds_the_item(session, no_model_key):
    rgb, carton = shelf_frame()
    f = find(rgb, "the orange juice carton", sam2=session.sam)
    assert f.how == "colour + sam-2.1"
    assert iou(f.mask, carton) >= 0.6
    u, v = f.centroid
    assert 360 <= u <= 450 and 150 <= v <= 300
    png = annotate(rgb, f, "orange juice")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_click_finds_the_item(session, no_model_key):
    rgb, _ = shelf_frame()
    f = find(rgb, "the bottle", point=(175, 240), sam2=session.sam)
    assert f.how == "sam-2.1 point" and 150 <= f.centroid[0] <= 200


def test_no_key_no_click_no_colour_says_what_is_missing(no_model_key):
    rgb, _ = shelf_frame()
    with pytest.raises(FindError, match="MODEL_API_KEY"):
        find(rgb, "the juice")


def test_a_named_colour_that_is_not_there(session, no_model_key):
    rgb, _ = shelf_frame()
    with pytest.raises(FindError, match="nothing purple"):
        find(rgb, "the purple can", sam2=session.sam)
