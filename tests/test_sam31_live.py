"""Explicit opt-in live SAM 3.1 smoke test (never part of offline checks)."""

import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hasami_perception.segment import MetaSam3


@pytest.mark.skipif(not (os.environ.get('MODEL_API_KEY') and os.environ.get('HASAMI_LIVE_META') == '1'),
                    reason='requires MODEL_API_KEY and HASAMI_LIVE_META=1')
def test_live_juice_carton_mask():
    fixture = Path(__file__).parent / 'fixtures' / 'sam31' / 'fridge.png'
    rgb = np.asarray(Image.open(fixture).convert('RGB'))
    mask = MetaSam3().segment(rgb, text='juice carton')
    assert mask.shape == rgb.shape[:2] and mask.dtype == bool
    carton_box = np.zeros(mask.shape, dtype=bool)
    carton_box[78:184, 492:552] = True
    assert (mask & carton_box).sum() / max(1, mask.sum()) > 0.7
