"""Find the asked-for item in one of the robot's camera frames: the first real step of "bring me the juice".

Three honest ways, tried in this order:
  1. text:   SAM 3.1 through the Meta Model API segments "juice carton" directly (needs MODEL_API_KEY)
  2. point:  the operator clicked the item (x, y): SAM 2.1 on device at that pixel
  3. colour: the request names a colour ("the orange carton"): the largest blob of that colour, then SAM 2.1 there
If none applies, FindError says what is missing instead of guessing. Nothing here moves the robot.
"""
from __future__ import annotations

import io
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw

from hasami_perception.colour import colour_mask, colour_word, components
from hasami_perception.segment import SetupRequired


class FindError(RuntimeError):
    pass


@dataclass
class Found:
    mask: np.ndarray
    centroid: tuple[float, float]           # (u, v) pixels
    bbox: tuple[int, int, int, int]         # u0, v0, u1, v1
    how: str                                # "sam-3.1 text" | "sam-2.1 point" | "colour + sam-2.1"
    pixels: int

    def summary(self) -> dict:
        return {"how": self.how, "centroid_px": [round(self.centroid[0], 1), round(self.centroid[1], 1)],
                "bbox_px": list(self.bbox), "pixels": self.pixels}


def _found(mask: np.ndarray, how: str, min_pixels: int) -> Found:
    mask = mask.astype(bool)
    n = int(mask.sum())
    if n < min_pixels:
        raise FindError(f"{how}: the mask has {n} pixels (< {min_pixels}); the item is not visible enough")
    vs, us = np.nonzero(mask)
    return Found(mask, (float(us.mean()), float(vs.mean())), (int(us.min()), int(vs.min()), int(us.max()), int(vs.max())),
                 how, n)


def find(rgb: np.ndarray, label: str, point: tuple[float, float] | None = None, sam2=None, sam3=None,
         min_pixels: int = 150) -> Found:
    """`sam2` / `sam3` are segmenters (hasami_perception.segment); they are created on demand when not given."""
    tries = []
    if sam3 is None:
        try:
            from hasami_perception.segment import MetaSam3
            sam3 = MetaSam3()
        except SetupRequired as e:
            tries.append(str(e))
    if sam3 is not None:
        return _found(sam3.segment(rgb, text=label), "sam-3.1 text", min_pixels)

    def sam2_at(p):
        nonlocal sam2
        if sam2 is None:
            from hasami_perception.segment import LocalSam2
            sam2 = LocalSam2()
        return sam2.segment(rgb, point=p)

    if point is not None:
        return _found(sam2_at(point), "sam-2.1 point", min_pixels)
    colour = colour_word(label)
    if colour is not None:
        blobs = components(colour_mask(rgb, colour), min_pixels=min_pixels)
        if not blobs:
            raise FindError(f"nothing {colour} big enough in the frame ({'; '.join(tries)})")
        vu = blobs[0].mean(axis=0)
        return _found(sam2_at((float(vu[1]), float(vu[0]))), "colour + sam-2.1", min_pixels)
    raise FindError(f"can't find {label!r} without one of: MODEL_API_KEY (SAM 3.1 text), a click on the item, or a "
                    f"colour in the request ({'; '.join(tries) or 'no SAM 3.1'})")


def annotate(rgb: np.ndarray, f: Found, label: str) -> bytes:
    """PNG of the frame with the mask outline, bounding box and centroid drawn on it."""
    img = Image.fromarray(rgb).convert("RGB")
    edge = f.mask & ~(np.roll(f.mask, 1, 0) & np.roll(f.mask, -1, 0) & np.roll(f.mask, 1, 1) & np.roll(f.mask, -1, 1))
    px = np.array(img)
    px[edge] = (255, 64, 64)
    img = Image.fromarray(px)
    d = ImageDraw.Draw(img)
    d.rectangle(f.bbox, outline=(255, 210, 0), width=2)
    u, v = f.centroid
    d.ellipse((u - 5, v - 5, u + 5, v + 5), outline=(255, 210, 0), width=2)
    d.text((f.bbox[0], max(0, f.bbox[1] - 14)), f"{label} ({f.how})", fill=(255, 210, 0))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def decode_jpeg(jpg: bytes) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(jpg)).convert("RGB"))
