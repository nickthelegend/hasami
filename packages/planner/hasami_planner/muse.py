"""Muse (Meta Model API) as the task planner: instruction + overhead frame -> validated plan.

The API is OpenAI-compatible, so this uses the `openai` SDK pointed at MODEL_API_BASE_URL.
Without MODEL_API_KEY the constructor raises SetupRequired; it never invents a plan.
"""

from __future__ import annotations

import base64
import json
import os
import time

from hasami_perception import SetupRequired

from .plan import FRAME_H, FRAME_W, MAX_STEPS, Plan, PlanError, tool_schema, validate_steps

DEFAULT_BASE_URL = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-spark-1.3"
SETUP_MESSAGE = "Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)"

SYSTEM_PROMPT = f"""You plan pick-and-place moves for a small robot arm (an SO-101) over a tabletop.
You see one overhead camera image, {FRAME_W}x{FRAME_H} pixels; u grows to the right, v grows downward.
Turn the user's instruction into an ordered list of at most {MAX_STEPS} steps. Each step picks one object
and puts it somewhere. For every step give:
- pick: the object's label, pick_point: the pixel at the visible centre of that object;
- place: the destination's label, place_point: the pixel where the object should end up. When several
  objects go into the same container, give each one a different free spot inside it so they do not stack.
Only use objects you can see. Respond by calling the submit_plan tool exactly once."""


def _model_name() -> str:
    return os.environ.get("HASAMI_PLANNER_MODEL", DEFAULT_MODEL)


def planner_status() -> dict:
    """For /api/status: is the planner usable, and if not, exactly what is missing."""
    ready = bool(os.environ.get("MODEL_API_KEY"))
    out: dict = {"ready": ready, "model": _model_name()}
    if not ready:
        out["setup"] = SETUP_MESSAGE
    return out


def build_messages(instruction: str, png: bytes, labels: list[str] | None = None) -> list[dict]:
    """System prompt + one user message carrying the instruction and the frame (images go in user
    messages only)."""
    text = f"Instruction: {instruction}"
    if labels:
        text += "\nObjects that may be in the scene: " + ", ".join(labels) + "."
    b64 = base64.b64encode(png).decode()
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]},
    ]


class Planner:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None,
                 timeout: float = 90.0):
        key = api_key or os.environ.get("MODEL_API_KEY")
        if not key:
            raise SetupRequired(SETUP_MESSAGE)
        from openai import OpenAI

        self.model = model or _model_name()
        self.base_url = base_url or os.environ.get("MODEL_API_BASE_URL", DEFAULT_BASE_URL)
        self.client = OpenAI(api_key=key, base_url=self.base_url, timeout=timeout, max_retries=1)

    def plan(self, instruction: str, png: bytes, labels: list[str] | None = None) -> tuple[Plan, float]:
        """Returns (validated plan, latency in ms). Raises PlanError with the raw output if it is invalid."""
        if not instruction or not instruction.strip():
            raise PlanError("empty instruction")
        t0 = time.perf_counter()
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=build_messages(instruction, png, labels),  # type: ignore[arg-type]
            tools=[tool_schema()],  # type: ignore[list-item]
            tool_choice={"type": "function", "function": {"name": "submit_plan"}},  # type: ignore[arg-type]
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        msg = resp.choices[0].message
        calls = [c for c in (msg.tool_calls or []) if getattr(c, "function", None) and c.function.name == "submit_plan"]
        if not calls:
            raise PlanError("the model did not call submit_plan", raw=msg.content or "")
        raw = calls[0].function.arguments or ""
        try:
            args = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlanError(f"submit_plan arguments are not JSON: {e}", raw=raw) from e
        if not isinstance(args, dict) or "steps" not in args:
            raise PlanError("submit_plan arguments have no 'steps'", raw=raw)
        plan = validate_steps(args["steps"], source="muse", model=self.model, instruction=instruction, raw=raw)
        return plan, latency_ms
