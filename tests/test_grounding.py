"""Grounding a planner's label + hint pixel: validation and the fallback order, on a synthetic image.

The segmenter passed in here is a flood fill over same-coloured, same-height pixels: a deterministic
stand-in for SAM so the decision logic (accept, fall back, give up) can be checked exactly."""

from collections import deque

import numpy as np
import pytest

from hasami_perception.grounding import GroundingError, ground, is_container, validate_mask

TABLE, RED, BLUE, SHADOW, SLATE = (199, 189, 173), (222, 41, 41), (46, 92, 230), (120, 112, 100), (46, 51, 61)


def scene():
    h, w = 200, 300
    rgb = np.zeros((h, w, 3), np.uint8)
    rgb[:] = TABLE
    heights = np.zeros((h, w))
    rgb[50:70, 60:80] = RED  # red cube, 20x20 px, 25 mm tall
    heights[50:70, 60:80] = 0.025
    rgb[70:78, 64:84] = SHADOW  # its shadow on the table
    rgb[50:70, 120:140] = BLUE  # blue cube
    heights[50:70, 120:140] = 0.025
    rgb[110:190, 180:280] = SLATE  # tray floor (6 mm)
    heights[110:190, 180:280] = 0.006
    return rgb, heights


def flood(rgb, heights):
    def seg(point):
        u, v = int(round(point[0])), int(round(point[1]))
        h, w = heights.shape
        out = np.zeros((h, w), bool)
        if not (0 <= u < w and 0 <= v < h):
            return out
        c0, z0 = rgb[v, u].astype(int), heights[v, u]
        q = deque([(v, u)])
        out[v, u] = True
        while q:
            a, b = q.popleft()
            for da, db in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                y, x = a + da, b + db
                if 0 <= y < h and 0 <= x < w and not out[y, x] and abs(heights[y, x] - z0) < 0.003 \
                        and np.abs(rgb[y, x].astype(int) - c0).max() < 30:
                    out[y, x] = True
                    q.append((y, x))
        return out
    return seg


def test_hint_on_the_object_is_accepted():
    rgb, z = scene()
    g = ground("red cube", (70, 60), rgb, z, flood(rgb, z))
    assert g.source == "hint" and g.mask.sum() == 400 and g.tries[0]["rejected"] == []


def test_hint_on_the_shadow_falls_back_to_the_colour():
    rgb, z = scene()
    g = ground("red cube", (74, 74), rgb, z, flood(rgb, z))
    assert g.source == "colour search"
    assert any("above the table" in r for r in g.tries[0]["rejected"])
    assert g.mask[60, 70] and g.mask.sum() == 400


def test_hint_on_the_wrong_object_is_rejected_by_colour():
    rgb, z = scene()
    g = ground("red cube", (128, 58), rgb, z, flood(rgb, z))  # Muse pointed at the blue cube
    assert "label says red" in g.tries[0]["rejected"][-1]
    assert g.source == "colour search" and g.mask[60, 70]


def test_no_colour_word_uses_the_nearest_raised_blob():
    rgb, z = scene()
    g = ground("the cube", (74, 76), rgb, z, flood(rgb, z))
    assert g.source == "raised blob" and g.mask[55, 65]


def test_gives_up_loudly_with_every_try():
    rgb, z = scene()
    with pytest.raises(GroundingError) as e:
        ground("purple cylinder", (20, 180), rgb, z, flood(rgb, z), search_px=40)
    tries = e.value.tries
    assert tries[0]["source"] == "hint" and tries[0]["rejected"]
    assert any(t["source"] == "colour search" and t["point"] is None for t in tries)
    assert "could not ground 'purple cylinder'" in str(e.value)


def test_containers_skip_the_object_checks():
    rgb, z = scene()
    assert is_container("tray") and is_container("the grey bin") and not is_container("red cube")
    g = ground("tray", (230, 150), rgb, z, flood(rgb, z))
    assert g.source == "hint" and g.mask.sum() == 8000
    assert validate_mask(g.mask, "red cube", rgb, z)  # the same mask is not a red cube


def test_size_and_distance_limits():
    rgb, z = scene()
    tiny = np.zeros(z.shape, bool)
    tiny[60, 70] = True
    assert any("pixels" in r for r in validate_mask(tiny, "red cube", rgb, z))
    far = flood(rgb, z)((70, 60))
    assert any("from the hint" in r for r in validate_mask(far, "red cube", rgb, z, hint=(250, 20)))


# ---- SAM 3.1 wire format (dev.meta.ai docs, 2026-10-21): pure parsing; no key here, so no live call ----
def test_sam31_output_tokens_parse_to_boxes_in_frame_pixels():
    from hasami_perception.segment import _output_text, parse_sam_output

    # the documented example (truncated payload), plus a second record with a confidence
    text = ("<0f>0<|box;x1=353;y1=382;x2=453;y2=427;w=537;h=561|><|mask;x=0;y=0;data=46,101,~abc|>,"
            "1<|box;x1=10;y1=20;x2=30;y2=40;w=537;h=561;c=0.9|>")
    found = parse_sam_output(_output_text({"output": [{"content": [{"type": "output_text", "text": text}]}]}),
                             (561, 537))
    assert [d["id"] for d in found] == [0, 1]
    assert found[0]["box"] == (353.0, 382.0, 453.0, 427.0) and found[1]["conf"] == 0.9
    # boxes are rescaled when the frame SAM saw differs from ours
    half = parse_sam_output(text, (280, 268))
    assert half[0]["box"][0] == pytest.approx(353 * 268 / 537)
    assert parse_sam_output("", (10, 10)) == []


def test_sam31_request_body_and_setup(monkeypatch):
    from hasami_perception import MetaSam3, SetupRequired

    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    with pytest.raises(SetupRequired, match="MODEL_API_KEY"):
        MetaSam3()
    body = MetaSam3(api_key="not-a-real-key").request_body(np.zeros((4, 4, 3), np.uint8), "red cube")
    assert body["model"] == "sam-3.1" and body["metadata"] == {"mask_encoding": "one_bit"}
    content = body["input"][0]["content"]
    assert body["input"][0]["role"] == "user"
    assert content[0] == {"type": "input_text", "text": "red cube"}
    assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/png;base64,")


# ---- in the simulator: a Muse-style plan (labels + hints) whose hint misses the object -----------------
def test_planned_step_with_a_bad_hint_is_grounded_and_placed(session):
    from hasami_core import simulated_click, simulated_drop_clicks
    from hasami_planner import validate_steps

    session.reset(2)
    frame = session.observe()
    u, v = simulated_click(frame.rgb, "red cube")
    drop = simulated_drop_clicks(frame.rgb, 1)[0]
    # 22 px beside the cube, on bare table: SAM at this pixel does not give a red, raised mask
    plan = validate_steps([{"pick": "red cube", "pick_point": [u + 22, v + 22], "place": "tray",
                            "place_point": list(drop), "reason": "hint deliberately off"}],
                          source="muse", model="test (no model called)", instruction="put the red cube in the tray")
    events = []
    rec = session.run(plan, on_event=lambda k, d: events.append((k, d)))
    seg = next(d for k, d in events if k == "segment" and d["role"] == "pick")
    assert seg["grounding"]["source"] in ("colour search", "raised blob")
    assert seg["grounding"]["tries"][0]["source"] == "hint" and seg["grounding"]["tries"][0]["rejected"]
    assert seg["truth_label"] == "red cube"
    assert rec["ok"] and rec["steps"][0]["result"]["in_tray"]


# ---- instruction level, no key: Muse-format plans written from every task family ----------------------
def test_bare_table_label_is_not_searched():
    from hasami_perception.grounding import GroundingError, ground, is_surface

    assert is_surface("the table") and is_surface("a free spot on the desk")
    assert not is_surface("tray") and not is_surface("red cube") and not is_surface("table tennis cube")
    with pytest.raises(GroundingError, match="bare table"):
        ground("the table", (10.0, 10.0), np.zeros((20, 20, 3), np.uint8), np.ones((20, 20)), lambda p: None)

