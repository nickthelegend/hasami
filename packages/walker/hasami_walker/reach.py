"""A taught pixel-to-arm grid for one fixed shelf and body-mounted camera.

Telemetry is the runtime's commanded position, never measured servo position. Move the
robot or camera, change shelf height, or change resolution: teach a new grid.
"""
from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np


class ReachError(ValueError):
    pass


def grid_path(name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ReachError("grid name must contain only letters, numbers, _ or -")
    return Path(os.environ.get("HASAMI_GRIDS_DIR", Path.home() / ".hasami" / "grids")) / f"{name}.json"


def save_grid(name: str, grid: dict) -> Path:
    validate_grid(grid)
    path = grid_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(grid, indent=2, sort_keys=True))
    os.replace(tmp, path)
    return path


def load_grid(name: str) -> dict:
    path = grid_path(name)
    if not path.exists():
        raise ReachError(f"Setup required: no taught grid {name!r}; run teach-grid {name}")
    grid = json.loads(path.read_text())
    validate_grid(grid)
    return grid


def validate_grid(grid: dict) -> None:
    if grid.get("version") != 1:
        raise ReachError("grid version is not supported (expected 1)")
    size = grid.get("resolution")
    if not isinstance(size, list) or len(size) != 2 or min(size) <= 0:
        raise ReachError("grid needs a positive camera resolution [width, height]")
    spots = grid.get("spots", [])
    if len(spots) < 3 or len(spots) > 12:
        raise ReachError("grid needs 3–12 taught spots")
    points = []
    for s in spots:
        for key, n in (("pixel", 2), ("pre_grasp", 6), ("grasp", 6), ("lift", 6), ("jaw_region", 4)):
            v = s.get(key)
            if not isinstance(v, list) or len(v) != n or not all(isinstance(x, (int, float)) and math.isfinite(x) for x in v):
                raise ReachError(f"each grid spot needs finite {key} ({n} values)")
        if s.get("mm_per_px", 0) <= 0:
            raise ReachError("each grid spot needs a positive mm_per_px")
        if not (0 <= s["pixel"][0] < size[0] and 0 <= s["pixel"][1] < size[1]):
            raise ReachError("grid spot is outside its camera resolution")
        x0, y0, x1, y1 = s["jaw_region"]
        if not (0 <= x0 < x1 < size[0] and 0 <= y0 < y1 < size[1]):
            raise ReachError("jaw region must be an in-frame x0,y0,x1,y1 rectangle")
        points.append(s["pixel"])
    p = np.asarray(points, dtype=float)
    if len({tuple(x) for x in points}) != len(points) or np.linalg.matrix_rank(p[1:] - p[0]) < 2:
        raise ReachError("degenerate grid: spots must be distinct and span an area")


@dataclass
class ReachPlan:
    pre_grasp: list[float]
    grasp: list[float]
    lift: list[float]
    mm_per_px: float
    jaw_region: list[float]
    triangle: list[int]

    def width_mm(self, width_px: float) -> float:
        return float(width_px * self.mm_per_px)


def _circumcircle(a, b, c):
    m = 2 * np.array([b - a, c - a])
    rhs = np.array([np.dot(b, b) - np.dot(a, a), np.dot(c, c) - np.dot(a, a)])
    if abs(np.linalg.det(m)) < 1e-8:
        return None
    center = np.linalg.solve(m, rhs)
    return center, float(np.sum((center - a) ** 2))


def plan_reach(grid: dict, pixel: tuple[float, float], *, resolution: tuple[int, int] | None = None) -> ReachPlan:
    """Delaunay interpolation for at most 12 spots; refuse outside the taught convex hull."""
    validate_grid(grid)
    if resolution is not None and list(resolution) != grid["resolution"]:
        raise ReachError(f"camera resolution changed: taught {grid['resolution']}, now {list(resolution)}")
    from itertools import combinations

    spots = grid["spots"]
    p = np.asarray([s["pixel"] for s in spots], dtype=float)
    target = np.asarray(pixel, dtype=float)
    if not np.isfinite(target).all():
        raise ReachError("target pixel must be finite")
    chosen = None
    for ids in combinations(range(len(p)), 3):
        a, b, c = p[list(ids)]
        circle = _circumcircle(a, b, c)
        if circle is None:
            continue
        center, radius2 = circle
        if any(np.sum((p[j] - center) ** 2) < radius2 - 1e-6 for j in range(len(p)) if j not in ids):
            continue
        mat = np.column_stack((b - a, c - a))
        uv = np.linalg.solve(mat, target - a)
        weights = np.array([1 - uv.sum(), uv[0], uv[1]])
        if min(weights) >= -1e-7:
            chosen = ids, np.maximum(weights, 0) / np.maximum(weights, 0).sum()
            break
    if chosen is None:
        raise ReachError(f"pixel {tuple(pixel)} is outside the taught grid hull")
    ids, weights = chosen

    def blend(key):
        return sum(weights[i] * np.asarray(spots[j][key], dtype=float) for i, j in enumerate(ids))

    return ReachPlan(*(blend(key).tolist() for key in ("pre_grasp", "grasp", "lift")),
                     float(blend("mm_per_px")), blend("jaw_region").tolist(), list(ids))
