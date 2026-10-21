"""Grounding a planner's words in pixels: from "red cube" plus a hint pixel to a mask we trust.

Muse names each object and gives a pixel as a hint. A language model's pixel can land beside the object,
on its shadow, or on a neighbour, so the hint is checked, not trusted:

  1. SAM at the hint. Accept the mask if it passes validate_mask (sensible size, near the hint, raised
     off the table for an object, and the colour the label names, if it names one).
  2. Otherwise look for the named colour near the hint (HSV blobs) and prompt SAM at that blob.
  3. Otherwise prompt SAM at the nearest raised blob (from depth) near the hint.

Each try is recorded. If none passes, GroundingError carries every try and why it was rejected; the
caller fails the step loudly instead of guessing. The segmenter is passed in (SAM 2.1 in the app), so
the logic here is plain functions over arrays and is tested on synthetic images.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from .colour import blob_near, colour_mask, colour_word, name_colour

CONTAINER_WORDS = ("tray", "bin", "box", "basket", "container")
SURFACE_WORDS = ("table", "desk", "surface", "floor")
SegmentFn = Callable[[tuple[float, float]], np.ndarray]


class GroundingError(ValueError):
    def __init__(self, message: str, tries: list[dict]):
        super().__init__(message)
        self.tries = tries


@dataclass
class Grounded:
    mask: np.ndarray
    point: tuple[float, float]
    source: str  # "hint", "colour search", "raised blob"
    tries: list[dict] = field(default_factory=list)


def is_container(label: str) -> bool:
    words = label.lower().replace("-", " ").split()
    return any(w in CONTAINER_WORDS for w in words) and not any(w in ("cube", "block", "cylinder") for w in words)


def is_surface(label: str) -> bool:
    """'the table', 'a free spot on the desk': a place on the bare surface, not a thing to segment."""
    words = label.lower().replace("-", " ").split()
    return any(w in SURFACE_WORDS for w in words) and not is_container(label) and \
        not any(w in ("cube", "block", "cylinder") for w in words)


def validate_mask(mask: np.ndarray, label: str, rgb: np.ndarray, heights: np.ndarray | None,
                  hint: tuple[float, float] | None = None, near_px: float = 40.0) -> list[str]:
    """Reasons to reject a mask for `label` (empty list = accept)."""
    reasons: list[str] = []
    n = int(mask.sum())
    container = is_container(label)
    lo, hi = (400, 60000) if container else (40, 8000)
    if n == 0:
        return ["empty mask"]
    if not lo <= n <= hi:
        reasons.append(f"{n} pixels, expected {lo}-{hi} for {'a container' if container else 'an object'}")
    vs, us = np.nonzero(mask)
    if hint is not None:
        u, v = int(round(hint[0])), int(round(hint[1]))
        inside = 0 <= v < mask.shape[0] and 0 <= u < mask.shape[1] and bool(mask[v, u])
        d = float(np.min(np.hypot(us - hint[0], vs - hint[1])))
        if not inside and d > near_px:
            reasons.append(f"mask is {d:.0f} px from the hint")
    if heights is not None and not container:
        raised = float((heights[mask] > 0.005).mean())
        if raised < 0.3:
            reasons.append(f"only {raised:.0%} of the mask is above the table (shadow or table?)")
    word = colour_word(label)
    if word and not container:
        px = rgb[mask]
        if heights is not None:
            top = heights[mask] > 0.005
            px = px[top] if top.any() else px
        seen = name_colour(px)
        if seen != word:
            reasons.append(f"mask looks {seen or 'uncoloured'}, label says {word}")
    return reasons


def raised_blob_near(heights: np.ndarray, hint: tuple[float, float], max_dist_px: float = 80.0,
                     exclude: np.ndarray | None = None) -> tuple[float, float] | None:
    return blob_near(heights > 0.005, hint, max_dist_px=max_dist_px, min_pixels=40, exclude=exclude)


def ground(label: str, hint: tuple[float, float], rgb: np.ndarray, heights: np.ndarray | None,
           segment: SegmentFn, search_px: float = 120.0, exclude: np.ndarray | None = None) -> Grounded:
    """Find `label` near `hint`: SAM at the hint, then at the named colour's nearest blob, then at the
    nearest raised blob. Raises GroundingError listing every try if none passes validate_mask.
    `exclude` masks pixels (e.g. the parked arm) out of the searches."""
    tries: list[dict] = []
    seen: set[tuple[int, int]] = set()
    if is_surface(label):  # nothing to find: searching for a raised blob would land on another object
        tries.append({"source": "surface", "point": [round(hint[0], 1), round(hint[1], 1)],
                      "rejected": ["a spot on the bare table: the hint is used as is"]})
        raise GroundingError(f"{label!r} is the bare table: the hint is the place", tries)

    def attempt(point: tuple[float, float] | None, source: str) -> Grounded | None:
        if point is None:
            tries.append({"source": source, "point": None, "rejected": ["nothing found near the hint"]})
            return None
        key = (int(round(point[0])), int(round(point[1])))
        if key in seen:
            return None
        seen.add(key)
        mask = segment(point)
        reasons = validate_mask(mask, label, rgb, heights, hint=point)
        tries.append({"source": source, "point": [round(point[0], 1), round(point[1], 1)], "rejected": reasons,
                      "pixels": int(mask.sum())})
        return None if reasons else Grounded(mask, point, source, tries)

    got = attempt(hint, "hint")
    if got:
        return got
    word = colour_word(label)
    if word and not is_container(label):
        got = attempt(blob_near(colour_mask(rgb, word), hint, max_dist_px=search_px, exclude=exclude), "colour search")
        if got:
            return got
    if heights is not None and not is_container(label):
        got = attempt(raised_blob_near(heights, hint, max_dist_px=search_px, exclude=exclude), "raised blob")
        if got:
            return got
    raise GroundingError(f"could not ground {label!r} near pixel ({hint[0]:.0f}, {hint[1]:.0f}): "
                         + "; ".join(f"{t['source']}: {', '.join(t['rejected']) or 'ok'}" for t in tries), tries)
