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


def test_recorded_video_is_served_when_the_file_is_there(client, runs_tmp):
    # a record the way scripts/run_demo.py writes it, and one whose video file is not in this folder (videos stay out of git)
    runs_tmp.mkdir(parents=True, exist_ok=True)
    mp4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8
    (runs_tmp / "20261021T000000000Z-demo.mp4").write_bytes(mp4)
    for rid, video in (("20261021T000000000Z", {"file": "20261021T000000000Z-demo.mp4", "seconds": 1.0}),
                       ("20261021T000000001Z", {"file": "20261021T000000001Z-demo.mp4", "seconds": 2.0}),
                       ("20261021T000000002Z", {"file": "../../etc/passwd.mp4"})):
        (runs_tmp / f"{rid}.json").write_text(json.dumps({"run_id": rid, "steps": [], "video": video}))
    runs = {r["run_id"]: r for r in client.get("/api/runs").json()["runs"]}
    assert runs["20261021T000000000Z"]["video_url"] == "/api/runs/20261021T000000000Z/video"
    assert "video_url" not in runs["20261021T000000001Z"] and "video_url" not in runs["20261021T000000002Z"]
    r = client.get("/api/runs/20261021T000000000Z/video")
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and r.content == mp4
    part = client.get("/api/runs/20261021T000000000Z/video", headers={"Range": "bytes=0-15"})
    assert part.status_code == 206 and part.content == mp4[:16]
    assert client.get("/api/runs/20261021T000000001Z/video").status_code == 404
    assert client.get("/api/runs/20261021T000000002Z/video").status_code == 404
    assert client.get("/api/runs/nope/video").status_code == 404


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


# ---- real camera endpoints (no webcam here: grab is Setup required; a synthetic photo is uploaded) ----
def test_realcam_grab_needs_setup_and_order_is_enforced(client, monkeypatch):
    monkeypatch.delenv("HASAMI_REALCAM", raising=False)
    r = client.post("/api/realcam/grab")
    assert r.status_code == 503 and r.json()["setup_required"] == "HASAMI_REALCAM"
    assert client.post("/api/realcam/segment", json={"point": [10, 10], "height_mm": 30}).status_code == 409
    assert client.post("/api/realcam/upload", content=b"not an image").status_code == 400


def test_realcam_synthetic_photo_segment_and_locate(client):
    """A rendered (synthetic, labelled as such) photo: an A4 sheet and a red 50 x 30 x 40 mm box seen by a
    tilted pinhole camera. Upload, click the sheet's corners, click the box: SAM 2.1 + plane geometry."""
    from test_realcam import _raster, a4_corners, camera, project

    k, r, t = camera()
    img = np.zeros((720, 1280, 3), np.uint8)
    img[:] = (120, 104, 88)  # wooden table
    sheet_px = project(k, r, t, np.c_[a4_corners(), np.zeros(4)])
    img[_raster(sheet_px)] = (236, 236, 232)
    cx, cy, hgt = 120.0, 330.0, 40.0
    top = np.array([[145, 345], [95, 345], [95, 315], [145, 315]], float)
    base_px = project(k, r, t, np.c_[top, np.zeros(4)])
    top_px = project(k, r, t, np.c_[top, np.full(4, hgt)])
    for a, b in ((0, 1), (1, 2), (2, 3), (3, 0)):  # the visible side faces, shaded
        img[_raster(np.array([base_px[a], base_px[b], top_px[b], top_px[a]]))] = (150, 30, 30)
    img[_raster(top_px)] = (220, 45, 45)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    up = client.post("/api/realcam/upload", content=buf.getvalue())
    assert up.status_code == 200 and up.json()["source"] == "upload"
    cal = client.post("/api/realcam/calibrate", json={"corners": sheet_px.tolist(), "sheet": "a4", "hfov_deg": 70})
    assert cal.status_code == 200, cal.text
    centre_px = project(k, r, t, np.array([[cx, cy, hgt]]))[0]
    res = client.post("/api/realcam/segment", json={"point": centre_px.tolist(), "height_mm": hgt}).json()
    assert res["arm"] == "sim" and "not connected" in res["note"]
    tgt = res["target"]
    assert tgt and tgt["parallax_corrected"]
    assert np.hypot(tgt["x_mm"] - cx, tgt["y_mm"] - cy) < 4.0, tgt
    assert sorted(tgt["extent_mm"]) == pytest.approx([30, 50], abs=6)
    assert png(client.get("/api/realcam/frame.png")).shape == (720, 1280, 3)


def test_realcam_calibrate_detects_a_landscape_sheet(client):
    """`sheet: auto` reads the orientation from the clicked corners; an explicit choice is kept and labelled."""
    from hasami_perception.twin import order_corners
    from test_realcam import _raster, a4_corners, camera, project

    k, r, t = camera(30, 10)
    img = np.zeros((720, 1280, 3), np.uint8)
    img[:] = (120, 104, 88)
    sheet_px = order_corners(project(k, r, t, np.c_[a4_corners(True), np.zeros(4)]))
    img[_raster(sheet_px)] = (236, 236, 232)
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    assert client.post("/api/realcam/upload", content=buf.getvalue()).status_code == 200
    auto = client.post("/api/realcam/calibrate", json={"corners": sheet_px.tolist(), "sheet": "auto", "hfov_deg": 70})
    assert auto.status_code == 200, auto.text
    a = auto.json()
    assert a["orientation"] == "landscape" and a["sheet"] == "a4-landscape" and a["size_mm"] == [297.0, 210.0]
    assert a["orientation_source"] == "detected from the clicked corners"
    chosen = client.post("/api/realcam/calibrate", json={"corners": sheet_px.tolist(), "sheet": "a4", "hfov_deg": 70}).json()
    assert chosen["orientation"] == "portrait" and chosen["orientation_source"] == "chosen"


def test_twin_from_a_photo_then_run_a_task_on_it(client, tmp_path, monkeypatch):
    """POST a MuJoCo-rendered desk photo -> twin JSON + side-by-side PNG; adjust a height; run clear-desk on the
    twin over SSE and get the goal checked on the twin's physics, recorded with the twin's label."""
    from hasami_core.twin import TWIN_LABEL
    from hasami_sim.photo import PhotoCamera, SheetPose, render_photo
    from hasami_sim.scene import ObjectSpec

    monkeypatch.setenv("HASAMI_TWINS_DIR", str(tmp_path / "twins"))
    cam = PhotoCamera(pos=(0.52, 0.07, 0.34), fovy_deg=50)
    objs = [ObjectSpec("a", "cube", "red", 0.20, 0.06, 0.3), ObjectSpec("c", "cylinder", "green", 0.17, -0.08)]
    buf = io.BytesIO()
    Image.fromarray(render_photo(objs, SheetPose(), cam)).save(buf, format="PNG")
    r = client.post("/api/twin", content=buf.getvalue(), params={"hfov_deg": cam.hfov_deg})
    assert r.status_code == 200, r.text
    tw = r.json()
    assert tw["label"] == TWIN_LABEL and tw["sheet"]["corners_source"] == "detected"
    assert sorted(o["colour"] for o in tw["objects"]) == ["green", "red"]
    assert all(o["in_twin"] and o["reachable"] for o in tw["objects"])
    assert {t["id"] for t in tw["tasks"]} >= {"clear-desk", "sort-by-colour"}
    sbs = png(client.get(tw["images"]["side_by_side"]))
    assert sbs.shape[1] == 2 * cam.width + 8
    assert (tmp_path / "twins" / tw["twin_id"] / "twin.json").is_file()

    adj = client.post(f"/api/twin/{tw['twin_id']}/adjust", json={"heights_mm": {"0": 45}})
    assert adj.status_code == 200, adj.text
    adj = adj.json()
    assert adj["twin_id"] == tw["twin_id"]
    assert adj["objects"][0]["size_mm"][2] == 45 and adj["objects"][0]["height_source"] == "typed"

    assert client.post(f"/api/twin/{tw['twin_id']}/run", json={"task": "stack-everything"}).status_code == 422
    assert client.get("/api/twin/nope").status_code == 404
    # a reloaded dashboard restores the latest twin from the server, images included
    latest = client.get("/api/twin").json()["twin"]
    assert latest["twin_id"] == tw["twin_id"] and latest["tasks"] and latest["images"]["side_by_side"].endswith(".png")
    assert client.get(f"/api/twin/{tw['twin_id']}").json()["images"] == latest["images"]
    run = client.post(f"/api/twin/{tw['twin_id']}/run", json={"task": "clear-desk"})
    assert run.status_code == 200, run.text
    with client.stream("GET", "/api/events", params={"once": "true"}) as resp:
        events = read_sse(resp)
    kinds = [k for k, _ in events]
    assert "goal" in kinds and kinds.index("goal") < kinds.index("done") and kinds[-1] == "done"
    goal = next(d for k, d in events if k == "goal")
    assert goal["ok"] is True and goal["label"] == TWIN_LABEL
    client.app.state.runner.thread.join(timeout=60)
    rec = client.get(f"/api/runs/{run.json()['run_id']}").json()
    assert rec["scene"] == "twin" and rec["goal"]["ok"] and rec["twin"]["task"] == "clear-desk"
    assert rec["twin"]["planner"].startswith("simulated-click oracle")


def test_twin_needs_a_sheet(client, tmp_path, monkeypatch):
    monkeypatch.setenv("HASAMI_TWINS_DIR", str(tmp_path / "twins"))
    buf = io.BytesIO()
    Image.fromarray(np.full((480, 640, 3), (150, 120, 100), np.uint8)).save(buf, format="PNG")
    r = client.post("/api/twin", content=buf.getvalue())
    assert r.status_code == 422 and "sheet" in r.json()["error"]
    assert client.post("/api/twin", content=b"nope").status_code == 400
    assert client.post("/api/twin", content=buf.getvalue(), params={"corners": "1,2,3"}).status_code == 422
