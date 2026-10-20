"""Perception: SAM masks fused with depth into 3D grasp targets.

Two segmenters share one interface:
  * LocalSam2  - SAM 2.1 (hiera-tiny, open weights) on this machine, prompted with a point.
  * MetaSam3   - SAM 3.1 on the Meta Model API, prompted with text. Needs MODEL_API_KEY.
Neither is mocked. Without a key MetaSam3 raises SetupRequired instead of guessing.
"""

from .locate import Target, locate, mask_iou
from .segment import LocalSam2, MetaSam3, Segmenter, SetupRequired

__all__ = ["LocalSam2", "MetaSam3", "Segmenter", "SetupRequired", "Target", "locate", "mask_iou"]
