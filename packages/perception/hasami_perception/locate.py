"""Turn a 2D mask plus a depth image into a 3D target the arm can grasp."""

from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np


@dataclass
class Target:
    center: list[float]  # world x, y, z of the object's centre (m)
    top_z: float  # height of the top surface (m)
    yaw: float  # rotation about z of the footprint's principal axis, folded into [-pi/4, pi/4)
    pixels: int  # mask pixels that sit above the table
    centroid_px: list[float]  # u, v of those pixels

    def to_dict(self) -> dict:
        return asdict(self)


def mask_iou(a: np.ndarray, b: np.ndarray) -> float:
    union = np.logical_or(a, b).sum()
    return float(np.logical_and(a, b).sum() / union) if union else 0.0


def locate(frame, mask: np.ndarray, table_z: float = 0.0, min_height: float = 0.005) -> Target:
    """Back-project every mask pixel with its depth; keep the ones above the table.

    Shadows and table pixels a mask leaks onto sit at the table height, so the height filter
    removes them. The top surface is the highest band of points; the centre is under it at
    half the measured height (the objects stand on the table).
    """
    vs, us = np.nonzero(mask)
    if len(us) == 0:
        raise ValueError("empty mask")
    fx, fy, cx, cy = frame.intrinsics()
    z = frame.depth[vs, us].astype(np.float64)
    p_cam = np.stack([(us - cx) / fx * z, -(vs - cy) / fy * z, -z], axis=1)
    pts = frame.cam_pos + p_cam @ frame.cam_mat.T
    keep = pts[:, 2] > table_z + min_height
    if keep.sum() < 10:
        raise ValueError("mask has no pixels above the table (is it on the object?)")
    pts, us_k, vs_k = pts[keep], us[keep], vs[keep]
    top = float(np.percentile(pts[:, 2], 90))
    band = pts[pts[:, 2] > top - 0.004]
    xy = band[:, :2]
    c = xy.mean(axis=0)
    yaw = footprint_yaw(xy)
    return Target(center=[float(c[0]), float(c[1]), (top + table_z) / 2], top_z=top, yaw=yaw,
                  pixels=int(keep.sum()), centroid_px=[float(us_k.mean()), float(vs_k.mean())])


def footprint_yaw(xy: np.ndarray) -> float:
    """Yaw of a footprint, folded into [-pi/4, pi/4).

    Near-square footprints (isotropic covariance) use the fourth-order moment, which every point
    contributes to; elongated ones use the minimum-area bounding rectangle.
    """
    pts = xy - xy.mean(axis=0)
    ev = np.linalg.eigvalsh(np.cov(pts.T)) if len(pts) > 2 else np.array([1.0, 1.0])
    if ev[0] > 0 and ev[1] / ev[0] < 1.15:
        return square_yaw(pts)
    return min_area_rect_yaw(pts)


def square_yaw(xy: np.ndarray) -> float:
    """Yaw of a square footprint from its fourth-order moment, folded into [-pi/4, pi/4).

    A square's covariance is isotropic, so PCA has no axis, and on a pixel grid its minimum-area
    rectangle snaps towards the image axes at small yaws (about 3 degrees of error near 0).
    sum((x + iy)^4) points along a corner (4 * angle); every pixel of the footprint votes, so the
    staircase edges average out (max error 1.8 degrees over 60 randomised cubes, mean 0.2).
    """
    p = xy - xy.mean(axis=0)
    z = np.sum((p[:, 0] + 1j * p[:, 1]) ** 4)
    yaw = np.angle(z) / 4 - np.pi / 4
    return float((yaw + np.pi / 4) % (np.pi / 2) - np.pi / 4)


def min_area_rect_yaw(xy: np.ndarray) -> float:
    """Yaw of the minimum-area bounding rectangle of a footprint, folded into [-pi/4, pi/4).

    Sweep the rectangle angle over 90 degrees (coarse, then fine) and keep the tightest box.
    """
    pts = xy - xy.mean(axis=0)

    def area(th: float) -> float:
        c, s = np.cos(th), np.sin(th)
        r = pts @ np.array([[c, -s], [s, c]])
        return float(np.ptp(r[:, 0]) * np.ptp(r[:, 1]))

    coarse = np.radians(np.arange(-45, 45, 1.0))
    best = coarse[int(np.argmin([area(t) for t in coarse]))]
    fine = best + np.radians(np.arange(-1, 1.01, 0.05))
    best = fine[int(np.argmin([area(t) for t in fine]))]
    return float((best + np.pi / 4) % (np.pi / 2) - np.pi / 4)
