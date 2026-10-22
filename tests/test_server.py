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
    # as the dashboard calls it: on 127.0.0.1, every POST with the X-Hasami header
    with TestClient(create_app(session), base_url="http://127.0.0.1:8420", headers={"X-Hasami": "1"}) as c:
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


def test_cors_allows_only_the_dashboard_and_its_dev_server(client):
    """CORS and the write guard trust an exact list: the server's own origin (8420) and the dashboard's Vite dev
    server (5190), on 127.0.0.1 / localhost / [::1]. Another app on another localhost port is a foreign site: it
    cannot read the API (no CORS header) and its writes get 403 even with X-Hasami or a JSON Content-Type."""
    for origin in ("http://localhost:5190", "http://127.0.0.1:5190", "http://127.0.0.1:8420", "http://localhost:8420",
                   "http://[::1]:8420"):
        r = client.get("/api/status", headers={"Origin": origin})
        assert r.headers.get("access-control-allow-origin") == origin, origin
    for origin in ("http://localhost:5173", "http://localhost:8888", "http://127.0.0.1:3000", "https://evil.example",
                   "http://localhost", "https://localhost:8420"):
        r = client.get("/api/realcam/frame.png", headers={"Origin": origin})
        assert "access-control-allow-origin" not in r.headers, origin
        pre = client.options("/api/reset", headers={"Origin": origin, "Access-Control-Request-Method": "POST",
                                                    "Access-Control-Request-Headers": "x-hasami,content-type"})
        assert "access-control-allow-origin" not in pre.headers, origin
    seed = client.get("/api/status").json()["scene"]["seed"]
    for headers in ({"Origin": "http://localhost:8888", "X-Hasami": "1"},
                    {"Origin": "http://127.0.0.1:3000", "Content-Type": "application/json"}):
        assert client.post("/api/realcam/upload", headers=headers, content=b"").status_code == 403, headers
        r = client.post("/api/reset", headers=headers, content=b'{"seed": 9}')
        assert r.status_code == 403 and "cross-site" in r.json()["error"], headers
    assert client.get("/api/status").json()["scene"]["seed"] == seed


def test_allowed_origins_follow_the_port_and_the_env(monkeypatch):
    from hasami_server.app import allowed_origins_from_env

    monkeypatch.setenv("HASAMI_PORT", "8431")
    monkeypatch.setenv("HASAMI_ALLOWED_ORIGINS", "http://studio.local:9000, http://127.0.0.1:7000")
    got = set(allowed_origins_from_env())
    assert {"http://127.0.0.1:8431", "http://localhost:8431", "http://127.0.0.1:8420", "http://localhost:5190",
            "http://studio.local:9000", "http://127.0.0.1:7000"} <= got
    assert not any(o.endswith(":8888") or o.endswith(":5173") for o in got)


def test_foreign_hosts_and_cross_site_writes_are_refused(client):
    """DNS rebinding (a page on another name resolved to 127.0.0.1 sends its own Host) gets 400. A cross-site
    POST that needs no CORS preflight (text/plain or no body) gets 403 and changes nothing: every write needs the
    X-Hasami header or a JSON / PNG / JPEG Content-Type, which a foreign page cannot send without a preflight."""
    bare = TestClient(client.app, base_url="http://127.0.0.1:8420")  # no X-Hasami header; the app is already up
    for host in ("rebind.evil.example", "rebind.evil.example:8420", "192.168.1.20:8420", "127.0.0.1.evil.example"):
        r = bare.get("/api/status", headers={"Host": host})
        assert r.status_code == 400 and "Host" in r.json()["error"], (host, r.status_code)
    for host in ("127.0.0.1:8420", "localhost:8420", "[::1]:8420", "localhost"):
        assert bare.get("/api/status", headers={"Host": host}).status_code == 200, host

    def photo(value: int) -> bytes:
        buf = io.BytesIO()
        Image.fromarray(np.full((48, 64, 3), value, np.uint8)).save(buf, format="PNG")
        return buf.getvalue()

    # the dashboard's own upload (or curl with an image Content-Type) works without the header
    assert bare.post("/api/realcam/upload", content=photo(200), headers={"Content-Type": "image/png"}).status_code == 200
    before = png(bare.get("/api/realcam/frame.png"))
    for headers in ({"Origin": "https://evil.example", "Content-Type": "text/plain"},
                    {"Content-Type": "text/plain"},
                    {"Origin": "null", "Content-Type": "application/x-www-form-urlencoded"},
                    {"Origin": "https://evil.example", "Content-Type": "image/png"},  # passed a preflight? not from there
                    {"Origin": "https://evil.example", "X-Hasami": "1", "Content-Type": "image/png"}):
        r = bare.post("/api/realcam/upload", content=photo(30), headers=headers)
        assert r.status_code == 403, (headers, r.status_code)
        assert "cross-site" in r.json()["error"]
    assert np.array_equal(png(bare.get("/api/realcam/frame.png")), before)  # the frame was not replaced
    # body-less writes need the header too
    seed = bare.get("/api/status").json()["scene"]["seed"]
    assert bare.post("/api/reset", headers={"Origin": "https://evil.example"}).status_code == 403
    assert bare.post("/api/reset").status_code == 403
    assert bare.get("/api/status").json()["scene"]["seed"] == seed
    assert bare.post("/api/realcam/grab", headers={"Origin": "https://evil.example"}).status_code == 403
    # JSON from the page itself, or a localhost dev server, is fine
    assert bare.post("/api/reset", json={"seed": 7}, headers={"Origin": "http://localhost:5190"}).status_code == 200
    assert bare.post("/api/reset", headers={"X-Hasami": "1", "Origin": "http://127.0.0.1:8420"}).status_code == 200


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


def test_realcam_calibrate_auto_follows_the_click_order(client):
    """`sheet: auto` decides the orientation for the corners in the order they were clicked, the order the
    homography is fitted on. A yawed sheet has no obvious top-left, so the first click can be any corner,
    either way round: edge 1 -> 2 must still measure its true length. Portrait and landscape x sheet yaw
    0 / 30 / 50 deg x first click on each corner x clockwise and anticlockwise, seen by a tilted camera."""
    from hasami_perception.realcam import homography, pixel_to_plane
    from test_realcam import a4_corners, camera, project

    k, r, t = camera(30, 10)
    buf = io.BytesIO()
    Image.fromarray(np.full((720, 1280, 3), 120, np.uint8)).save(buf, format="PNG")
    assert client.post("/api/realcam/upload", content=buf.getvalue()).status_code == 200
    wrong = []
    for landscape in (False, True):
        sheet = a4_corners(landscape) - a4_corners(landscape).mean(axis=0)
        for yaw_deg in (0, 30, 50):
            a = np.radians(yaw_deg)
            plane = sheet @ np.array([[np.cos(a), -np.sin(a)], [np.sin(a), np.cos(a)]]).T + [150.0, 150.0]
            px = project(k, r, t, np.c_[plane, np.zeros(4)])
            for start in range(4):
                for turn in (1, -1):
                    idx = [(start + turn * i) % 4 for i in range(4)]
                    clicks = px[idx]
                    res = client.post("/api/realcam/calibrate", json={"corners": clicks.tolist(), "sheet": "auto",
                                                                       "hfov_deg": 70})
                    assert res.status_code == 200, res.text
                    got = res.json()
                    true_01 = float(np.linalg.norm(plane[idx[1]] - plane[idx[0]]))
                    h = homography(a4_corners(got["orientation"] == "landscape"), clicks)
                    measured_01 = float(np.linalg.norm(np.diff(pixel_to_plane(h, clicks[:2]), axis=0)))
                    if abs(measured_01 - true_01) > 2.0 or abs(got["size_mm"][0] - true_01) > 2.0:
                        wrong.append((landscape, yaw_deg, start, turn, got["orientation"], round(measured_01), round(true_01)))
    assert not wrong, f"(landscape, yaw, first corner, turn, auto said, edge 1->2 measured, true): {wrong}"


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


# ---- photo uploads: size caps, decompression bombs, and never blocking the server ----
def _png_header_only(w: int, h: int) -> bytes:
    """A tiny PNG file that claims to be w x h pixels (a decompression bomb when w x h is huge)."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(b"\x00" * 10)) + chunk(b"IEND", b"")


def test_photo_uploads_refuse_bombs_and_oversize_bodies(client):
    """A photo that claims 400 MP (PIL's own bomb limit) or 81 MP (under PIL's warning, over Hasami's 40 MP) is
    refused with a readable 400 before any pixel is decoded; a body over 15 MB gets 413. Both upload endpoints."""
    for path in ("/api/realcam/upload", "/api/twin"):
        for w, h in ((20000, 20000), (9000, 9000)):
            r = client.post(path, content=_png_header_only(w, h), headers={"Content-Type": "image/png"})
            assert r.status_code == 400, (path, w, h, r.status_code)
            assert "too large" in r.json()["error"] and "MP" in r.json()["error"], r.json()
        r = client.post(path, content=b"\x00" * 15_000_001, headers={"Content-Type": "image/png"})
        assert r.status_code == 413 and "15 MB" in r.json()["error"], (path, r.status_code)
        assert client.post(path, content=b"", headers={"Content-Type": "image/png"}).status_code == 400
    assert client.get("/api/realcam/frame.png").status_code == 404  # nothing was stored


def test_a_twin_build_does_not_block_the_server(session, runs_tmp, no_model_key, tmp_path, monkeypatch):
    """A real uvicorn on 127.0.0.1: while POST /api/twin runs SAM 2.1 and MuJoCo (seconds), /api/status keeps
    answering quickly, because the decode and the build run in a worker thread, not on the event loop."""
    import socket
    import threading
    import time

    import httpx2
    import uvicorn

    monkeypatch.setenv("HASAMI_TWINS_DIR", str(tmp_path / "twins"))
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(session), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
        sample = httpx2.get(f"{base}/api/twin/sample.png?seed=3", timeout=60)
        hfov = sample.headers["x-hasami-hfov"]
        lat: list[float] = []
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                t0 = time.perf_counter()
                assert httpx2.get(f"{base}/api/status", timeout=60).status_code == 200
                lat.append(time.perf_counter() - t0)
                time.sleep(0.05)

        poller = threading.Thread(target=poll)
        poller.start()
        time.sleep(0.5)
        t0 = time.perf_counter()
        r = httpx2.post(f"{base}/api/twin?source=synthetic&hfov_deg={hfov}", content=sample.content,
                        headers={"Content-Type": "image/png"}, timeout=180)
        build_s = time.perf_counter() - t0
        time.sleep(0.3)
        stop.set()
        poller.join()
        assert r.status_code == 200, r.text
        during = len(lat)
        assert build_s > 1.0 and during > 10, (build_s, during)
        assert max(lat) < 0.2, f"/api/status took up to {max(lat) * 1000:.0f} ms during a {build_s:.1f} s twin build"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        session.store_images = session.snapshot_on_phase = False
        session.reset(1)


def test_twins_are_capped_and_the_least_recently_used_is_dropped(session, runs_tmp, no_model_key, tmp_path, monkeypatch):
    """Twins live in server memory with their photo and masks, and on disk: HASAMI_MAX_TWINS (8 by default) caps
    them. Building one more drops the twin used least recently (built, adjusted, loaded or run), from memory
    and from the twins folder; its id then gives 404."""
    from hasami_sim.photo import PhotoCamera, SheetPose, render_photo
    from hasami_sim.scene import ObjectSpec

    monkeypatch.setenv("HASAMI_TWINS_DIR", str(tmp_path / "twins"))
    monkeypatch.setenv("HASAMI_MAX_TWINS", "2")
    cam = PhotoCamera(pos=(0.52, 0.07, 0.34), fovy_deg=50)
    buf = io.BytesIO()
    Image.fromarray(render_photo([ObjectSpec("a", "cube", "red", 0.20, 0.06, 0.3)], SheetPose(), cam)).save(buf, format="PNG")
    on_disk = lambda: sorted(p.name for p in (tmp_path / "twins").iterdir())  # noqa: E731
    try:
        with TestClient(create_app(session), base_url="http://127.0.0.1:8420", headers={"X-Hasami": "1"}) as c:
            def build() -> str:
                r = c.post("/api/twin", content=buf.getvalue(), params={"hfov_deg": cam.hfov_deg},
                           headers={"Content-Type": "image/png"})
                assert r.status_code == 200, r.text
                return r.json()["twin_id"]

            ids = [build() for _ in range(3)]
            assert on_disk() == sorted(ids[1:])
            assert c.get(f"/api/twin/{ids[0]}").status_code == 404
            assert c.get(f"/api/twin/{ids[0]}/photo.png").status_code == 404
            assert all(c.get(f"/api/twin/{i}").status_code == 200 for i in ids[1:])
            # loading the older one makes it the most recently used: the next build drops the other
            assert c.post(f"/api/twin/{ids[1]}/load").status_code == 200
            ids.append(build())
            assert on_disk() == sorted([ids[1], ids[3]])
            assert c.get(f"/api/twin/{ids[2]}").status_code == 404
            assert c.get("/api/twin").json()["twin"]["twin_id"] == ids[3]
    finally:
        session.store_images = session.snapshot_on_phase = False
        session.reset(1)


# ---- malformed but schema-shaped inputs: a readable 4xx (or 502 for a failing remote model), never a bare 500 ----
def test_off_frame_clicks_and_non_finite_points_are_refused(client):
    for pt in ([700, 10], [-40, -40], [10, 480], [640, 0], [-0.6, 5]):
        r = client.post("/api/segment", json={"point": pt})
        assert r.status_code == 400, (pt, r.status_code)
        assert "outside the 640 x 480 frame" in r.json()["error"], r.json()
    r = client.post("/api/segment", content=b'{"point": [NaN, 10]}', headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert client.post("/api/segment", json={"point": [639.4, 479.4]}).status_code == 200  # the last pixel is fine


def test_seeds_must_fit_32_bits(client):
    for seed in (-1, 2**32, 2**70):
        assert client.post("/api/reset", json={"seed": seed}).status_code == 422, seed
        assert client.post("/api/reset", json={"seed": seed, "family": "stack-two"}).status_code == 422, seed
    assert client.post("/api/reset", json={"seed": 2**32 - 1}).json()["seed"] == 2**32 - 1
    assert client.post("/api/reset", json={"seed": 0, "family": "stack-two"}).status_code == 200


def test_corners_must_be_four_finite_pairs(client, tmp_path, monkeypatch):
    from hasami_sim.photo import PhotoCamera, SheetPose, render_photo
    from hasami_sim.scene import ObjectSpec

    buf = io.BytesIO()
    Image.fromarray(np.full((480, 640, 3), 120, np.uint8)).save(buf, format="PNG")
    assert client.post("/api/realcam/upload", content=buf.getvalue()).status_code == 200
    bad = ([[1], [2], [3], [4]], [[10, 10, 5], [600, 10, 5], [600, 400, 5], [10, 400, 5]], [[1, 2]] * 3)
    for corners in bad:
        r = client.post("/api/realcam/calibrate", json={"corners": corners, "sheet": "auto"})
        assert r.status_code == 422, (corners, r.status_code)
    r = client.post("/api/realcam/calibrate", content=b'{"corners": [[NaN, 1], [5, 1], [5, 5], [1, 5]]}',
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    # a twin's adjust takes the same corners
    monkeypatch.setenv("HASAMI_TWINS_DIR", str(tmp_path / "twins"))
    cam = PhotoCamera(pos=(0.52, 0.07, 0.34), fovy_deg=50)
    photo = io.BytesIO()
    Image.fromarray(render_photo([ObjectSpec("a", "cube", "red", 0.20, 0.06, 0.3)], SheetPose(), cam)).save(photo, format="PNG")
    tw = client.post("/api/twin", content=photo.getvalue(), params={"hfov_deg": cam.hfov_deg})
    assert tw.status_code == 200, tw.text
    for corners in bad:
        r = client.post(f"/api/twin/{tw.json()['twin_id']}/adjust", json={"corners": corners})
        assert r.status_code == 422, (corners, r.status_code)


def test_a_failing_meta_segmenter_is_a_readable_502(client, monkeypatch):
    """SAM 3.1's endpoint unreachable (a real refused connection on 127.0.0.1, nothing mocked; the placeholder
    key never leaves this machine): 502 with the error's type, not a bare 500."""
    import socket

    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))  # bound, not listening: connections are refused
        monkeypatch.setenv("MODEL_API_KEY", "placeholder-not-a-key")
        monkeypatch.setenv("MODEL_API_BASE_URL", f"http://127.0.0.1:{closed.getsockname()[1]}/v1")
        r = client.post("/api/segment", json={"text": "red cube", "segmenter": "meta"})
    assert r.status_code == 502, (r.status_code, r.text)
    assert r.json()["error"].startswith("URLError"), r.json()
