"""The plan schema (strict), the no-key behaviour, and a live Muse call when MODEL_API_KEY is set."""

import os

import pytest

from hasami_planner import (MAX_STEPS, SETUP_MESSAGE, PlanError, Planner, SetupRequired, build_messages,
                            plan_from_clicks, tool_schema, validate_steps)

GOOD = [
    {"pick": "red cube", "pick_point": [245.0, 243.0], "place": "tray", "place_point": [170.0, 270.0],
     "reason": "the user asked for the red cube in the tray"},
    {"pick": "blue cube", "pick_point": [398, 240], "place": "tray", "place_point": [200, 300], "reason": ""},
]


def test_schema_accepts_a_good_plan():
    plan = validate_steps(GOOD, source="muse", model="muse-spark-1.3", instruction="tidy up")
    assert [s.pick for s in plan.steps] == ["red cube", "blue cube"]
    assert plan.steps[1].pick_point == [398.0, 240.0]


@pytest.mark.parametrize("bad_point", [[640, 10], [10, 480], [-1, 5], [5, -0.5], [1, 2, 3], [7]])
def test_schema_rejects_points_outside_the_frame(bad_point):
    steps = [dict(GOOD[0], place_point=bad_point)]
    with pytest.raises(PlanError) as e:
        validate_steps(steps, source="muse", raw='{"steps": "..."}')
    assert "place_point" in str(e.value)
    assert e.value.raw == '{"steps": "..."}'


@pytest.mark.parametrize("edge", [[639.6, 10], [10, 479.6], [639.5, 479.5]])
def test_schema_rejects_points_that_round_onto_the_frame_edge(edge):
    """The segmenter indexes the mask at the rounded pixel, so 639.6 is pixel 640: off the 640-wide frame. The
    plan must refuse it up front (it used to validate, then the run failed inside with no steps)."""
    with pytest.raises(PlanError) as e:
        validate_steps([dict(GOOD[0], pick_point=edge)], source="clicks")
    assert "pick_point" in str(e.value) and "outside" in str(e.value)


def test_schema_accepts_the_last_pixel():
    plan = validate_steps([dict(GOOD[0], pick_point=[639.4, 479.4], place_point=[0, 0])], source="clicks")
    assert plan.steps[0].pick_point == [639.4, 479.4]


def test_schema_rejects_too_many_steps_unknown_fields_and_empty():
    with pytest.raises(PlanError):
        validate_steps([GOOD[0]] * (MAX_STEPS + 1), source="muse")
    with pytest.raises(PlanError):
        validate_steps([dict(GOOD[0], speed="fast")], source="muse")
    with pytest.raises(PlanError):
        validate_steps([], source="muse")
    with pytest.raises(PlanError):
        validate_steps([dict(GOOD[0], pick="")], source="muse")


def test_plan_from_clicks():
    plan = plan_from_clicks([{"pick_point": [100, 200], "place_point": [300, 400]}])
    assert plan.source == "clicks"
    assert plan.steps[0].pick == "clicked object"
    with pytest.raises(PlanError):
        plan_from_clicks([{"pick_point": [100, 200], "place_point": [900, 400]}])
    with pytest.raises(PlanError):
        plan_from_clicks([{"pick_point": [100, 200]}])


def test_tool_schema_is_a_submit_plan_function():
    t = tool_schema()
    assert t["type"] == "function" and t["function"]["name"] == "submit_plan"
    steps = t["function"]["parameters"]["properties"]["steps"]
    assert steps["maxItems"] == MAX_STEPS
    assert set(steps["items"]["required"]) == {"pick", "pick_point", "place", "place_point", "reason"}


def test_planner_without_key_is_setup_required(no_model_key):
    with pytest.raises(SetupRequired) as e:
        Planner()
    assert str(e.value) == "Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)"
    assert str(e.value) == SETUP_MESSAGE


def test_planner_puts_the_image_in_the_user_message_only():
    msgs = build_messages("put the red cube in the tray", b"\x89PNG\r\n\x1a\n", ["red cube", "tray"])
    assert msgs[0]["role"] == "system" and isinstance(msgs[0]["content"], str)
    user = msgs[1]
    assert user["role"] == "user"
    image = [p for p in user["content"] if p["type"] == "image_url"]
    assert len(image) == 1 and image[0]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("MODEL_API_KEY"), reason="MODEL_API_KEY not set (live Muse call)")
def test_live_muse_plan(session):
    session.reset(1)
    png, _ = session.frame_png("overhead")
    plan, latency_ms = Planner().plan("put the red cube in the tray", png,
                                      [o["label"] for o in session.scene_objects()])
    assert plan.source == "muse" and 1 <= len(plan.steps) <= MAX_STEPS
    assert "red" in plan.steps[0].pick.lower()
    assert latency_ms > 0
