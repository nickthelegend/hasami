"""The plan schema, shared by the Muse planner and the click path. Validated strictly."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

FRAME_W, FRAME_H = 640, 480
MAX_STEPS = 6


class PlanError(ValueError):
    """The model (or the caller) produced a plan that does not validate. `raw` keeps what it sent."""

    def __init__(self, message: str, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


def _check_point(v: list[float]) -> list[float]:
    if len(v) != 2:
        raise ValueError(f"a point is [u, v], got {len(v)} numbers")
    u, w = float(v[0]), float(v[1])
    if not (0 <= u < FRAME_W and 0 <= w < FRAME_H):
        raise ValueError(f"point ({u:.1f}, {w:.1f}) is outside the {FRAME_W}x{FRAME_H} frame")
    return [u, w]


class PlanStep(BaseModel):
    """Move one object: pick at pick_point, place at place_point (pixels in the overhead frame)."""

    model_config = ConfigDict(extra="forbid")

    pick: str = Field(min_length=1, max_length=80)
    pick_point: list[float]
    place: str = Field(min_length=1, max_length=80)
    place_point: list[float]
    reason: str = Field(default="", max_length=400)

    @field_validator("pick_point", "place_point")
    @classmethod
    def _in_frame(cls, v: list[float]) -> list[float]:
        return _check_point(v)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: list[PlanStep] = Field(min_length=1, max_length=MAX_STEPS)
    # muse: a Muse reply; clicks: a person (or the click oracle) clicked; scripted: a Muse-format plan written
    # from a benchmark task by code (labels + deliberately perturbed hints), no model involved
    source: Literal["muse", "clicks", "scripted"]
    model: str | None = None
    instruction: str | None = None


def tool_schema() -> dict:
    """JSON schema of the `submit_plan` tool the planner asks Muse to call."""
    point = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
             "description": f"[u, v] pixel in the {FRAME_W}x{FRAME_H} overhead image; u right, v down"}
    step = {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "description": "label of the object to pick, e.g. 'red cube'"},
            "pick_point": point | {"description": "pixel on the object to pick (its visible centre)"},
            "place": {"type": "string", "description": "label of the destination, e.g. 'tray'"},
            "place_point": point | {"description": "pixel where the object should end up"},
            "reason": {"type": "string", "description": "one short sentence"},
        },
        "required": ["pick", "pick_point", "place", "place_point", "reason"],
        "additionalProperties": False,
    }
    return {
        "type": "function",
        "function": {
            "name": "submit_plan",
            "description": "Submit the ordered pick-and-place steps that carry out the instruction.",
            "parameters": {
                "type": "object",
                "properties": {"steps": {"type": "array", "items": step, "minItems": 1, "maxItems": MAX_STEPS}},
                "required": ["steps"],
                "additionalProperties": False,
            },
        },
    }


def validate_steps(steps: object, *, source: Literal["muse", "clicks", "scripted"], model: str | None = None,
                   instruction: str | None = None, raw: str | None = None) -> Plan:
    try:
        return Plan(steps=steps, source=source, model=model, instruction=instruction)  # type: ignore[arg-type]
    except ValidationError as e:
        problems = "; ".join(f"{'.'.join(str(x) for x in err['loc'])}: {err['msg']}" for err in e.errors())
        raise PlanError(f"invalid plan: {problems}", raw=raw) from e


def plan_from_clicks(steps: list[dict]) -> Plan:
    """The no-key path: a person clicked each object and where it goes.

    Each step needs pick_point and place_point; labels are optional (the clicks are the plan).
    """
    if not isinstance(steps, list):
        raise PlanError("steps must be a list of {pick_point, place_point}")
    norm = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise PlanError(f"step {i}: expected an object with pick_point and place_point")
        norm.append({
            "pick": s.get("pick") or "clicked object",
            "pick_point": s.get("pick_point"),
            "place": s.get("place") or "clicked target",
            "place_point": s.get("place_point"),
            "reason": s.get("reason") or "clicked by the user",
        })
    return validate_steps(norm, source="clicks")
