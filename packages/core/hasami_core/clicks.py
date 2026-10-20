"""Simulated clicks: where a person looking at the overhead RGB image would click.

Used by the demo script and the tests in place of a human. It sees only the rendered RGB pixels
(colour thresholds, then the largest connected blob), never the simulator's ground truth, so a
click can be off-centre or miss exactly as a hurried person's might.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# RGB rules for the scene's objects as rendered by the overhead camera (lit, with shadows).
_RULES = {
    "red cube": lambda r, g, b: (r > 170) & (g < 120) & (b < 120),
    "green cube": lambda r, g, b: (g > 160) & (r < 150) & (b < 190) & (g - r > 60),
    "blue cube": lambda r, g, b: (b > 170) & (r < 160) & (g < 200) & (b - r > 60),
    # The tray is dark slate; the arm's dark servos also pass this test, hence the blob filter.
    "tray": lambda r, g, b: (r < 120) & (g < 120) & (b < 135) & (b >= r),
}


def largest_blob(mask: np.ndarray) -> np.ndarray:
    """Largest 4-connected component of a boolean mask (plain BFS; masks here are small)."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    best: list[tuple[int, int]] = []
    for v0, u0 in zip(*np.nonzero(mask)):
        if seen[v0, u0]:
            continue
        comp = []
        q = deque([(int(v0), int(u0))])
        seen[v0, u0] = True
        while q:
            v, u = q.popleft()
            comp.append((v, u))
            for dv, du in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                a, b = v + dv, u + du
                if 0 <= a < h and 0 <= b < w and mask[a, b] and not seen[a, b]:
                    seen[a, b] = True
                    q.append((a, b))
        if len(comp) > len(best):
            best = comp
    out = np.zeros_like(mask, dtype=bool)
    if best:
        vs, us = zip(*best)
        out[list(vs), list(us)] = True
    return out


def colour_blob(rgb: np.ndarray, label: str) -> np.ndarray:
    if label not in _RULES:
        raise KeyError(f"no colour rule for {label!r}; known: {sorted(_RULES)}")
    r, g, b = (rgb[..., i].astype(int) for i in range(3))
    return largest_blob(_RULES[label](r, g, b))


def simulated_click(rgb: np.ndarray, label: str, min_pixels: int = 40) -> tuple[float, float]:
    """The pixel a person would click for `label`: the middle of its visible blob."""
    blob = colour_blob(rgb, label)
    if blob.sum() < min_pixels:
        raise LookupError(f"cannot see the {label} in the image ({int(blob.sum())} matching pixels)")
    vs, us = np.nonzero(blob)
    return float(np.median(us)), float(np.median(vs))


def simulated_drop_clicks(rgb: np.ndarray, n: int, container: str = "tray") -> list[tuple[float, float]]:
    """n separate spots inside the container, as a person would click to keep objects apart:
    the container's quadrant centres (in the image), in reading order."""
    blob = colour_blob(rgb, container)
    if blob.sum() < 200:
        raise LookupError(f"cannot see the {container} in the image")
    vs, us = np.nonzero(blob)
    u0, u1, v0, v1 = us.min(), us.max(), vs.min(), vs.max()
    spots = [(0.3, 0.3), (0.7, 0.3), (0.3, 0.7), (0.7, 0.7)]
    if n > len(spots):
        raise ValueError(f"at most {len(spots)} separate drop spots in the {container}")
    return [(float(u0 + a * (u1 - u0)), float(v0 + b * (v1 - v0))) for a, b in spots[:n]]
