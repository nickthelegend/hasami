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
    """Yaw of the minimum-area bounding rectangle of a footprint, folded into [-pi/4, pi/4).

    PCA cannot do this: a square's covariance is isotropic, so it has no principal axis.
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
