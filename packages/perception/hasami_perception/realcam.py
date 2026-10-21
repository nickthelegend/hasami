"""Real-camera perception (a Mac webcam, no arm): the table as a plane, measured with a sheet of A4 paper or
a printed ArUco marker, and SAM 2.1 masks turned into table-plane positions, yaw and size.

Geometry (pure numpy, unit-tested on synthetic cameras):
  * homography(plane_pts, image_pts): DLT with Hartley normalisation. H maps table-plane millimetres
    (z = 0) to pixels.
  * pose_from_homography(H, K): the camera pose from H when the intrinsics K are known (or guessed from a
    field of view); needed to undo parallax for an object's top face, which sits `height` above the table.
  * pixel_to_plane(H, uv, K=None, z=0): back-project pixels onto the plane z (mm). Without K only z = 0 is
    possible, and a top face is located as if it lay on the table (parallax error grows with height and
    with distance from the image centre).
  * locate_on_plane(mask, H, K, height_mm): centre, yaw and footprint of a masked object's top face.

Capture (OpenCV, imported lazily) only opens a camera when HASAMI_REALCAM names one; otherwise it raises
SetupRequired. This module never opens the webcam on its own.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass

import numpy as np

from .locate import footprint_extent, footprint_yaw, is_round
from .segment import SetupRequired

A4_MM = (210.0, 297.0)
REALCAM_SETUP = ("Setup required: HASAMI_REALCAM (camera index, e.g. 0) and `pip install opencv-python` "
                 "(the camera is opened only when you ask)")


def a4_corners(landscape: bool = False) -> np.ndarray:
    """A4 sheet corners in mm, in click order: top-left, top-right, bottom-right, bottom-left."""
    w, h = (A4_MM[1], A4_MM[0]) if landscape else A4_MM
    return np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=float)


def _normaliser(p: np.ndarray) -> np.ndarray:
    c = p.mean(axis=0)
    d = np.mean(np.linalg.norm(p - c, axis=1)) or 1.0
    s = np.sqrt(2) / d
    return np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])


def homography(plane_pts, image_pts) -> np.ndarray:
    """H (3x3, H[2,2] = 1) with image ~ H @ [x, y, 1] for >= 4 point pairs (DLT, normalised)."""
    src, dst = np.asarray(plane_pts, float), np.asarray(image_pts, float)
    if src.shape != dst.shape or src.shape[0] < 4 or src.shape[1] != 2:
        raise ValueError("need at least 4 matching (x, y) / (u, v) pairs")
    ts, td = _normaliser(src), _normaliser(dst)
    s = (ts @ np.c_[src, np.ones(len(src))].T).T
    d = (td @ np.c_[dst, np.ones(len(dst))].T).T
    rows = []
    for (x, y, _), (u, v, _) in zip(s, d):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    _, sv, vt = np.linalg.svd(np.array(rows))
    if sv[-2] < 1e-10:
        raise ValueError("degenerate points (three or more on a line?)")
    h = np.linalg.inv(td) @ vt[-1].reshape(3, 3) @ ts
    return h / h[2, 2]


def apply_h(h: np.ndarray, pts) -> np.ndarray:
    p = np.c_[np.asarray(pts, float), np.ones(len(pts))] @ h.T
    return p[:, :2] / p[:, 2:3]


def intrinsics_from_fov(width: int, height: int, hfov_deg: float = 70.0) -> np.ndarray:
    """A pinhole K from a horizontal field of view (an assumption until the camera is calibrated)."""
    f = 0.5 * width / np.tan(np.radians(hfov_deg) / 2)
    return np.array([[f, 0, (width - 1) / 2], [0, f, (height - 1) / 2], [0, 0, 1.0]])


def pose_from_homography(h: np.ndarray, k: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(R, t) with pixel ~ K (R @ [x, y, z] + t) for plane points (mm) on z = 0, z = x cross y. Depending on
    the click order the camera may sit on the -z side; pixel_to_plane accounts for that."""
    m = np.linalg.inv(k) @ h
    lam = 1.0 / np.linalg.norm(m[:, 0])
    if m[2, 2] * lam < 0:  # the plane origin must be in front of the camera
        lam = -lam
    r1, r2, t = m[:, 0] * lam, m[:, 1] * lam, m[:, 2] * lam
    u, _, vt = np.linalg.svd(np.c_[r1, r2, np.cross(r1, r2)])  # nearest rotation
    r = u @ vt
    return r, t


def pixel_to_plane(h: np.ndarray, uv, k: np.ndarray | None = None, z: float = 0.0) -> np.ndarray:
    """Pixels -> (x, y) in mm on the plane at height z (needs K unless z = 0)."""
    uv = np.atleast_2d(np.asarray(uv, float))
    if z == 0.0 or k is None:
        if z != 0.0:
            raise ValueError("a height above the table needs the camera intrinsics K")
        return apply_h(np.linalg.inv(h), uv)
    r, t = pose_from_homography(h, k)
    c = -r.T @ t  # camera centre in plane coordinates
    z = z if c[2] > 0 else -z  # "above the table" is towards the camera
    rays = (r.T @ (np.linalg.inv(k) @ np.c_[uv, np.ones(len(uv))].T)).T
    s = (z - c[2]) / rays[:, 2]
    return (c + rays * s[:, None])[:, :2]


@dataclass
class PlaneTarget:
    x_mm: float
    y_mm: float
    yaw: float  # radians, folded into [-pi/4, pi/4)
    extent_mm: list[float]  # along yaw, across
    round: bool
    height_mm: float  # typed by the user (a webcam has no depth)
    parallax_corrected: bool

    def to_dict(self) -> dict:
        return asdict(self)


def locate_on_plane(mask: np.ndarray, h: np.ndarray, k: np.ndarray | None, height_mm: float) -> PlaneTarget:
    """Where a masked object's top face is on the table plane. With K the top face is intersected with the
    plane at `height_mm` (no parallax); without K it is treated as if on the table."""
    vs, us = np.nonzero(mask)
    if len(us) < 10:
        raise ValueError("mask too small to locate")
    use_k = k is not None and height_mm > 0
    xy = pixel_to_plane(h, np.c_[us, vs], k if use_k else None, height_mm if use_k else 0.0)
    c = xy.mean(axis=0)
    yaw = footprint_yaw(xy)
    pitch = float(np.median(np.linalg.norm(np.diff(pixel_to_plane(h, np.array([[us[0], vs[0]], [us[0] + 1, vs[0]]])),
                                                  axis=0), axis=1)))
    return PlaneTarget(float(c[0]), float(c[1]), yaw, footprint_extent(xy, yaw, pitch), is_round(xy), float(height_mm),
                       bool(use_k))


# ---- capture and marker detection (OpenCV, lazy) ----------------------------------------------------
def _cv2():
    try:
        import cv2  # type: ignore[import-not-found]
    except ImportError as e:
        raise SetupRequired(REALCAM_SETUP) from e
    return cv2


def grab_frame() -> np.ndarray:
    """One RGB frame from the camera HASAMI_REALCAM names. Raises SetupRequired if it is not set."""
    idx = os.environ.get("HASAMI_REALCAM")
    if not idx:
        raise SetupRequired(REALCAM_SETUP)
    cv2 = _cv2()
    cap = cv2.VideoCapture(int(idx) if idx.isdigit() else idx)
    try:
        for _ in range(5):  # let exposure settle
            ok, bgr = cap.read()
        if not ok:
            raise RuntimeError(f"camera {idx!r} returned no frame")
        return bgr[..., ::-1].copy()
    finally:
        cap.release()


def detect_aruco(rgb: np.ndarray, marker_mm: float, dictionary: str = "DICT_4X4_50") -> tuple[np.ndarray, np.ndarray]:
    """The first ArUco marker's corners: (plane points in mm, image points), for homography()."""
    cv2 = _cv2()
    aruco = cv2.aruco
    det = aruco.ArucoDetector(aruco.getPredefinedDictionary(getattr(aruco, dictionary)))
    corners, ids, _ = det.detectMarkers(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY))
    if ids is None or not len(ids):
        raise ValueError("no ArUco marker found")
    s = marker_mm
    return np.array([[0, 0], [s, 0], [s, s], [0, s]], float), corners[0].reshape(4, 2).astype(float)
