"""Items on a shelf, seen from the front: a fridge shelf through the robot's head camera.

The overhead view the tabletop uses cannot see into a fridge, and a camera looking into it sees each item's
front face, not its top. So, from RGB + metric depth only:

  1. the shelf: the largest flat surface at a height the arm can reach (a histogram of every pixel's height),
     and its extent (where that surface is seen);
  2. what stands on it: pixels raised above the shelf, inside its extent (the side walls and the back wall
     stand on its edges, so a 12 mm margin drops them), split into connected blobs;
  3. each blob: SAM 2.1 prompted at its middle with its box (the boxed point prompt the tabletop uses to
     keep a mask from spreading), then measured: the visible front face gives the width across the view and
     the nearest surface; the centre is half a width behind that surface (exact for a cylinder and for a
     square carton face-on; a carton turned by 15 degrees shows a corner, still centred). Height is the top
     above the shelf, colour the named colour most mask pixels fall into.

Plain functions over a Frame and masks; nothing here reads the simulator's ground truth.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass

import numpy as np

from .colour import components, mean_colour, name_colour

REACH_Z = (0.03, 0.15)  # shelf heights the arm can work at (m above the plane the arm stands on)
WALL_MARGIN = 0.012  # raised points this close to the shelf's seen edge are walls, not items
MIN_ITEM_PIXELS = 150
MAX_ITEM_HEIGHT = 0.15


@dataclass
class Shelf:
    z: float  # height of the shelf's top surface (m)
    x: tuple[float, float]  # extent of the surface seen (m, 1st-99th percentile)
    y: tuple[float, float]
    pixels: int

    def to_dict(self) -> dict:
        return {"z": round(self.z, 4), "x": [round(v, 4) for v in self.x], "y": [round(v, 4) for v in self.y],
                "pixels": self.pixels}


@dataclass
class ShelfItem:
    center: list[float]  # world x, y, z of the item's centre (m)
    bottom_z: float  # the shelf under it
    top_z: float
    width: float  # across the line of sight (m)
    colour: str | None  # named colour of most mask pixels (None if no name fits half of them)
    rgb: list[float]  # mean colour of the mask
    pixels: int
    centroid_px: list[float]
    view: list[float]  # horizontal unit vector from the camera towards the item

    @property
    def height(self) -> float:
        return self.top_z - self.bottom_z

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(center=[round(v, 4) for v in self.center], bottom_z=round(self.bottom_z, 4), top_z=round(self.top_z, 4),
                 width_mm=round(self.width * 1000, 1), height_mm=round(self.height * 1000, 1),
                 rgb=[round(v, 1) for v in self.rgb], centroid_px=[round(v, 1) for v in self.centroid_px],
                 view=[round(v, 4) for v in self.view])
        del d["width"]
        return d


def world_points(frame) -> np.ndarray:
    """H x W x 3 world coordinates of every pixel, from its depth."""
    h, w = frame.depth.shape
    vs, us = np.mgrid[0:h, 0:w]
    fx, fy, cx, cy = frame.intrinsics()
    z = frame.depth.astype(np.float64)
    return frame.cam_pos + np.stack([(us - cx) / fx * z, -(vs - cy) / fy * z, -z], axis=-1) @ frame.cam_mat.T


def find_shelf(frame, z_range: tuple[float, float] = REACH_Z, pts: np.ndarray | None = None) -> Shelf:
    """The largest flat, level surface between z_range heights. ValueError if there is none."""
    pts = world_points(frame) if pts is None else pts
    z = pts[..., 2]
    sel = (z > z_range[0]) & (z < z_range[1])
    if sel.sum() < 500:
        raise ValueError(f"no surface between {z_range[0] * 1000:.0f} and {z_range[1] * 1000:.0f} mm in view")
    zz = z[sel]
    hist, edges = np.histogram(zz, bins=np.arange(z_range[0], z_range[1] + 0.001, 0.001))
    k = int(np.argmax(hist))
    near = np.abs(zz - (edges[k] + 0.0005)) < 0.003
    level = float(np.median(zz[near]))
    plane = np.abs(z - level) < 0.002
    n = int(plane.sum())
    if n < 2000:
        raise ValueError(f"no shelf: the flattest surface in reach ({level * 1000:.0f} mm) covers only {n} pixels")
    xs, ys = pts[..., 0][plane], pts[..., 1][plane]
    return Shelf(level, (float(np.percentile(xs, 1)), float(np.percentile(xs, 99))),
                 (float(np.percentile(ys, 1)), float(np.percentile(ys, 99))), n)


def raised_on_shelf(frame, shelf: Shelf, pts: np.ndarray | None = None, margin: float = WALL_MARGIN) -> np.ndarray:
    """Pixels standing on the shelf: 6-150 mm above it and inside its seen extent, less `margin` (walls)."""
    pts = world_points(frame) if pts is None else pts
    x, y, z = pts[..., 0], pts[..., 1], pts[..., 2]
    return ((z > shelf.z + 0.006) & (z < shelf.z + MAX_ITEM_HEIGHT)
            & (x > shelf.x[0] - 0.02) & (x < shelf.x[1] - margin)
            & (y > shelf.y[0] + margin) & (y < shelf.y[1] - margin))


def blobs_on_shelf(frame, shelf: Shelf, pts: np.ndarray | None = None) -> list[np.ndarray]:
    """Each separate thing standing on the shelf, as a boolean mask, left to right in the image."""
    raised = raised_on_shelf(frame, shelf, pts)
    out = []
    for comp in components(raised, MIN_ITEM_PIXELS):
        m = np.zeros(raised.shape, dtype=bool)
        m[comp[:, 0], comp[:, 1]] = True
        out.append(m)
    return sorted(out, key=lambda m: float(np.nonzero(m)[1].mean()))


def measure(frame, mask: np.ndarray, shelf: Shelf, pts: np.ndarray | None = None) -> ShelfItem:
    """An item's centre, width, height and colour from its mask, seen from the front (see the module doc)."""
    pts = world_points(frame) if pts is None else pts
    sel = mask & (pts[..., 2] > shelf.z + 0.005)
    if sel.sum() < 30:
        raise ValueError("mask has no pixels above the shelf (is it on the item?)")
    p = pts[sel]
    vs, us = np.nonzero(sel)
    cam = frame.cam_pos[:2]
    c0 = p[:, :2].mean(axis=0)
    view = (c0 - cam) / np.linalg.norm(c0 - cam)
    across_dir = np.array([-view[1], view[0]])
    along = p[:, :2] @ view
    # mask pixels on the silhouette can carry the depth of the wall behind: keep the item's own depth range
    near = along < np.percentile(along, 2) + 0.05
    p, along, vs, us = p[near], along[near], vs[near], us[near]
    across = p[:, :2] @ across_dir
    pitch = float(np.median(frame.depth[sel])) / frame.intrinsics()[0]  # metres per pixel on the item
    width = float(np.ptp(across) + pitch)
    front = float(np.percentile(along, 2))
    mid = 0.5 * float(across.min() + across.max())
    xy = view * (front + width / 2) + across_dir * mid
    top = float(np.percentile(p[:, 2], 99))
    return ShelfItem(center=[float(xy[0]), float(xy[1]), (top + shelf.z) / 2], bottom_z=shelf.z, top_z=top, width=width,
                     colour=name_colour(frame.rgb[sel]), rgb=[float(v) for v in mean_colour(frame.rgb, sel)],
                     pixels=int(sel.sum()), centroid_px=[float(us.mean()), float(vs.mean())],
                     view=[float(view[0]), float(view[1])])


def box_of(mask: np.ndarray, pad: float = 6.0) -> tuple[float, float, float, float]:
    vs, us = np.nonzero(mask)
    h, w = mask.shape
    return (max(0.0, us.min() - pad), max(0.0, vs.min() - pad), min(w - 1.0, us.max() + pad), min(h - 1.0, vs.max() + pad))


def survey(frame, segment: Callable[..., np.ndarray]) -> tuple[Shelf, list[dict]]:
    """Find the shelf and everything on it. `segment(point=(u, v), box=(x1, y1, x2, y2))` is SAM 2.1. Each
    entry: the blob's prompt point and box, SAM's mask, its measurement (or why it could not be measured)."""
    pts = world_points(frame)
    shelf = find_shelf(frame, pts=pts)
    found = []
    for blob in blobs_on_shelf(frame, shelf, pts):
        vs, us = np.nonzero(blob)
        # the prompt: the blob pixel nearest its median (a concave blob's median can fall outside it)
        mu, mv = float(np.median(us)), float(np.median(vs))
        k = int(np.argmin((us - mu) ** 2 + (vs - mv) ** 2))
        point = (float(us[k]), float(vs[k]))
        box = box_of(blob)
        mask = segment(point=point, box=box)
        entry: dict = {"point": [round(point[0], 1), round(point[1], 1)], "box": [round(v, 1) for v in box],
                       "blob_pixels": int(blob.sum()), "mask": mask}
        try:
            entry["item"] = measure(frame, mask, shelf, pts)
        except ValueError as e:
            entry["error"] = str(e)
        found.append(entry)
    return shelf, found


__all__ = ["Shelf", "ShelfItem", "blobs_on_shelf", "box_of", "find_shelf", "measure", "raised_on_shelf", "survey",
           "world_points"]
