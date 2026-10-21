"""The HTTP contract (specs/hasami.md) through FastAPI's TestClient against the real session."""

import base64
import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from hasami_core import simulated_click, simulated_drop_clicks
from hasami_server import create_app


@pytest.fixture()
def client(session, runs_tmp, no_model_key, monkeypatch):
    monkeypatch.delenv("HASAMI_SO101_PORT", raising=False)
    session.reset(1)
    with TestClient(create_app(session)) as c:
        yield c
    session.store_images = session.snapshot_on_phase = False  # the session fixture outlives this client
    session.reset(1)


def png(resp) -> np.ndarray:
    return np.array(Image.open(io.BytesIO(resp.content)))


def read_sse(resp) -> list[tuple[str, dict]]:
    events, kind = [], None
    for line in resp.iter_lines():
        if line.startswith("event: "):
            kind = line[7:]
        elif line.startswith("data: ") and kind:
            events.append((kind, json.loads(line[6:])))
            kind = None
    return events


def test_status_shape(client):
    st = client.get("/api/status").json()
    assert st["arm"] == "sim"
    assert st["so101"] == {"connected": False, "port": None,
                           "setup": "Setup required: HASAMI_SO101_PORT (SO-101 follower arm on USB) and "
                                    "`pip install lerobot[feetech]`"}
    assert st["planner"]["ready"] is False and st["planner"]["model"] == "muse-spark-1.3"
    assert st["planner"]["setup"].startswith("Setup required: MODEL_API_KEY")
    segs = {s["name"]: s for s in st["segmenters"]}
    assert segs["local"]["ready"] is True and segs["local"]["loaded"] is True
    assert segs["meta"]["ready"] is False and "MODEL_API_KEY" in segs["meta"]["setup"]
    assert [o["label"] for o in st["scene"]["objects"]] == ["red cube", "green cube", "blue cube", "tray"]
    assert st["run"]["busy"] is False


@pytest.mark.parametrize("camera", ["overhead", "demo"])
def test_frame_png_decodes(client, camera):
    r = client.get("/api/frame.png", params={"camera": camera})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = png(r)
    assert img.shape == (480, 640, 3) and img.std() > 5


def test_frame_unknown_camera(client):
    assert client.get("/api/frame.png", params={"camera": "wrist"}).status_code == 422


def test_segment_returns_mask_and_target(client, session):
    frame = session.observe()
    u, v = simulated_click(frame.rgb, "green cube")
    r = client.post("/api/segment", json={"point": [u, v]})
    assert r.status_code == 200, r.text
    body = r.json()
    mask = np.array(Image.open(io.BytesIO(base64.b64decode(body["mask_png"])))) > 0
    assert mask.shape == (480, 640) and mask[int(round(v)), int(round(u))]
    t = body["target"]
    assert set(t) == {"center", "top_z", "yaw", "pixels", "centroid_px"}
    true = session.arm.object_pos("green_cube")
    assert np.linalg.norm(np.array(t["center"][:2]) - true[:2]) < 0.003
    assert body["iou_vs_truth"] >= 0.6 and body["truth_label"] == "green cube"


def test_segment_errors(client):
    assert client.post("/api/segment", json={"text": "red cube"}).status_code == 400
    r = client.post("/api/segment", json={"text": "red cube", "segmenter": "meta"})
    assert r.status_code == 503 and r.json()["setup_required"] == "MODEL_API_KEY"


def test_plan_without_key_is_503(client):
    r = client.post("/api/plan", json={"instruction": "put the red cube in the tray"})
    assert r.status_code == 503
    assert r.json()["setup_required"] == "MODEL_API_KEY"
    r = client.post("/api/run", json={"instruction": "put the red cube in the tray"})
    assert r.status_code == 503 and r.json()["setup_required"] == "MODEL_API_KEY"


def test_run_rejects_bad_steps(client):
    r = client.post("/api/run", json={"steps": [{"pick_point": [10, 10], "place_point": [700, 10]}]})
    assert r.status_code == 422
    assert client.post("/api/run", json={}).status_code == 422


def test_run_streams_events_to_done(client, session, runs_tmp):
    frame = session.observe()
    pick = simulated_click(frame.rgb, "red cube")
    drop = simulated_drop_clicks(frame.rgb, 1)[0]
    r = client.post("/api/run", json={"steps": [{"pick_point": list(pick), "place_point": list(drop)}]})
    assert r.status_code == 200, r.text
    run_id = r.json()["run_id"]

    busy = client.post("/api/run", json={"steps": [{"pick_point": list(pick), "place_point": list(drop)}]})
    assert busy.status_code == 409 and busy.json()["run_id"] == run_id
    assert client.post("/api/reset", json={}).status_code == 409

    with client.stream("GET", "/api/events", params={"once": "true"}) as resp:
        assert resp.headers["content-type"].startswith("text/event-stream")
        events = read_sse(resp)
    kinds = [k for k, _ in events]
    assert kinds[0] == "plan" and kinds[-1] == "done"
    assert kinds.count("phase") == 8 and kinds.count("result") == 1
    assert all(d["run_id"] == run_id for _, d in events)
    result = next(d for k, d in events if k == "result")
    assert result["object"] == "red cube" and result["in_tray"] is True
    assert events[-1][1]["ok"] is True

    # recorded and listed
    runner = client.app.state.runner
    runner.thread.join(timeout=30)
    runs = client.get("/api/runs").json()["runs"]
    assert runs and runs[0]["run_id"] == run_id and runs[0]["ok"] is True
    full = client.get(f"/api/runs/{run_id}").json()
    assert full["steps"][0]["result"]["in_tray"] is True
    assert (runs_tmp / f"{run_id}.json").is_file()
    assert client.get("/api/runs/../../etc").status_code == 404

    # what the agent saw, per step: frame, SAM masks, truth masks, and the composites
    imgs = full["steps"][0]["attempts"][0]["images"]
    assert set(imgs) == {"frame", "after", "pick", "place", "pick_truth", "place_truth"}
    view = png(client.get(f"/api/runs/{run_id}/steps/0.png"))
    frame_png = png(client.get(f"/api/runs/{run_id}/steps/0.png", params={"kind": "frame"}))
    assert view.shape == frame_png.shape == (480, 640, 3) and not np.array_equal(view, frame_png)
    pick_mask = png(client.get(f"/api/runs/{run_id}/steps/0.png", params={"kind": "pick"})) > 127
    truth = png(client.get(f"/api/runs/{run_id}/steps/0.png", params={"kind": "pick_truth"})) > 127
    iou = (pick_mask & truth).sum() / (pick_mask | truth).sum()
    assert iou == pytest.approx(full["steps"][0]["attempts"][0]["pick"]["iou_vs_truth"], abs=1e-3)
    assert client.get(f"/api/runs/{run_id}/steps/0.png", params={"kind": "view_truth"}).status_code == 200
    assert client.get(f"/api/runs/{run_id}/steps/5.png").status_code == 404
    assert client.get(f"/api/runs/{run_id}/steps/0.png", params={"attempt": 3}).status_code == 404


def test_reset_to_a_task_scene(client):
    r = client.post("/api/reset", json={"seed": 3, "family": "sort-by-colour"}).json()
    assert r["scene"] == "sort-by-colour" and "instruction" in r and 4 <= len(r["objects"]) - 2 <= 8
    st = client.get("/api/status").json()
    assert st["scene"]["name"] == "sort-by-colour" and {"tray", "bin"} <= {o["name"] for o in st["scene"]["objects"]}
    assert client.post("/api/reset", json={"seed": 3, "family": "juggling"}).status_code == 422
    back = client.post("/api/reset", json={"seed": 1}).json()
    assert back["scene"] == "classic"


def test_reset(client):
    a = client.post("/api/reset", json={"seed": 42}).json()
    b = client.post("/api/reset", json={"seed": 42}).json()
    assert a["seed"] == 42 and a == b
    assert set(a["objects"]) == {"red_cube", "green_cube", "blue_cube", "tray"}
    c = client.post("/api/reset").json()
    assert isinstance(c["seed"], int)


def test_root_serves_dashboard_or_a_note(client):
    from hasami_server.app import DASHBOARD_DIST

    r = client.get("/")
    assert r.status_code == 200
    if DASHBOARD_DIST.is_dir():
        assert r.headers["content-type"].startswith("text/html")
    else:
        assert "Dashboard not built" in r.json()["note"]


def test_cors_allows_localhost_dev_ports(client):
    r = client.get("/api/status", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:5173"
    r = client.get("/api/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers

