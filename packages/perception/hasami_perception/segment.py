"""Segmenters. Both return a boolean H x W mask for one object."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
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
                text: str | None = None, box: tuple[float, float, float, float] | None = None) -> np.ndarray:
        """Mask of the object at `point`. With `box` (x1, y1, x2, y2) SAM is also given the box, a tighter
        prompt that keeps the mask from spreading past it; the mask is cut to the box."""
        if point is None:
            raise ValueError("SAM 2.1 needs a point prompt (u, v); text prompts need SAM 3.1 (MetaSam3)")
        img = Image.fromarray(rgb)
        extra = {} if box is None else {"input_boxes": [[[float(v) for v in box]]]}
        inp = self.processor(images=img, input_points=[[[[float(point[0]), float(point[1])]]]],
                             input_labels=[[[1]]], return_tensors="pt", **extra).to(self.device)
        key = hashlib.blake2b(rgb.tobytes(), digest_size=16).hexdigest()
        with self._torch.no_grad():
            if self._cache_key != key:  # the image encoder is most of the cost: run it once per frame
                self._cache_emb = self.model.get_image_embeddings(inp["pixel_values"])
                self._cache_key = key
            out = self.model(input_points=inp["input_points"], input_labels=inp["input_labels"],
                             input_boxes=inp.get("input_boxes"), image_embeddings=self._cache_emb,
                             multimask_output=True)
        masks = self.processor.post_process_masks(out.pred_masks.cpu(), inp["original_sizes"].cpu())[0][0].numpy() > 0
        scores = out.iou_scores[0, 0].cpu().numpy()
        if box is not None:
            x1, y1, x2, y2 = (int(round(v)) for v in box)
            keep = np.zeros(masks.shape[1:], dtype=bool)
            keep[max(0, y1):y2 + 1, max(0, x1):x2 + 1] = True
            masks = masks & keep
        # Of SAM's three hypotheses, keep the best-scored one that contains the clicked pixel.
        # the rounded pixel, clamped to the mask: 639.6 on a 640-wide frame is column 639, not 640
        u = min(max(int(round(point[0])), 0), masks.shape[2] - 1)
        v = min(max(int(round(point[1])), 0), masks.shape[1] - 1)
        order = np.argsort(-scores)
        for i in order:
            if masks[i][v, u]:
                return masks[i]
        return masks[order[0]]


    def segment_many(self, rgb: np.ndarray, points: list[tuple[float, float]], batch: int = 16
                     ) -> list[tuple[np.ndarray, np.ndarray]]:
        """Every point as its own single-click prompt (the image is encoded once). Returns, per point, SAM's
        three hypotheses (3 x H x W bool) and their predicted IoU scores (3,). Used for automatic masks."""
        if not len(points):
            return []
        img = Image.fromarray(rgb)
        key = hashlib.blake2b(rgb.tobytes(), digest_size=16).hexdigest()
        out: list[tuple[np.ndarray, np.ndarray]] = []
        for i in range(0, len(points), batch):
            chunk = [[[float(u), float(v)]] for u, v in points[i:i + batch]]
            inp = self.processor(images=img, input_points=[chunk], input_labels=[[[1] for _ in chunk]],
                                 return_tensors="pt").to(self.device)
            with self._torch.no_grad():
                if self._cache_key != key:
                    self._cache_emb = self.model.get_image_embeddings(inp["pixel_values"])
                    self._cache_key = key
                res = self.model(input_points=inp["input_points"], input_labels=inp["input_labels"],
                                 image_embeddings=self._cache_emb, multimask_output=True)
            masks = self.processor.post_process_masks(res.pred_masks.cpu(), inp["original_sizes"].cpu())[0].numpy() > 0
            scores = res.iou_scores[0].cpu().numpy()
            out.extend((masks[j], scores[j]) for j in range(len(chunk)))
        return out


class MetaSam3:
    """SAM 3.1 on the Meta Model API: text-prompted segmentation ("the red cube").

    Wire format per dev.meta.ai/docs/media-segmentation and /docs/sam/reading-segmentation (read
    2026-10-21): POST {base}/responses with model "sam-3.1" and one user message holding an input_text
    (the prompt) and an input_image; the output is special-token text, one line per frame
    ("<0f>id<|box;x1=..;y1=..;x2=..;y2=..;w=W;h=H[;c=conf]|><|mask;x=0;y=0;data=H,W,<enc>payload|>,id...").
    Box corners are source pixels (documented). Masks are base85 payloads whose decoding the docs leave
    to Meta's `meta-sam-parser` package; if it is installed the mask is used, otherwise SAM 2.1 is
    prompted at the box centre by the caller (hasami_core grounding). Never run with a key yet: the
    request and the parser are written to the docs but unverified. Without MODEL_API_KEY this raises
    SetupRequired; it never returns a placeholder mask.
    """

    name = "sam-3.1 (Meta Model API)"

    def __init__(self, api_key: str | None = None, model: str = "sam-3.1", base_url: str | None = None):
        self.api_key = api_key or os.environ.get("MODEL_API_KEY")
        if not self.api_key:
            raise SetupRequired("Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)")
        self.model = model
        # read when constructed (as the Muse client does), not when this module was imported
        self.base_url = (base_url or os.environ.get("MODEL_API_BASE_URL") or META_BASE_URL).rstrip("/")

    def request_body(self, rgb: np.ndarray, text: str) -> dict:
        buf = io.BytesIO()
        Image.fromarray(rgb).save(buf, format="PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        return {"model": self.model, "stream": False, "metadata": {"mask_encoding": "one_bit"},
                "input": [{"type": "message", "role": "user", "content": [
                    {"type": "input_text", "text": text},
                    {"type": "input_image", "image_url": f"data:image/png;base64,{b64}"},
                ]}]}

    def detect(self, rgb: np.ndarray, text: str) -> list[dict]:
        """All matches for `text`: [{id, box: (x1, y1, x2, y2) in frame pixels, conf, mask: array | None}]."""
        import urllib.error
        import urllib.request

        req = urllib.request.Request(f"{self.base_url}/responses", data=json.dumps(self.request_body(rgb, text)).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"SAM 3.1 request failed: HTTP {e.code} {e.read()[:300]!r}") from e
        return parse_sam_output(_output_text(data), rgb.shape[:2])

    def segment(self, rgb: np.ndarray, *, point: tuple[float, float] | None = None,
                text: str | None = None) -> np.ndarray:
        if not text:
            raise ValueError("SAM 3.1 is prompted with text, e.g. 'red cube'")
        found = self.detect(rgb, text)
        if not found:
            raise RuntimeError(f"SAM 3.1 found no {text!r}")
        best = max(found, key=lambda d: d.get("conf") or 0.0)
        if best["mask"] is None:
            raise RuntimeError("SAM 3.1 returned a box but its mask could not be decoded "
                               "(pip install meta-sam-parser); box " + str(best["box"]))
        return best["mask"]


def _output_text(resp: dict) -> str:
    """The text of a Responses API result (output_text, or the output message's text parts)."""
    if isinstance(resp.get("output_text"), str):
        return resp["output_text"]
    parts = []
    for item in resp.get("output", []) or []:
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
    if not parts:
        raise RuntimeError(f"SAM 3.1 response had no output text; keys: {list(resp)[:10]}")
    return "".join(parts)


_TOKEN = re.compile(r"<\|(box|mask);([^|]*)\|>")


def parse_sam_output(text: str, hw: tuple[int, int]) -> list[dict]:
    """Parse SAM 3.1's special-token output for one image (frame 0) into boxes (+ masks if decodable).

    Documented grammar: '<0f>' then comma-separated records 'id<|box;k=v;...|><|mask;k=v;...;data=H,W,<enc>payload|>'.
    """
    line = text.strip().splitlines()[0] if text.strip() else ""
    m = re.match(r"<(\d+)f>", line)
    body = line[m.end():] if m else line
    out: list[dict] = []
    for rec in re.split(r",(?=\d+<\|)", body):
        idm = re.match(r"(\d+)", rec)
        if not idm:
            continue
        item: dict = {"id": int(idm.group(1)), "box": None, "conf": None, "mask": None}
        for kind, fields in _TOKEN.findall(rec):
            kv = dict(f.split("=", 1) for f in fields.split(";") if "=" in f)
            if kind == "box":
                sx = hw[1] / float(kv.get("w", hw[1]))
                sy = hw[0] / float(kv.get("h", hw[0]))
                item["box"] = (float(kv["x1"]) * sx, float(kv["y1"]) * sy, float(kv["x2"]) * sx, float(kv["y2"]) * sy)
                if "c" in kv:
                    item["conf"] = float(kv["c"])
            elif kind == "mask":
                if "c" in kv and item["conf"] is None:
                    item["conf"] = float(kv["c"])
                item["mask"] = _decode_mask(kv, item["box"], hw)
        if item["box"] is not None:
            out.append(item)
    return out


def _decode_mask(kv: dict, box, hw: tuple[int, int]) -> np.ndarray | None:
    """Decode with Meta's meta-sam-parser when installed (the docs define the payload only through it).
    The raster is placed at the box when its size matches the box, or used as is when it matches the frame."""
    try:
        import meta_sam_parser  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:  # the package's Python signature is not in the docs we read; any mismatch means "no mask"
        raster = np.asarray(meta_sam_parser.decode_mask_to_raster(kv["data"]))
        h, w = (int(v) for v in kv["data"].split(",")[:2])
        raster = raster.reshape(h, w)
    except Exception:  # noqa: BLE001
        return None
    full = np.zeros(hw, dtype=bool)
    if (h, w) == hw:
        return raster.astype(bool)
    if box is not None:
        x1, y1 = int(round(box[0])) + int(kv.get("x", 0)), int(round(box[1])) + int(kv.get("y", 0))
        hh, ww = min(h, hw[0] - y1), min(w, hw[1] - x1)
        if hh > 0 and ww > 0:
            full[y1:y1 + hh, x1:x1 + ww] = raster[:hh, :ww].astype(bool)
            return full
    return None
