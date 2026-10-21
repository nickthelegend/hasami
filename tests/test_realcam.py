"""Real-camera geometry on synthetic cameras (no webcam is opened): homography from four A4 corners, the
camera pose recovered from it, and an object's top face located on the table plane with and without the
parallax correction. This is the math only; real measurements of real objects are pending (TESTING.md)."""

import numpy as np
import pytest

from hasami_perception import SetupRequired
from hasami_perception.realcam import (a4_corners, apply_h, grab_frame, homography, intrinsics_from_fov,
                                       locate_on_plane, pixel_to_plane, pose_from_homography)

W, H = 1280, 720


def camera(tilt_deg=35.0, yaw_deg=12.0, dist=600.0):
    """A camera looking down at the table plane (mm) from `dist`, tilted and turned."""
    k = intrinsics_from_fov(W, H, 70)
    t_, y_ = np.radians(tilt_deg), np.radians(yaw_deg)
    rz = np.array([[np.cos(y_), -np.sin(y_), 0], [np.sin(y_), np.cos(y_), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(np.pi - t_), -np.sin(np.pi - t_)], [0, np.sin(np.pi - t_), np.cos(np.pi - t_)]])
    r = rx @ rz  # world (plane) -> camera; camera z looks down at the table
    centre = np.array([150.0, -dist * np.sin(t_) + 150, dist * np.cos(t_)])
    t = -r @ centre
    return k, r, t


def project(k, r, t, pts3):
    p = (k @ (r @ np.asarray(pts3, float).T + t[:, None])).T
    return p[:, :2] / p[:, 2:3]


def test_homography_from_a4_corners_and_pose():
    k, r, t = camera()
    plane = a4_corners()
    img = project(k, r, t, np.c_[plane, np.zeros(4)])
    h = homography(plane, img)
    pts = np.array([[30.0, 40.0], [180.0, 250.0], [105.0, 148.5]])
    assert apply_h(h, pts) == pytest.approx(project(k, r, t, np.c_[pts, np.zeros(3)]), abs=1e-6)
    assert pixel_to_plane(h, img) == pytest.approx(plane, abs=1e-6)
    r2, t2 = pose_from_homography(h, k)
    assert r2 == pytest.approx(r, abs=1e-6) and t2 == pytest.approx(t, abs=1e-4)
    with pytest.raises(ValueError):
        homography(plane[:3], img[:3])


def _raster(poly_px):
    """Pixels inside a convex polygon (image coordinates)."""
    vs, us = np.mgrid[0:H, 0:W]
    inside = np.ones((H, W), bool)
    n = len(poly_px)
    sign = None
    for i in range(n):
        (x0, y0), (x1, y1) = poly_px[i], poly_px[(i + 1) % n]
        cr = (x1 - x0) * (vs - y0) - (y1 - y0) * (us - x0)
        if sign is None:
            sign = np.sign(np.sum(cr > 0) - np.sum(cr < 0))
        inside &= cr * sign >= 0
    return inside


@pytest.mark.parametrize("yaw_deg", [0.0, 20.0, -35.0])
def test_locate_a_box_top_face_with_parallax_correction(yaw_deg):
    k, r, t = camera()
    h = homography(a4_corners(), project(k, r, t, np.c_[a4_corners(), np.zeros(4)]))
    # a 50 x 30 mm box, 40 mm tall, standing at (120, 170) mm on the table
    cx, cy, hgt, yaw = 120.0, 170.0, 40.0, np.radians(yaw_deg)
    c, s = np.cos(yaw), np.sin(yaw)
    corners = np.array([[25, 15], [-25, 15], [-25, -15], [25, -15]]) @ np.array([[c, s], [-s, c]]) + [cx, cy]
    mask = _raster(project(k, r, t, np.c_[corners, np.full(4, hgt)]))
    got = locate_on_plane(mask, h, k, hgt)
    assert got.parallax_corrected
    assert np.hypot(got.x_mm - cx, got.y_mm - cy) < 1.0
    d = np.degrees(np.angle(np.exp(4j * (got.yaw - yaw))) / 4)
    assert abs(d) < 1.5
    assert sorted(got.extent_mm) == pytest.approx([30.0, 50.0], abs=2.5)
    # without intrinsics the top face is treated as lying on the table: centimetres of parallax error
    flat = locate_on_plane(mask, h, None, hgt)
    assert not flat.parallax_corrected and np.hypot(flat.x_mm - cx, flat.y_mm - cy) > 10.0


def test_capture_needs_setup(monkeypatch):
    monkeypatch.delenv("HASAMI_REALCAM", raising=False)
    with pytest.raises(SetupRequired, match="HASAMI_REALCAM"):
        grab_frame()
