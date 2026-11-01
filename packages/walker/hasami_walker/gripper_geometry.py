"""Robonine parallel gripper dimensions and scale-aware image width estimate.

Specs: https://github.com/roboninecom/SO-ARM100-101-102-Parallel-Gripper/blob/main/docs/specifications.md
The pinion conversion is an estimate from the printed gear, not a substitute for jaw-gap calibration.
"""
from __future__ import annotations

import math

STROKE_MM = 84.0
MAX_SPEED_MM_S = 14.0
MASS_G = 170.0
MOUNT_FLANGE_DIAMETER_MM = 20.0
MOUNT_BORE_DIAMETER_MM = 7.0
MOUNT_HOLES = 4
MOUNT_HOLE_DIAMETER_MM = 3.2
MIN_GRIP_GAP_MM = 4.0       # conservative software policy; not a Robonine specification
MAX_SQUEEZE_MM = 4.0        # software cap because the PWM servo cannot limit force
PINION_PITCH_RADIUS_MM_EST = 9.0
GAP_MM_PER_SERVO_DEG_EST = 2 * math.pi * PINION_PITCH_RADIUS_MM_EST / 180


def estimate_width(mask_width_px: int, *, mm_per_pixel: float | None = None, distance_mm: float | None = None,
                   focal_px: float | None = None, open_gap_mm: float | None = None) -> dict:
    """Project a mask's horizontal pixel span to width at a known object distance or measured local scale.

    Distance requires calibrated horizontal focal length in pixels. The result assumes the item's width is
    roughly parallel to the image plane; perspective/occlusion can make it inaccurate. No scale means unknown.
    """
    if mask_width_px <= 0 or (open_gap_mm is not None and (not math.isfinite(open_gap_mm) or open_gap_mm <= 0)):
        raise ValueError("mask width and any supplied open gap must be positive")
    if mm_per_pixel is not None and (distance_mm is not None or focal_px is not None):
        raise ValueError("use either mm_per_pixel or distance_mm with focal_px")
    if mm_per_pixel is None and (distance_mm is None) != (focal_px is None):
        raise ValueError("distance_mm and focal_px must be supplied together")
    if focal_px is not None and (not math.isfinite(focal_px) or focal_px <= 0):
        raise ValueError("horizontal focal length must be finite and positive")
    scale = mm_per_pixel if mm_per_pixel is not None else (distance_mm / focal_px if distance_mm is not None else None)
    if scale is not None and (not math.isfinite(scale) or scale <= 0):
        raise ValueError("image scale must be finite and positive")
    width = mask_width_px * scale if scale is not None else None
    return {"mask_width_px": mask_width_px, "width_mm": round(width, 1) if width is not None else None,
            "fits_stroke": width <= STROKE_MM if width is not None else None,
            "fits_open_gap": width <= open_gap_mm if width is not None and open_gap_mm is not None else None,
            "open_gap_mm": open_gap_mm, "scale": "known" if scale is not None else "unknown"}
