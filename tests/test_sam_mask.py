"""Offline conformance checks against saved SAM 3.1 Responses API output."""

import json
import re
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from hasami_perception.sam_mask import InvalidSamMask, decode_raster, encode_raster, pack, unpack
from hasami_perception.segment import _output_text, parse_sam_output

FIXTURES = Path(__file__).parent / 'fixtures' / 'sam31'
HW = (470, 836)


def response(name):
    return _output_text(json.loads((FIXTURES / name).read_text()))


def test_every_saved_object_decodes_with_canonical_shape_and_binary_values():
    objects = 0
    for name, expected in [('carton.json', 1), ('wheels.json', 3), ('empty.json', 0)]:
        text = response(name)
        data = re.findall(r'data=(\d+),(\d+),([!~][^|]+)', text)
        assert len(data) == expected
        for h, w, payload in data:
            raster = decode_raster(payload, int(w), int(h))
            assert raster.shape == (int(h), int(w))
            assert raster.dtype == np.uint8
            assert set(np.unique(raster)).issubset({0, 1})
            assert encode_raster(raster) == payload
            objects += 1
        assert len(parse_sam_output(text, HW)) == expected
    assert objects == 4


def test_carton_mask_occupies_plausible_fraction_and_stays_in_inclusive_box():
    item = parse_sam_output(response('carton.json'), HW)[0]
    mask = item['mask']
    assert item['box'] == (492, 78, 551, 183)
    assert mask.shape == HW and mask.dtype == bool
    assert 0.30 < mask[78:184, 492:552].mean() < 0.98
    outside = mask.copy()
    outside[78:184, 492:552] = False
    assert not outside.any()


def test_carton_mask_selects_orange_pixels_better_than_box_complement():
    rgb = np.asarray(Image.open(FIXTURES / 'fridge.png').convert('RGB')).astype(int)
    mask = parse_sam_output(response('carton.json'), HW)[0]['mask']
    box = np.zeros(HW, dtype=bool)
    box[78:184, 492:552] = True
    orange = ((rgb[:, :, 0] > rgb[:, :, 1] * 1.2) &
              (rgb[:, :, 1] > rgb[:, :, 2] * 1.2) &
              (rgb[:, :, 0] > 110))
    assert orange[mask].mean() > orange[box & ~mask].mean() + 0.25
    assert (orange & mask).sum() / (orange & box).sum() > 0.90


def test_corrupted_payload_raises_readable_error():
    text = response('carton.json')
    corrupt = re.sub(r'(data=106,60,!)([^|]+)', r'\1"\2', text)
    with pytest.raises(ValueError, match='Malformed SAM 3.1 mask payload'):
        parse_sam_output(corrupt, HW)


def test_round_trip_synthetic_raster_and_base85_tail():
    raster = np.zeros((19, 27), dtype=np.uint8)
    raster[2:17, 4:23] = 1
    raster[8:11, 9:14] = 0
    payload = encode_raster(raster)
    assert payload == '!!!!!5Crl[^CuyRQx]9rg$:zkk!!!'  # @meta-sam/parser 0.1.0
    assert np.array_equal(decode_raster(payload, 27, 19), raster)
    for length in range(1, 13):
        raw = bytes(range(length))
        assert unpack(pack(raw)) == raw


def test_scaled_mask_and_offsets_clip_to_target_frame():
    payload = encode_raster(np.ones((3, 3), dtype=np.uint8))
    text = (f'<0f>0<|box;x1=3;y1=3;x2=5;y2=5;w=6;h=6|>'
            f'<|mask;x=1;y=1;data=3,3,{payload}|>')
    item = parse_sam_output(text, (12, 12))[0]
    assert item['box'] == (6, 6, 10, 10)
    expected = np.zeros((12, 12), dtype=bool)
    expected[8:12, 8:12] = True
    assert np.array_equal(item['mask'], expected)


def test_lossless_has_explicit_error():
    with pytest.raises(NotImplementedError, match='lossless'):
        decode_raster('~abc', 1, 1)


def test_noncanonical_payload_rejected():
    with pytest.raises(InvalidSamMask, match='canonical|length|character'):
        unpack('!!!!!a')
