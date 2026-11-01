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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

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
            # the Meta Model API accepts only "auto" (HTTP 400 for a named function, checked 2026-11-01); the system
            # prompt asks for submit_plan and a reply without it is a PlanError below
            tool_choice="auto",
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


class FetchIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    item_text: str = Field(min_length=2, max_length=100)
    destination: str = Field(min_length=2, max_length=100)


FETCH_INTENT_TOOL = {"type": "function", "function": {
    "name": "fetch_intent", "description": "Extract the item to find and delivery destination from a fetch instruction.",
    "parameters": {"type": "object", "properties": {
        "item_text": {"type": "string", "description": "Short visible item phrase for image segmentation"},
        "destination": {"type": "string", "description": "Destination explicitly named by the user, or 'operator'"}},
        "required": ["item_text", "destination"], "additionalProperties": False}}}


def fetch_intent(instruction: str, planner: Planner | None = None) -> tuple[FetchIntent, float, str]:
    """One Muse tool call, strict schema. No local language guess is substituted."""
    if not instruction.strip():
        raise PlanError("empty fetch instruction")
    planner = planner or Planner()
    t0 = time.perf_counter()
    response = planner.client.chat.completions.create(
        model=planner.model,
        messages=[{"role": "system", "content": "Extract only what the user requested. Do not invent an item. "
                   "If no destination is stated, use 'operator'. Call fetch_intent exactly once."},
                  {"role": "user", "content": instruction}],
        tools=[FETCH_INTENT_TOOL],
        tool_choice="auto",  # the only value the Meta Model API accepts; no fetch_intent call is a PlanError below
    )
    all_calls = response.choices[0].message.tool_calls or []
    calls = [c for c in all_calls
             if getattr(c, "function", None) and c.function.name == "fetch_intent"]
    if len(calls) != 1 or len(all_calls) != 1:
        raise PlanError(f"Muse returned {len(all_calls)} tool calls, {len(calls)} fetch_intent; expected exactly one fetch_intent")
    try:
        intent = FetchIntent.model_validate_json(calls[0].function.arguments)
    except ValidationError as e:
        raise PlanError(f"invalid fetch_intent: {e}") from e
    return intent, (time.perf_counter() - t0) * 1000, planner.model
