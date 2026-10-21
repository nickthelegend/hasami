"""Is a located target an object the arm could pick? Checked before any move.

On noisy depth a mask can leak onto the table (SAM picks the whole-table hypothesis at a pixel the object
has left). A few hundred table pixels then sit above the 5 mm height cut by noise alone, and the "target"
is the table's middle, 594 mm wide and 6 mm tall. The arm used to be sent there. `check_target` turns that
into reasons to reject:

  * footprint: the narrow side must fit between the open jaws, and the long side must be within the size
    range of the objects the scene declares (any face down, or leaning on an edge, so a tipped object
    still passes);
  * height: the top must be at least most of the smallest object's smallest side above the table, and no
    higher than two of the largest objects stacked;
  * centre: inside the workspace (the reach annulus, or inside a container's footprint);
  * mask: touching the frame's edge means it leaked past every object (all objects are inside the frame).

Plain functions over a Target and a mask; the priors come from the scene's object list (sizes the planner
is told), never from where the objects are.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .locate import Target

# The reach annulus of a top-down grasp (hasami_sim.scene.REACH_R, measured), widened by a margin.
REACH_R = (0.16, 0.272)
REACH_MARGIN = 0.015
EDGE_PX = 5  # mask pixels on the frame border that count as touching it
SIZE_TOL = 0.008  # measured footprints come out up to a few mm larger than the object (pixel pitch, noise)
LEAN = 1.42  # a side leaning on its edge shows up to its diagonal from above


@dataclass
class Priors:
    """What a pickable target can look like in this scene."""

    grip_max: float  # widest object the open (pre-grasp) jaws take, m
    dim_min: float  # smallest side of any object in the scene, m
    dim_max: float  # largest side of any object in the scene, m
    reach: tuple[float, float] = REACH_R  # radius from the base axis, m
    containers: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y, half) footprints

    def to_dict(self) -> dict:
        return {"grip_max_mm": round(self.grip_max * 1000, 1), "dim_min_mm": round(self.dim_min * 1000, 1),
                "dim_max_mm": round(self.dim_max * 1000, 1), "reach_m": [round(r, 3) for r in self.reach]}


def priors_for(dims: list[tuple[float, float, float]], grip_max: float,
               containers: list[tuple[float, float, float]] | None = None) -> Priors:
    """Priors from the sizes of the scene's objects (full x, y, z in m) and the jaws' pre-grasp opening."""
    if not dims:
        raise ValueError("no object sizes: cannot judge a target")
    flat = [float(v) for d in dims for v in d]
    return Priors(grip_max=float(grip_max), dim_min=min(flat), dim_max=max(flat), containers=list(containers or []))


def edge_pixels(mask: np.ndarray) -> int:
    return int(mask[0].sum() + mask[-1].sum() + mask[1:-1, 0].sum() + mask[1:-1, -1].sum())


def in_workspace(xy, priors: Priors) -> bool:
    r = float(np.hypot(xy[0], xy[1]))
    if priors.reach[0] - REACH_MARGIN <= r <= priors.reach[1] + REACH_MARGIN:
        return True
    return any(abs(xy[0] - cx) <= h and abs(xy[1] - cy) <= h for cx, cy, h in priors.containers)


def check_target(target: Target, mask: np.ndarray | None, priors: Priors) -> list[str]:
    """Reasons the target is not a pickable object (empty list = plausible)."""
    reasons: list[str] = []
    e = sorted(target.extent or [0.0, 0.0])
    narrow, long = e[0], e[1]
    if narrow > priors.grip_max + SIZE_TOL / 2:
        reasons.append(f"footprint {narrow * 1000:.0f} mm across its narrow side; the open jaws take "
                       f"{priors.grip_max * 1000:.0f} mm")
    if long > LEAN * priors.dim_max + SIZE_TOL:
        reasons.append(f"footprint {long * 1000:.0f} mm long; the largest object here is "
                       f"{priors.dim_max * 1000:.0f} mm ({LEAN * priors.dim_max * 1000:.0f} mm leaning on an edge)")
    if long < 0.5 * priors.dim_min:
        reasons.append(f"footprint {long * 1000:.0f} mm long; the smallest object here is "
                       f"{priors.dim_min * 1000:.0f} mm")
    lo, hi = 0.6 * priors.dim_min, 2 * priors.dim_max + SIZE_TOL
    if not lo <= target.top_z <= hi:
        reasons.append(f"top {target.top_z * 1000:.0f} mm above the table; objects here stand "
                       f"{lo * 1000:.0f}-{hi * 1000:.0f} mm")
    if not in_workspace(target.center[:2], priors):
        r = float(np.hypot(*target.center[:2]))
        reasons.append(f"centre {r * 1000:.0f} mm from the base, outside the workspace")
    if mask is not None:
        n = edge_pixels(mask)
        if n >= EDGE_PX:
            reasons.append(f"mask touches the frame edge ({n} px): it leaked past the objects")
    return reasons


def grow_box(mask: np.ndarray, pad: float = 8.0) -> tuple[float, float, float, float] | None:
    """Bounding box (x1, y1, x2, y2) of a mask, grown by `pad` pixels and clipped to the frame."""
    vs, us = np.nonzero(mask)
    if len(us) == 0:
        return None
    h, w = mask.shape
    return (max(0.0, us.min() - pad), max(0.0, vs.min() - pad), min(w - 1.0, us.max() + pad),
            min(h - 1.0, vs.max() + pad))


def box_around(point: tuple[float, float], half_px: float, shape: tuple[int, int]) -> tuple[float, float, float, float]:
    h, w = shape
    return (max(0.0, point[0] - half_px), max(0.0, point[1] - half_px), min(w - 1.0, point[0] + half_px),
            min(h - 1.0, point[1] + half_px))


__all__ = ["Priors", "box_around", "check_target", "edge_pixels", "grow_box", "in_workspace", "priors_for"]
