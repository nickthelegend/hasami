"""Offline pixel provenance checks; never constructs a simulator or loads a model."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / 'packages/landing/assets'
RUNS = ROOT / 'deployments/runs'
kind = sys.argv[1]
checks = []

def encoded_source(src, asset, crop=None):
    image = Image.open(src).convert('RGB')
    if crop:
        image = image.crop(crop)
    buf = io.BytesIO()
    image.save(buf, 'WEBP', quality=90, method=6)
    expected = hashlib.sha256(buf.getvalue()).hexdigest()
    actual = hashlib.sha256(asset.read_bytes()).hexdigest()
    assert actual == expected, asset.name
    checks.append({'asset': str(asset.relative_to(ROOT)), 'source': str(src.relative_to(ROOT)), 'crop': crop, 'sha256': actual, 'reencoded_source_sha256': expected})

if kind == 'hero':
    encoded_source(RUNS / '20261021T053112874Z/s0a1-frame.png', ASSETS / 'frame-hero.webp')
    spec = importlib.util.spec_from_file_location('landing_assets', ROOT / 'scripts/landing_assets.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ['index.html', 'evidence.html']:
        html = (ROOT / 'packages/landing' / name).read_text()
        for tag in ['pick', 'place']:
            path = module.outline(module.mask('20261021T053112874Z', 's0a1-' + tag))
            assert 'd="' + path + '"' in html
            checks.append({'page': name, 'mask': 's0a1-' + tag, 'exact_recorded_outline': True})
elif kind.startswith('replay-'):
    tag = kind.removeprefix('replay-')
    encoded_source(RUNS / ('20261021T053203432Z/' + tag + '-after.png'), ASSETS / (kind + '.webp'), (120, 190, 420, 340))
elif kind == 'fetch':
    import numpy as np
    asset = Image.open(ASSETS / 'fridge-fetch.webp').convert('RGB')
    folder = RUNS / '20261026T111544704Z'
    frame = np.asarray(Image.open(folder / 's0a1-frame.png').convert('RGB')).astype(np.float32)
    mask = np.asarray(Image.open(folder / 's0a1-pick.png').convert('L')) > 127
    tint = np.array([56, 242, 196], np.float32)
    frame[mask] = .45 * frame[mask] + .55 * tint
    edge = mask & ~(np.roll(mask, 1, 0) & np.roll(mask, -1, 0) & np.roll(mask, 1, 1) & np.roll(mask, -1, 1))
    frame[edge] = tint
    frames = [Image.fromarray(frame.clip(0,255).astype(np.uint8)), Image.open(folder / 's0a1-scene_after.png').convert('RGB'), Image.open(folder / 's0a1-basket_after.png').convert('RGB')]
    for index, frame in enumerate(frames):
        x = index * 648
        error = sum(ImageStat.Stat(ImageChops.difference(asset.crop((x,0,x+640,480)),frame)).mean) / 3
        assert error < 2, (index, error)
        checks.append({'panel': index + 1, 'recorded_run': folder.name, 'mean_absolute_rgb_error': round(error,4), 'threshold': 2, 'source': ['s0a1-frame.png + s0a1-pick.png tint', 's0a1-scene_after.png', 's0a1-basket_after.png'][index]})
else:
    raise ValueError(kind)
print(json.dumps(checks))
