"""What is ready and what needs setup. Shared by the HTTP server and the MCP server."""

from __future__ import annotations

import os

from hasami_perception.segment import SetupRequired
from hasami_planner import planner_status
from hasami_robot import so101_status

SAM2_CHECKPOINT = "facebook/sam2.1-hiera-tiny"
META_SETUP = "Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)"


def _sam2_cached() -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache

        hit = try_to_load_from_cache(SAM2_CHECKPOINT, "config.json")
        return isinstance(hit, str)
    except Exception:
        return False


def segmenters_status(session=None) -> list[dict]:
    local = {"name": "local", "model": "sam2.1-hiera-tiny", "prompt": "point", "ready": True,
             "loaded": bool(session is not None and session.sam_loaded), "weights_cached": _sam2_cached()}
    meta: dict = {"name": "meta", "model": "sam-3.1", "prompt": "text", "ready": bool(os.environ.get("MODEL_API_KEY"))}
    if not meta["ready"]:
        meta["setup"] = META_SETUP
    else:
        meta["note"] = "SAM 3.1 wire format on the Meta Model API is unverified"
    return [local, meta]


def status(session=None) -> dict:
    from .session import OBJECTS

    return {
        "arm": "sim",
        "so101": so101_status(),
        "planner": planner_status(),
        "segmenters": segmenters_status(session),
        "scene": {"objects": [{"name": n, "label": label} for n, label in OBJECTS.items()],
                  "seed": getattr(session, "seed", None)},
    }


def setup_name(err: SetupRequired | str) -> str:
    """'Setup required: MODEL_API_KEY (...)' -> 'MODEL_API_KEY'."""
    text = str(err)
    if text.startswith("Setup required:"):
        text = text.split(":", 1)[1].strip()
    return text.split()[0] if text else "unknown"
