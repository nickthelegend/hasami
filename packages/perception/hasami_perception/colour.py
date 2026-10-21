"""Colour from pixels: HSV colour names, "same colour as this mask", and connected blobs.

Image-only helpers used to re-find an object for a retry, to check a mask against the colour a planner
named, and by the simulated-click oracle. No ground truth here.
"""

from __future__ import annotations

from collections import deque

import numpy as np

# Hue ranges in degrees (lit and shaded faces of the scene's saturated colours), minimum saturation and
# value. Red wraps around 0.
HUES: dict[str, tuple[tuple[float, float], ...]] = {
    "red": ((0, 14), (340, 360)),
    "orange": ((15, 38),),
    "yellow": ((40, 65),),
    "green": ((90, 165),),
    "blue": ((205, 240),),
    "purple": ((262, 300),),
}
MIN_SAT = 0.45
MIN_VAL = 0.25


def rgb_to_hsv(rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """H in degrees [0, 360), S and V in [0, 1], for an (..., 3) uint8 or float array."""
    x = rgb.astype(np.float64)
    if x.max() > 1.0:
        x = x / 255.0
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx, mn = x.max(axis=-1), x.min(axis=-1)
    d = mx - mn
    h = np.zeros_like(mx)
    nz = d > 1e-9
    rm = nz & (mx == r)
    gm = nz & (mx == g) & ~rm
    bm = nz & ~rm & ~gm
    h[rm] = (60 * ((g[rm] - b[rm]) / d[rm])) % 360
    h[gm] = 60 * ((b[gm] - r[gm]) / d[gm]) + 120
    h[bm] = 60 * ((r[bm] - g[bm]) / d[bm]) + 240
    s = np.where(mx > 1e-9, d / np.maximum(mx, 1e-9), 0.0)
    return h, s, mx


def colour_word(label: str) -> str | None:
    """The colour named in a label ('the red cube' -> 'red'), if it is one we know."""
    words = label.lower().replace("-", " ").split()
    for w in words:
        if w in HUES:
            return w
    return None


def colour_mask(rgb: np.ndarray, name: str) -> np.ndarray:
    if name not in HUES:
        raise KeyError(f"unknown colour {name!r}; known: {sorted(HUES)}")
    h, s, v = rgb_to_hsv(rgb)
    m = np.zeros(h.shape, dtype=bool)
    for lo, hi in HUES[name]:
        m |= (h >= lo) & (h < hi)
    return m & (s >= MIN_SAT) & (v >= MIN_VAL)


def name_colour(rgb_pixels: np.ndarray) -> str | None:
    """The named colour most pixels of a region fall into (None if under half of them match any)."""
    px = rgb_pixels.reshape(-1, 1, 3)
    best, frac = None, 0.0
    for name in HUES:
        f = float(colour_mask(px, name).mean()) if len(px) else 0.0
        if f > frac:
            best, frac = name, f
    return best if frac >= 0.5 else None


def mean_colour(rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
    return rgb[mask].reshape(-1, 3).astype(np.float64).mean(axis=0) if mask.any() else np.zeros(3)


def similar_mask(rgb: np.ndarray, ref: np.ndarray, hue_tol: float = 18.0, min_sat: float = 0.35) -> np.ndarray:
    """Pixels whose hue is within `hue_tol` degrees of a reference colour (saturated pixels only, so the
    table and the grey containers do not match a coloured object)."""
    h, s, v = rgb_to_hsv(rgb)
    rh, rs, _ = rgb_to_hsv(np.asarray(ref, dtype=np.float64).reshape(1, 3) / (255.0 if np.max(ref) > 1 else 1.0))
    if float(rs[0]) < min_sat:
        raise ValueError("reference colour is not saturated enough to search by hue")
    dh = np.abs(((h - float(rh[0])) + 180) % 360 - 180)
    return (dh <= hue_tol) & (s >= min_sat) & (v >= MIN_VAL)


def components(mask: np.ndarray, min_pixels: int = 1) -> list[np.ndarray]:
    """4-connected components of a boolean mask, as (N, 2) arrays of (v, u), largest first."""
    h, w = mask.shape
    seen = np.zeros_like(mask, dtype=bool)
    out = []
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
        if len(comp) >= min_pixels:
            out.append(np.array(comp))
    out.sort(key=len, reverse=True)
    return out


def blob_near(mask: np.ndarray, near: tuple[float, float], max_dist_px: float = 80.0, min_pixels: int = 40,
              exclude: np.ndarray | None = None) -> tuple[float, float] | None:
    """Centre (u, v) of the blob of `mask` closest to pixel `near` (by its nearest pixel), within
    `max_dist_px`, ignoring pixels in `exclude`. None if there is none."""
    m = mask & ~exclude if exclude is not None else mask
    best, best_d = None, max_dist_px
    for comp in components(m, min_pixels):
        d = float(np.min(np.hypot(comp[:, 1] - near[0], comp[:, 0] - near[1])))
        if d <= best_d:
            best, best_d = comp, d
    if best is None:
        return None
    return float(np.median(best[:, 1])), float(np.median(best[:, 0]))
