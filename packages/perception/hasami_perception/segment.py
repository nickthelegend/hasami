"""Segmenters. Both return a boolean H x W mask for one object."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
from typing import Protocol

import numpy as np
from PIL import Image

META_BASE_URL = os.environ.get("MODEL_API_BASE_URL", "https://api.meta.ai/v1")


class SetupRequired(RuntimeError):
    """A credential or device is missing. The message names exactly what to set."""


class Segmenter(Protocol):
    name: str

    def segment(self, rgb: np.ndarray, *, point: tuple[float, float] | None = None,
                text: str | None = None) -> np.ndarray: ...


class LocalSam2:
    """SAM 2.1 hiera-tiny from Hugging Face (open weights, no token), on CPU or Apple MPS."""

    name = "sam2.1-hiera-tiny (local)"

    def __init__(self, checkpoint: str = "facebook/sam2.1-hiera-tiny", device: str | None = None):
        import torch
        from transformers import Sam2Model, Sam2Processor

        self._torch = torch
        self.device = device or "cpu"
        self.processor = Sam2Processor.from_pretrained(checkpoint)
        self.model = Sam2Model.from_pretrained(checkpoint).to(self.device).eval()
        self._cache_key: str | None = None
        self._cache_emb = None

    def segment(self, rgb: np.ndarray, *, point: tuple[float, float] | None = None,
                text: str | None = None) -> np.ndarray:
        if point is None:
            raise ValueError("SAM 2.1 needs a point prompt (u, v); text prompts need SAM 3.1 (MetaSam3)")
        img = Image.fromarray(rgb)
        inp = self.processor(images=img, input_points=[[[[float(point[0]), float(point[1])]]]],
                             input_labels=[[[1]]], return_tensors="pt").to(self.device)
        key = hashlib.blake2b(rgb.tobytes(), digest_size=16).hexdigest()
        with self._torch.no_grad():
            if self._cache_key != key:  # the image encoder is most of the cost: run it once per frame
                self._cache_emb = self.model.get_image_embeddings(inp["pixel_values"])
                self._cache_key = key
            out = self.model(input_points=inp["input_points"], input_labels=inp["input_labels"],
                             image_embeddings=self._cache_emb, multimask_output=True)
        masks = self.processor.post_process_masks(out.pred_masks.cpu(), inp["original_sizes"].cpu())[0][0].numpy() > 0
        scores = out.iou_scores[0, 0].cpu().numpy()
        # Of SAM's three hypotheses, keep the best-scored one that contains the clicked pixel.
        u, v = int(round(point[0])), int(round(point[1]))
        order = np.argsort(-scores)
        for i in order:
            if masks[i][v, u]:
                return masks[i]
        return masks[order[0]]


class MetaSam3:
    """SAM 3.1 on the Meta Model API: text-prompted segmentation ("the red cube").

    The request goes to POST {base}/responses with the image inline; the response carries a
    mask per instance. Without MODEL_API_KEY this raises SetupRequired; it never returns a
    placeholder mask.
    """

    name = "sam-3.1 (Meta Model API)"

    def __init__(self, api_key: str | None = None, model: str = "sam-3.1", base_url: str = META_BASE_URL):
        self.api_key = api_key or os.environ.get("MODEL_API_KEY")
        if not self.api_key:
            raise SetupRequired("Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)")
        self.model = model
        self.base_url = base_url.rstrip("/")

    def segment(self, rgb: np.ndarray, *, point: tuple[float, float] | None = None,
                text: str | None = None) -> np.ndarray:
        import urllib.error
        import urllib.request

        if not text:
            raise ValueError("SAM 3.1 is prompted with text, e.g. 'red cube'")
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        body = {
            "model": self.model,
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                {"type": "input_text", "text": text},
            ]}],
        }
        req = urllib.request.Request(f"{self.base_url}/responses", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"SAM 3.1 request failed: HTTP {e.code} {e.read()[:300]!r}") from e
        return _first_mask(data, rgb.shape[:2])


def _first_mask(resp: dict, hw: tuple[int, int]) -> np.ndarray:
    """Find the first instance mask in a SAM 3.1 response (PNG/base64 or RLE). Fail loudly otherwise."""
    def walk(o):
        if isinstance(o, dict):
            yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)

    for node in walk(resp):
        for key in ("mask_png", "mask", "png"):
            val = node.get(key)
            if isinstance(val, str) and len(val) > 64:
                raw = base64.b64decode(val.split(",", 1)[-1])
                m = np.array(Image.open(io.BytesIO(raw)).convert("L").resize((hw[1], hw[0]))) > 127
                if m.any():
                    return m
        rle = node.get("rle") or node.get("counts")
        if isinstance(rle, list) and rle and isinstance(rle[0], int):
            flat = np.zeros(hw[0] * hw[1], dtype=bool)
            pos, val = 0, False
            for n in rle:
                flat[pos:pos + n] = val
                pos += n
                val = not val
            return flat.reshape(hw[1], hw[0]).T
    raise RuntimeError(f"SAM 3.1 response had no mask I could read; keys: {list(resp)[:10]}")
