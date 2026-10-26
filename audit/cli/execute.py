"""Bounded real-runtime audit. No monkeypatches, injected implementations, credentials or robot I/O.

Run with run-audit.sh, which holds the shared heavy slot. HTTP uses Playwright against uvicorn;
MCP uses the official stdio client. Large binary payloads are logged as lengths/hashes/dimensions.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import zlib

import numpy as np
from PIL import Image
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[2]
API = ROOT / "audit/api"
CLI = ROOT / "audit/cli"
API.mkdir(exist_ok=True)
RESULTS = json.loads((CLI / "results.json").read_text()) if (CLI / "results.json").exists() else {}
CHILDREN = []
SETUP = "Setup required: MODEL_API_KEY (Meta Model API key from https://dev.meta.ai/api-keys/)"
for name in ("MODEL_API_KEY", "HASAMI_REALCAM", "HASAMI_SO101_PORT", "HASAMI_DISTURB"):
    os.environ.pop(name, None)


def digest(b):
    return {"bytes": len(b), "sha256": hashlib.sha256(b).hexdigest()}


def compact(v):
    if isinstance(v, dict):
        return {k: ({"base64_omitted": True, **digest(base64.b64decode(x))} if k == "mask_png" and isinstance(x, str) and x else compact(x))
                for k, x in v.items()}
    if isinstance(v, list):
        return [compact(x) for x in v]
    return v


def write(path, value):
    path.write_text(json.dumps(compact(value), separators=(",", ":"), default=str) + "\n")


def result(row, status="PASS", note=""):
    RESULTS[row] = {"status": status, "note": note}
    write(CLI / "results.json", RESULTS)
    print(row, status, note, flush=True)


def stop(p):
    if p.poll() is None:
        p.terminate()
        try:
            p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()


def cleanup():
    for p in reversed(CHILDREN):
        stop(p)


def terminate(*_):
    raise SystemExit(143)


signal.signal(signal.SIGTERM, terminate)


class HTTP:
    def __init__(self, context):
        self.c = context
        self.row = "startup"
        self.logs = {}

    async def req(self, method, path, expected=200, body=None, data=None, headers=None):
        h = {"X-Hasami": "1", **(headers or {})}
        if body is not None:
            data = json.dumps(body, allow_nan=True)
            h["Content-Type"] = "application/json"
        started = time.monotonic()
        r = await self.c.fetch(path, method=method, data=data, headers=h, timeout=180000)
        b = await r.body()
        entry = {"method": method, "path": path, "status": r.status,
                 "expected": expected, "ms": round((time.monotonic()-started)*1000, 1),
                 "headers": {k: v for k, v in r.headers.items() if k.lower() in (
                     "content-type", "cache-control", "x-hasami-live", "x-hasami-photo", "x-hasami-hfov",
                     "access-control-expose-headers", "access-control-allow-origin", "content-range", "allow",
                     "x-accel-buffering")}}
        if method != "HEAD" and "json" in r.headers.get("content-type", ""):
            entry["json"] = compact(await r.json())
        elif "image" in r.headers.get("content-type", ""):
            entry["image"] = {**digest(b), "size": Image.open(io.BytesIO(b)).size}
        elif "event-stream" in r.headers.get("content-type", ""):
            events = []
            for block in b.decode().split("\n\n"):
                fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
                if "event" in fields:
                    event_data = json.loads(fields["data"])
                    events.append({"id": fields["id"], "event": fields["event"],
                                   "data": compact(event_data) if fields["event"] in ("result", "done", "goal", "recorded")
                                   else {"run_id": event_data["run_id"]}})
            entry["sse"] = {**digest(b), "retry": b.startswith(b"retry: 2000\n\n"), "events": events}
        else:
            entry["body"] = digest(b)
        self.logs.setdefault(self.row, []).append(entry)
        write(API / f"{self.row}.json", self.logs[self.row])
        assert r.status == expected, (method, path, expected, r.status, entry.get("json"))
        return r

    async def get(self, path, expected=200, **kw):
        return await self.req("GET", path, expected, **kw)

    async def post(self, path, body=None, expected=200, **kw):
        return await self.req("POST", path, expected, body=body, **kw)

    async def check(self, row, fn):
        self.row = row
        try:
            await fn()
            result(row)
        except Exception as e:
            result(row, "FAIL", f"{type(e).__name__}: {e}")

    async def jget(self, path, **kw):
        return await (await self.get(path, **kw)).json()

    async def jpost(self, path, body=None, **kw):
        return await (await self.post(path, body, **kw)).json()


def png_bytes(rgb):
    b = io.BytesIO()
    Image.fromarray(rgb).save(b, format="PNG")
    return b.getvalue()


def header_png(w, h):
    def chunk(kind, b):
        return struct.pack(">I", len(b)) + kind + b + struct.pack(">I", zlib.crc32(kind+b) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)) + chunk(b"IEND", b"")


async def start_server(h, tmp, port=8420, extra=None):
    env = os.environ.copy()
    env.update(HASAMI_RUNS_DIR=str(tmp / "runs"), HASAMI_TWINS_DIR=str(tmp / "twins"), HASAMI_PORT=str(port))
    env.update(extra or {})
    with open(API / "server.log", "a") as log:
        p = subprocess.Popen([sys.executable, "-m", "hasami_server"], cwd=ROOT, env=env, stdout=log, stderr=log)
    CHILDREN.append(p)
    for _ in range(120):
        if p.poll() is not None:
            raise RuntimeError(f"uvicorn exited {p.returncode}; see server.log")
        try:
            r = await h.c.get("/api/status", timeout=1000)
            if r.status == 200:
                return p
        except Exception:
            pass
        await asyncio.sleep(.25)
    raise RuntimeError("uvicorn startup timed out")


async def api_audit(tmp):
    from hasami_core import simulated_click, simulated_drop_clicks
    from hasami_core.tasks import FAMILIES
    (API / "server.log").unlink(missing_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 8420))
    async with async_playwright() as pw:
        c = await pw.request.new_context(base_url="http://127.0.0.1:8420")
        h = HTTP(c)
        dist = ROOT / "packages/dashboard/dist"
        hidden = dist.with_name("dist-audit-hidden")
        dist.rename(hidden)
        p = None
        try:
            p = await start_server(h, tmp)
            async def fallback():
                j = await h.jget("/")
                assert j["name"] == "hasami" and j["api"] == "/api/status"
                assert j["note"].startswith("Dashboard not built: run ") and "pnpm install && pnpm build" in j["note"]
            await h.check("A002", fallback)
        finally:
            if p: stop(p)
            hidden.rename(dist)
        p = await start_server(h, tmp)
        h.row = "A026"
        assert await (await h.get("/api/events?once=true")).text() == "retry: 2000\n\n"
        async def built():
            r = await h.get("/")
            assert r.headers["cache-control"] == "no-cache" and "text/html" in r.headers["content-type"]
            for asset in re.findall(r'(?:src|href)="(/assets/[^\"]+)"', await r.text()):
                assert (await h.get(asset)).headers["cache-control"] == "public, max-age=31536000, immutable"
            browser = await pw.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", headless=True)
            errors, network = [], []
            try:
                page = await browser.new_page()
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
                page.on("requestfailed", lambda r: errors.append(r.failure))
                page.on("response", lambda r: network.append({"url": r.url, "status": r.status}))
                await page.goto("http://127.0.0.1:8420/")
                await page.get_by_text("Viewfinder / overhead", exact=True).wait_for()
                await page.screenshot(path=str(API / "A001.jpg"), type="jpeg", quality=20)
                assert not errors and all(r["status"] < 400 for r in network), (errors, network)
            finally:
                write(API / "A001-browser.json", {"console_errors": errors, "network": network})
                await browser.close()
        await h.check("A001", built)
        async def status():
            j = await h.jget("/api/status")
            assert j["arm"] == "sim" and not j["so101"]["connected"]
            assert not j["planner"]["ready"] and j["planner"]["model"] == "muse-spark-1.3" and j["planner"]["setup"] == SETUP
            seg = {s["name"]: s for s in j["segmenters"]}
            assert seg["local"]["ready"] and seg["local"]["model"] == "sam2.1-hiera-tiny" and seg["local"]["prompt"] == "point"
            assert not seg["meta"]["ready"] and seg["meta"]["model"] == "sam-3.1" and seg["meta"]["prompt"] == "text"
            assert isinstance(j["run"]["busy"], bool) and {"name", "seed", "objects"} <= j["scene"].keys()
        await h.check("A003", status)
        async def frames():
            for cam in ("overhead", "demo"):
                r = await h.get(f"/api/frame.png?camera={cam}")
                assert r.headers["cache-control"] == "no-store" and r.headers["x-hasami-live"] in ("0", "1")
                assert Image.open(io.BytesIO(await r.body())).size == (640, 480)
        await h.check("A004", frames)
        async def invalid(row, path, cases, method="POST"):
            async def go():
                for body in cases:
                    r = await (h.post(path, body, 422) if method == "POST" else h.get(path+body, 422))
                    j = await r.json()
                    assert j["error"] and j["detail"]
                    assert all(set(d) == {"type", "loc", "msg"} for d in j["detail"])
            await h.check(row, go)
        await invalid("A005", "/api/frame.png", ["?camera=unknown"], "GET")
        h.row = "prerequisites"
        await h.post("/api/reset", {"seed": 0})
        rgb = np.array(Image.open(io.BytesIO(await (await h.get("/api/frame.png")).body())))
        pick = list(simulated_click(rgb, "red cube"))
        drop = list(simulated_drop_clicks(rgb, 1)[0])
        steps = [{"pick_point": pick, "place_point": drop}]
        async def segment():
            j = await h.jpost("/api/segment", {"point": pick})
            assert j["point"] == pick and j["segmenter"] == "sam2.1-hiera-tiny (local)" and isinstance(j["latency_ms"], (float,int))
            assert {"center", "top_z", "yaw", "pixels", "centroid_px"} == j["target"].keys()
            assert j["truth_label"] == "red cube" and j["iou_vs_truth"] > .6
            assert Image.open(io.BytesIO(base64.b64decode(j["mask_png"]))).size == (640,480)
        await h.check("A006", segment)
        async def error(row, path, body, code, message):
            async def go():
                assert (await h.jpost(path, body, expected=code))["error"] == message
            await h.check(row, go)
        await error("A007", "/api/segment", {}, 400, "the local segmenter (SAM 2.1) needs a point [u, v]; text prompts need segmenter 'meta' (SAM 3.1)")
        await error("A008", "/api/segment", {"segmenter": "meta"}, 400, "the meta segmenter (SAM 3.1) needs a text prompt, e.g. 'red cube'")
        async def off():
            for pt in ([640,480], [-1,-1]):
                j = await h.jpost("/api/segment", {"point": pt}, expected=400)
                assert j["error"] == f"point ({pt[0]:.1f}, {pt[1]:.1f}) is outside the 640 x 480 frame"
        await h.check("A009", off)
        await invalid("A010", "/api/segment", [{"point": [1]}, {"point": [float('nan'), 1]}, {"segmenter": "bad"}])
        async def bare():
            for point in ([10,10], [620,460], [300,400]):
                j = await h.jpost("/api/segment", {"point": point})
                if j["target"] is None:
                    assert j["target_error"] and j["mask_png"]
                    return
            raise AssertionError("no tested bare-table point returned target=null")
        await h.check("A011", bare)
        async def setup(row, path, body):
            async def go():
                j = await h.jpost(path, body, expected=503)
                assert j == {"setup_required": "MODEL_API_KEY", "message": SETUP}
                assert not (await h.jget("/api/status"))["run"]["busy"]
            await h.check(row, go)
        await setup("A012", "/api/segment", {"segmenter":"meta", "text":"red cube"})
        await setup("A014", "/api/plan", {"instruction":"put the red cube in the tray"})
        await invalid("A015", "/api/plan", [{}, {"instruction":""}, {"instruction":"x"*501}])
        await setup("A018", "/api/run", {"instruction":"put the red cube in the tray"})
        await setup("A019", "/api/run", {"steps": steps, "segmenter":"meta"})
        async def sources():
            for body in ({}, {"steps":steps, "instruction":"move"}):
                assert await h.jpost("/api/run", body, expected=422) == {"error":"send either instruction or steps"}
        await h.check("A020", sources)
        async def badplans():
            for ss in ([], steps*7, [{"pick_point":[-1,1], "place_point":drop}], [{"pick_point":[639.6,479.6], "place_point":drop}]):
                assert (await h.jpost("/api/run", {"steps":ss}, expected=422))["error"].startswith("invalid plan:")
            for pt in ([1], [float('inf'),2]):
                assert (await h.jpost("/api/run", {"steps":[{"pick_point":pt,"place_point":drop}]}, expected=422))["detail"]
        await h.check("A021", badplans)
        await invalid("A027", "/api/events", ["?since=bad", "?once=bad"], "GET")
        async def resets():
            await h.post("/api/reset")
            a = await h.jpost("/api/reset", {"seed":0})
            b = await h.jpost("/api/reset", {"seed":0})
            assert a == b and a["scene"] == "classic" and not (await h.jget("/api/status"))["run"]["busy"]
        await h.check("A028", resets)
        async def families():
            for family in FAMILIES:
                j = await h.jpost("/api/reset", {"family":family,"seed":0})
                assert j["scene"] == family and j["seed"] == 0 and j["objects"] and j["instruction"]
        await h.check("A029", families)
        async def unknownfamily():
            a = (await h.jget("/api/status"))["scene"]
            j = await h.jpost("/api/reset", {"family":"unknown"}, expected=422)
            assert j["error"].startswith("unknown family 'unknown'; known:") and all(f in j["error"] for f in FAMILIES)
            assert (await h.jget("/api/status"))["scene"] == a
        await h.check("A030", unknownfamily)
        await invalid("A031", "/api/reset", [{"seed":-1},{"seed":4294967296}])
        async def emptyruns():
            j = await h.jget("/api/runs?limit=1&offset=0")
            assert j == {"runs":[],"total":0,"offset":0,"limit":1,"dir":"HASAMI_RUNS_DIR"}
        await h.check("A032", emptyruns)
        await invalid("A033", "/api/runs", ["?limit=0", "?limit=1001", "?offset=-1"], "GET")
        async def realempty():
            assert (await h.jget("/api/realcam"))["frame_id"] is None
        await h.check("A043", realempty)
        async def grab():
            j = await h.jpost("/api/realcam/grab", expected=503)
            assert j == {"setup_required":"HASAMI_REALCAM", "message":"Setup required: HASAMI_REALCAM (camera index, e.g. 0) and `pip install opencv-python` (the camera is opened only when you ask)"}
        await h.check("A045", grab)
        corners = [[20,20],[300,20],[300,220],[20,220]]
        async def noframe():
            for path, body in (("calibrate",{"corners":corners}), ("segment",{"point":[160,120],"height_mm":25})):
                assert await h.jpost("/api/realcam/"+path, {"frame_id":"absent",**body}, expected=409) == {"error":"grab or upload a frame first","replaced":True}
        await h.check("A053", noframe)
        photo = np.full((240,320,3), 200, np.uint8)
        photo[100:140,140:180] = (210,40,40)
        raw = png_bytes(photo)
        async def upload():
            nonlocal fid
            a = await h.jpost("/api/realcam/upload", data=raw, headers={"Content-Type":"image/png"})
            buf = io.BytesIO(); Image.fromarray(photo).save(buf, format="JPEG")
            b = await h.jpost("/api/realcam/upload", data=buf.getvalue(), headers={"Content-Type":"image/jpeg"})
            assert a["frame_id"] != b["frame_id"] and b["source"] == "upload" and b["calibration"] is None
            assert (b["width"],b["height"]) == (320,240)
            fid = b["frame_id"]
        fid = None
        await h.check("A046", upload)
        h.row = "A043"
        populated = await h.jget("/api/realcam")
        assert populated["frame_id"] == fid and populated["calibration"] is None
        for row, data, code in (("A047",b"",400),("A048",b"not an image",400),("A049",b"x"*15_000_001,413)):
            async def badphoto(data=data,code=code):
                for path in ("/api/realcam/upload","/api/twin"):
                    j = await h.jpost(path,data=data,expected=code,headers={"Content-Type":"image/png"})
                    expected = {"A047":"send a PNG or JPEG photo (under 15 MB) as the body", "A048":"not an image", "A049":"photo too big: send a PNG or JPEG under 15 MB"}[row]
                    assert j == {"error":expected}
                    assert (await h.jget("/api/realcam"))["frame_id"] == fid
            await h.check(row, badphoto)
        async def hugepixels():
            for path in ("/api/realcam/upload","/api/twin"):
                for w in (9000,20000):
                    j = await h.jpost(path,data=header_png(w,w),expected=400,headers={"Content-Type":"image/png"})
                    assert j["error"].startswith("photo too large") and "40 MP" in j["error"]
        await h.check("A050", hugepixels)
        # A real chunked HTTP upload reaches the byte guard without a Content-Length header.
        def chunked_upload(path):
            import http.client
            connection = http.client.HTTPConnection("127.0.0.1", 8420, timeout=30)
            try:
                connection.request("POST", path, body=(b"x" * 800_000 for _ in range(20)),
                                   headers={"X-Hasami":"1", "Content-Type":"image/png"}, encode_chunked=True)
                response = connection.getresponse()
                return {"method":"POST", "path":path, "transport":"HTTP/1.1 chunked (no Content-Length)",
                        "status":response.status, "json":json.loads(response.read())}
            finally:
                connection.close()
        h.row = "A049"
        for path in ("/api/realcam/upload", "/api/twin"):
            chunked = await asyncio.to_thread(chunked_upload, path)
            h.logs[h.row].append(chunked); write(API/f"{h.row}.json", h.logs[h.row])
            assert chunked["status"] == 413 and chunked["json"] == {"error":"photo too big: send a PNG or JPEG under 15 MB"}
        async def realframe():
            for suffix in ("",f"?frame_id={fid}"):
                assert (await h.get("/api/realcam/frame.png"+suffix)).headers["cache-control"] == "no-store"
        await h.check("A051", realframe)
        async def uncalibrated():
            assert await h.jpost("/api/realcam/segment",{"frame_id":fid,"point":[160,120],"height_mm":25},expected=409) == {"error":"grab or upload a frame and click the sheet's corners first"}
        await h.check("A056", uncalibrated)
        async def calibrate():
            for sheet in ("auto","a4","a4-landscape"):
                j = await h.jpost("/api/realcam/calibrate",{"frame_id":fid,"corners":corners,"sheet":sheet,"hfov_deg":None})
                assert j["ok"] and j["frame_id"] == fid and j["requested"] == sheet and j["corners"] == corners
                assert j["parallax"] == "not corrected" and j["hfov_deg"] is None
                assert j["orientation_source"] == ("detected from the clicked corners" if sheet == "auto" else "chosen")
        await h.check("A054", calibrate)
        cal = {"frame_id":fid,"corners":corners}
        await invalid("A055", "/api/realcam/calibrate", [{"corners":corners},{**cal,"corners":corners[:3]}, {**cal,"corners":[[float('nan'),1]]*4}, {**cal,"sheet":"letter"},{**cal,"hfov_deg":19},{**cal,"hfov_deg":151}])
        h.row = "A055"
        deg = await h.jpost("/api/realcam/calibrate", {**cal,"corners":[[1,1]]*4}, expected=400)
        assert deg == {"error":"degenerate points (three or more on a line?)"}
        segment_body = {"frame_id":fid,"point":[160,120],"height_mm":25}
        async def real_segment():
            j = await h.jpost("/api/realcam/segment",segment_body)
            assert j["target_error"] is None and j["arm"] == "sim" and j["frame_id"] == fid and j["mask_png"]
            assert {"x_mm","y_mm","yaw","extent_mm","round","height_mm","parallax_corrected"} <= j["target"].keys()
            # A genuine tiny visual mark (not a replacement mask) exercises the small-mask path.
            tiny = np.full((240,320,3),200,np.uint8); tiny[120,160] = (210,40,40)
            t = await h.jpost("/api/realcam/upload",data=png_bytes(tiny),headers={"Content-Type":"image/png"})
            await h.post("/api/realcam/calibrate",{**cal,"frame_id":t["frame_id"]})
            small = await h.jpost("/api/realcam/segment",{**segment_body,"frame_id":t["frame_id"]})
            assert small["target"] is None and small["target_error"] == "mask too small to locate"
        await h.check("A057", real_segment)
        # Restore the same real photo and calibration after the tiny-mask check.
        h.row="prerequisites"
        fid = (await h.jpost("/api/realcam/upload",data=raw,headers={"Content-Type":"image/png"}))["frame_id"]
        cal["frame_id"] = fid; segment_body["frame_id"] = fid
        await h.post("/api/realcam/calibrate",cal)
        await invalid("A058", "/api/realcam/segment", [{**segment_body,"height_mm":-1},{**segment_body,"height_mm":301},{"frame_id":fid,"point":[1,2]},{"point":[1,2],"height_mm":1},{**segment_body,"point":[1]},{**segment_body,"point":[float('nan'),1]}])
        h.row="A058"
        assert await h.jpost("/api/realcam/segment",{**segment_body,"point":[319.7,0]},expected=400) == {"error":"point (319.7, 0.0) is outside the 320 x 240 frame"}
        async def stale():
            nonlocal fid
            old = fid
            fid = (await h.jpost("/api/realcam/upload",data=raw,headers={"Content-Type":"image/png"}))["frame_id"]
            for path,body in (("calibrate",cal),("segment",segment_body)):
                j = await h.jpost("/api/realcam/"+path,{**body,"frame_id":old},expected=409)
                assert j["replaced"] and j["error"] == "the frame was replaced in another tab (or by another client): this tab now shows the current one"
            assert (await h.jget("/api/realcam/frame.png?frame_id="+old,expected=409))["replaced"]
        await h.check("A052", stale)
        async def clear():
            assert await h.jpost("/api/realcam/clear") == {"cleared":True}
            assert await h.jpost("/api/realcam/clear") == {"cleared":False}
            assert (await h.jget("/api/realcam"))["frame_id"] is None
            assert await h.jget("/api/realcam/frame.png",expected=404) == {"error":"no real-camera frame yet: grab or upload one"}
        await h.check("A044", clear)
        async def latest_empty():
            assert await h.jget("/api/twin") == {"twin":None,"note":"no twin yet (a twin and its photo are kept until evicted or the server stops)"}
        await h.check("A065", latest_empty)
        async def sample():
            nonlocal sample_raw,hfov
            r=await h.get("/api/twin/sample.png?seed=0")
            assert r.headers["x-hasami-photo"] == "synthetic (MuJoCo render)" and r.headers["cache-control"] == "no-store"
            hfov=float(r.headers["x-hasami-hfov"]); sample_raw=await r.body()
            for seed in (-1,10001): await h.get(f"/api/twin/sample.png?seed={seed}",422)
        sample_raw=None;hfov=None
        await h.check("A059",sample)
        async def twinbuild():
            nonlocal twin,tid
            twin=await h.jpost(f"/api/twin?hfov_deg={hfov}&source=synthetic",data=sample_raw,headers={"Content-Type":"image/png"})
            tid=twin["twin_id"]
            assert twin["label"] == "digital twin from one photo · simulated arm" and twin["photo"]["source"] == "synthetic"
            assert {"sheet","objects","stats","tasks","images"} <= twin.keys()
            for name in ("twin.json","photo.png","side_by_side.png"): assert (tmp/"twins"/tid/name).is_file()
        twin=None;tid=None
        await h.check("A060",twinbuild)
        async def querybad():
            for query in ("side=bad","source=bad","hfov_deg=19","hfov_deg=151","offset_mm=99","offset_mm=401","default_height_mm=4","default_height_mm=151","corners=1,2"):
                j=await h.jpost("/api/twin?"+query,data=sample_raw,expected=422,headers={"Content-Type":"image/png"})
                assert j["error"]
        await h.check("A061",querybad)
        async def geometry():
            j=await h.jpost("/api/twin",data=png_bytes(np.zeros((240,320,3),np.uint8)),expected=422,headers={"Content-Type":"image/png"})
            assert j["error"] == "no sheet of paper found (no large bright, unsaturated region); click its corners"
            await h.post("/api/twin?corners=1,1,1,1,1,1,1,1",data=sample_raw,expected=422,headers={"Content-Type":"image/png"})
        await h.check("A062",geometry)
        result("A062","UNTESTED","Real dark-image and degenerate-geometry failures verified; injected IndexError fixture excluded by no-mocks constraint.")
        async def adjust():
            nonlocal twin
            twin=await h.jpost(f"/api/twin/{tid}/adjust",{"heights_mm":{"0":40},"side":"left","hfov_deg":hfov,"default_height_mm":30,"corners":twin["sheet"]["corners_px"]})
            assert twin["twin_id"] == tid and twin["arm"]["side"] == "left"
            assert twin["objects"][0]["size_mm"][2] == 40 and twin["objects"][0]["height_source"] == "typed"
            assert (await h.jget("/api/twin"))["twin"]["twin_id"] == tid
        if tid:
            await h.check("A063",adjust)
            async def badadjust():
                for height in (4,151): assert await h.jpost(f"/api/twin/{tid}/adjust",{"heights_mm":{"0":height}},expected=422) == {"error":"heights must be 5-150 mm"}
                for body in ({"side":"bad"},{"hfov_deg":19},{"hfov_deg":151},{"offset_mm":99},{"offset_mm":401},{"corners":[[1,2]]},{"default_height_mm":4},{"default_height_mm":151}):
                    assert (await h.jpost(f"/api/twin/{tid}/adjust",body,expected=422))["detail"]
            await h.check("A064",badadjust)
            async def twinread():
                j=await h.jget(f"/api/twin/{tid}")
                assert j["twin_id"] == tid and j["images"] and j["objects"] and j["tasks"]
            await h.check("A066",twinread)
            async def twinimgs():
                for name in ("photo","side_by_side"):
                    assert (await h.get(f"/api/twin/{tid}/{name}.png")).headers["cache-control"] == "no-store"
                await h.get(f"/api/twin/{tid}/bad.png",422)
            await h.check("A068",twinimgs)
            async def load():
                j=await h.jpost(f"/api/twin/{tid}/load")
                assert j["scene"] == "twin" and j["twin_id"] == tid
                assert (await h.jget("/api/status"))["scene"]["name"] == "twin"
                await h.get("/api/frame.png")
            await h.check("A069",load)
            result("A069","UNTESTED","Real twin load/status/frame verified; injected scene-load ValueError fixture excluded by no-mocks constraint.")
            async def badtask():
                j=await h.jpost(f"/api/twin/{tid}/run",{"task":"unknown"},expected=422)
                assert j["error"].startswith("this twin has no task 'unknown'; it has ")
                for body in ({},{"task":""},{"task":"x"*41}): await h.post(f"/api/twin/{tid}/run",body,422)
            await h.check("A071",badtask)
        async def missingtwin():
            expected={"error":"no twin 'missing' (a twin lives in server memory until it is evicted or the server stops; upload the photo again)"}
            for path in ("/api/twin/missing","/api/twin/missing/photo.png"):
                assert await h.jget(path,expected=404) == expected
            for path,body in (("adjust",{}),("load",None),("run",{"task":"all-to-tray"})):
                assert await h.jpost("/api/twin/missing/"+path,body,expected=404) == expected
        await h.check("A067",missingtwin)
        # Start one clicked run, test every busy route while it really holds the slot, then read SSE.
        h.row="prerequisites";await h.post("/api/reset",{"seed":0})
        h.row="A017"
        rid=(await h.jpost("/api/run",{"steps":steps}))["run_id"]
        assert (await h.jget("/api/status"))["run"] == {"busy":True,"run_id":rid}
        async def busy_run():
            assert await h.jpost("/api/run",{"steps":steps},expected=409) == {"error":"a run is in progress","run_id":rid}
        await h.check("A022",busy_run)
        async def busyroutes():
            cases=[("/api/segment",{"point":pick}), ("/api/reset",{}), ("/api/realcam/segment",segment_body), ("/api/twin",None)]
            if tid: cases += [(f"/api/twin/{tid}/adjust",{}),(f"/api/twin/{tid}/load",None),(f"/api/twin/{tid}/run",{"task":"all-to-tray"})]
            for path,body in cases:
                started=time.monotonic()
                assert await h.jpost(path,body,expected=409) == {"error":"a run is in progress","run_id":rid}
                assert time.monotonic()-started < 2
        await h.check("A023",busyroutes)
        async def events():
            nonlocal sse,ids
            r=await h.get("/api/events?once=true&since=0")
            assert r.headers["cache-control"] == "no-store" and r.headers["x-accel-buffering"] == "no"
            sse=await r.text();assert sse.startswith("retry: 2000\n\n")
            kinds=re.findall(r"^event: (.+)$",sse,re.M);ids=re.findall(r"^id: (.+)$",sse,re.M)
            assert kinds[0] == "plan" and kinds[-1] == "done" and all(re.fullmatch(r"[a-f0-9]{8}-\d+",i) for i in ids)
            for kind in ("plan","segment","target","phase","verify","result","done"): assert kind in kinds
            assert all(json.loads(d)["run_id"] == rid for d in re.findall(r"^data: (.+)$",sse,re.M))
            done_seq = ids[-1].split("-")[1]
            assert "recorded" in (await (await h.get(f"/api/events?once=true&since={done_seq}")).text())
        sse=None;ids=[]
        await h.check("A025",events)
        result("A017","PASS" if RESULTS["A025"]["status"] == "PASS" else "FAIL","Real clicked run and terminal SSE; record and image checks follow in A034/A038.")
        async def replay():
            for last in ("garbage","99999999","deadbeef-99999999"):
                r=await h.get("/api/events?once=true",headers={"Last-Event-ID":last})
                assert re.findall(r"^event: (.+)$",await r.text(),re.M)[0] == "plan"
            r=await h.get("/api/events?once=true",headers={"Last-Event-ID":ids[0]})
            assert re.findall(r"^id: (.+)$",await r.text(),re.M) == ids[1:]
            # Playwright's request API buffers unbounded streams; read idle SSE with a real page fetch instead.
            browser=await pw.chromium.launch(executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",headless=True)
            try:
                page=await browser.new_page();await page.goto("http://127.0.0.1:8420/")
                beat=await page.evaluate("""async (id) => {const abort=new AbortController();const r=await fetch('/api/events',{headers:{'Last-Event-ID':id},signal:abort.signal});const reader=r.body.getReader();let text='';const timer=setTimeout(()=>abort.abort(),22000);try{while(!text.includes(': keep-alive')){let x=await reader.read();text+=new TextDecoder().decode(x.value)}}finally{clearTimeout(timer);abort.abort()}return {status:r.status,text}}""",ids[-1])
                assert beat["status"] == 200 and ": keep-alive" in beat["text"]
                h.logs[h.row].append({"idle_page_fetch":beat});write(API/f"{h.row}.json",h.logs[h.row])
            finally: await browser.close()
        await h.check("A026",replay)
        async def record():
            nonlocal rec
            rec=await h.jget(f"/api/runs/{rid}")
            assert rec["run_id"] == rid and rec["plan"] and rec["steps"][0]["result"]["ok"] and rec["timings"]
            assert (tmp/"runs"/f"{rid}.json").is_file()
            j=await h.jget("/api/runs?limit=1&offset=0")
            assert len(j["runs"]) == 1 and j["total"] == 1 and j["runs"][0]["run_id"] == rid
        rec=None
        await h.check("A034",record)
        async def missingrun():
            for rr in ("unknown","bad%21"):
                j=await h.jget("/api/runs/"+rr,expected=404);assert j["error"].startswith("no run ")
            await h.get("/api/runs/%2e%2e%2foutside",404)
        await h.check("A035",missingrun)
        async def images():
            for kind in ("frame","after","pick","place","pick_truth","place_truth","view","view_truth"):
                r=await h.get(f"/api/runs/{rid}/steps/0.png?kind={kind}")
                assert r.headers["cache-control"] == "max-age=3600"
                Image.open(io.BytesIO(await r.body())).load()
        await h.check("A038",images)
        await invalid("A039",f"/api/runs/{rid}/steps/0.png",["?kind=unknown","?attempt=0","?attempt=10"],"GET")
        h.row="A039";await h.get(f"/api/runs/{rid}/steps/no.png",422)
        async def missingstep():
            assert await h.jget(f"/api/runs/{rid}/steps/99.png",expected=404) == {"error":f"run {rid} has no step 99"}
            assert await h.jget(f"/api/runs/{rid}/steps/0.png?attempt=9",expected=404) == {"error":"step 0 has no attempt 9"}
        await h.check("A040",missingstep)
        # Damaged/legacy/escaping records are isolated input files, never replacement runtime functions.
        async def damaged():
            (tmp/"runs/damaged.json").write_text('{"run_id":')
            j=await h.jget("/api/runs/damaged",expected=503)
            assert "damaged or still being written" in j["error"] and j["run_id"] == "damaged"
            listing=await h.jget("/api/runs")
            assert next(r for r in listing["runs"] if r["run_id"] == "damaged")["damaged"]
        await h.check("A036",damaged)
        async def oldimages():
            for name,attempts,kind,message in (("old",[],"view","this run was recorded before per-step images"),("noframe",[{"images":{}}],"view","no stored frame for this step"),("nopick",[{"images":{}}],"pick","no pick image stored for this step")):
                write(tmp/"runs"/f"{name}.json",{"run_id":name,"steps":[{"attempts":attempts}]})
                assert await h.jget(f"/api/runs/{name}/steps/0.png?kind={kind}",expected=404) == {"error":message}
        await h.check("A041",oldimages)
        async def containment():
            write(tmp/"runs/escape.json",{"run_id":"escape","steps":[{"attempts":[{"images":{"pick":"../../outside.png"}}]}]})
            assert await h.jget("/api/runs/escape/steps/0.png?kind=pick",expected=404) == {"error":"step image 'pick' is not a file in the run's folder"}
        await h.check("A042",containment)
        async def video():
            import imageio.v2 as imageio
            video=tmp/"runs/local.mp4"
            writer=imageio.get_writer(str(video),fps=5,macro_block_size=1)
            for i in range(5): writer.append_data(np.roll(rgb,i*2,axis=1))
            writer.close()
            write(tmp/"runs/video.json",{"run_id":"video","steps":[],"video":{"file":"local.mp4"}})
            r=await h.get("/api/runs/video/video");assert await r.body() == video.read_bytes()
            r=await h.get("/api/runs/video/video",206,headers={"Range":"bytes=0-1023"})
            assert len(await r.body()) == 1024 and r.headers["content-range"] == f"bytes 0-1023/{video.stat().st_size}"
            for name,file in (("unsafevideo","../../outside.mp4"),("absentvideo","absent.mp4")):
                write(tmp/"runs"/f"{name}.json",{"run_id":name,"steps":[],"video":{"file":file}})
                assert await h.jget(f"/api/runs/{name}/video",expected=404) == {"error":f"run {name} has no video file here"}
        await h.check("A037",video)
        if tid:
            # Return to the default placement so the task tests the measured twin without an edited side.
            h.row="prerequisites";twin=await h.jpost(f"/api/twin/{tid}/adjust",{"side":"far"})
            async def twinrun():
                task=twin["tasks"][0]["id"]
                j=await h.jpost(f"/api/twin/{tid}/run",{"task":task});trid=j["run_id"]
                assert j["task"] == task and j["planner"] and j["instruction"] and j["label"]
                r=await h.get("/api/events?once=true");text=await r.text()
                kinds=re.findall(r"^event: (.+)$",text,re.M)
                assert kinds.index("goal") < kinds.index("done")
                rr=await h.jget(f"/api/runs/{trid}")
                assert rr["twin"]["twin_id"] == tid and rr["twin"]["task"] == task
                goal=next(json.loads(block.split("data: ")[1]) for block in text.split("\n\n") if "event: goal\n" in block)
                assert rr["goal"]["ok"] == goal["ok"] and rr["goal"]["detail"] == goal["detail"]
            await h.check("A070",twinrun)
        async def exclusion():
            # Real scene compilation makes the exclusive interval observable; never sleep/inject in the route.
            h.row="A024"
            one=asyncio.create_task(h.post("/api/reset",{"seed":0,"family":"all-to-tray"}))
            try:
                await asyncio.sleep(.01)
                cases=[("/api/run",{"steps":steps}),("/api/reset",{"seed":1})]
                if tid: cases.append((f"/api/twin/{tid}/load",None))
                responses=await asyncio.gather(*(h.post(path,body,expected=409) for path,body in cases))
                for response in responses:
                    assert (await response.json())["error"] == "the scene is being reset or loaded; try again"
            finally:
                await one
            assert not (await h.jget("/api/status"))["run"]["busy"]
        await h.check("A024",exclusion)
        async def host():
            j=await h.jget("/api/status",expected=400,headers={"Host":"foreign.example"})
            assert j["error"].startswith("unknown Host header 'foreign.example': Hasami answers only on ") and j["error"].endswith("(HASAMI_ALLOWED_HOSTS adds names)")
        await h.check("A072",host)
        async def originreads():
            for method in ("GET","HEAD"):
                r=await h.req(method,"/api/status",403,headers={"Origin":"http://localhost:8888"})
                assert "access-control-allow-origin" not in r.headers
                if method=="GET": assert (await r.json())["error"] == "cross-site read refused: a page on another origin cannot read this API (HASAMI_ALLOWED_ORIGINS adds origins)"
        await h.check("A073",originreads)
        guard="cross-site request refused: writes come from this machine's pages with the X-Hasami: 1 header, or with Content-Type application/json (image/png or image/jpeg for a photo)"
        async def originwrites():
            before=await h.jget("/api/status")
            assert await h.jpost("/api/reset",{"seed":123},expected=403,headers={"Origin":"http://localhost:8888"}) == {"error":guard}
            assert (await h.jget("/api/status"))["scene"] == before["scene"]
        await h.check("A074",originwrites)
        async def noheader():
            for data,ct in ((None,None),("hello","text/plain"),("x=1","application/x-www-form-urlencoded")):
                r=await c.post("/api/reset",data=data,headers={"Content-Type":ct} if ct else {})
                j=await r.json();h.logs.setdefault(h.row,[]).append({"method":"POST","path":"/api/reset","status":r.status,"json":j});write(API/f"{h.row}.json",h.logs[h.row])
                assert r.status == 403 and j == {"error":guard}
            r=await c.post("/api/reset",data='{"seed":0}',headers={"Content-Type":"application/json"});assert r.status==200
            r=await c.post("/api/realcam/upload",data=raw,headers={"Content-Type":"image/png"});assert r.status==200
        await h.check("A075",noheader)
        async def cors():
            for origin in ("http://127.0.0.1:8420","http://localhost:5190","http://localhost:8888"):
                foreign=origin.endswith("8888")
                r=await h.req("OPTIONS","/api/reset",400 if foreign else 200,headers={"Origin":origin,"Access-Control-Request-Method":"POST","Access-Control-Request-Headers":"x-hasami,content-type"})
                assert r.headers.get("access-control-allow-origin") == (None if foreign else origin)
                if foreign: assert await r.text() == "Disallowed CORS origin"
        await h.check("A076",cors)
        async def docs():
            j=await h.jget("/openapi.json");assert j["info"]["title"] == "Hasami"
            assert all(path in j["paths"] for path in ("/api/status","/api/run","/api/twin","/api/realcam/upload"))
            # The schema is large; keep its hash and path inventory, rather than duplicating it in git.
            h.logs[h.row][-1]["json"]={"info":j["info"],"paths":list(j["paths"])};write(API/f"{h.row}.json",h.logs[h.row])
            for path in ("/docs","/redoc","/docs/oauth2-redirect"):
                assert "text/html" in (await h.get(path)).headers["content-type"]
                await h.get(path,400,headers={"Host":"foreign.example"})
                await h.get(path,403,headers={"Origin":"http://localhost:8888"})
        await h.check("A078",docs)
        async def unknown():
            assert await h.jget("/api/not-a-route",expected=404) == {"detail":"Not Found"}
            r=await h.get("/api/reset",405);assert await r.json() == {"detail":"Method Not Allowed"} and "POST" in r.headers["allow"]
        await h.check("A079",unknown)
        for row,reason in (("A013","Needs an injected segmenter/transport failure; no mocks permitted and Meta calls prohibited."),("A016","Needs an injected planner PlanError and generic exception; no mocks permitted and MODEL_API_KEY absent."),("A077","Needs an injected reset/twin RuntimeError; no mocks permitted.")):
            result(row,"UNTESTED",reason)
        stop(p)
        await c.dispose()
        # A second real server verifies the configured port and exact extra-origin allowlist.
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 8422))
        c = await pw.request.new_context(base_url="http://127.0.0.1:8422")
        h.c = c
        p = await start_server(h, tmp, port=8422, extra={"HASAMI_ALLOWED_ORIGINS":"http://localhost:9229"})
        h.row = "A076"
        for origin, allowed in (("http://127.0.0.1:8422",True), ("http://localhost:9229",True), ("http://localhost:9230",False)):
            r = await h.req("OPTIONS", "/api/reset", 200 if allowed else 400,
                            headers={"Origin":origin,"Access-Control-Request-Method":"POST"})
            assert r.headers.get("access-control-allow-origin") == (origin if allowed else None)
        stop(p)
        await c.dispose()


async def mcp_audit(tmp):
    from mcp import ClientSession, StdioServerParameters, stdio_client
    from hasami_core import simulated_click, simulated_drop_clicks
    params=StdioServerParameters(command=sys.executable,args=["-m","hasami_mcp"],cwd=str(tmp),env={"HF_HOME":os.environ["HF_HOME"],"HASAMI_SEED":"5","HASAMI_MCP_LOG":str(CLI/"mcp.jsonl"),"HASAMI_RUNS_DIR":str(tmp/"mcp-runs")})
    (CLI/"mcp.jsonl").unlink(missing_ok=True)
    evidence={}
    def data(r):return r.structured_content or json.loads(r.content[0].text)
    with open(CLI/"mcp-stderr.log","w") as err:
        async with stdio_client(params,errlog=err) as (read,write_stream):
            async with ClientSession(read,write_stream) as s:
                init=await s.initialize();tools=await s.list_tools()
                evidence["initialize"]=init.model_dump(mode="json");evidence["tools"]=tools.model_dump(mode="json")
                assert init.server_info.name == "hasami" and init.server_info.version == "0.1.0"
                assert {t.name for t in tools.tools} == {"status","look","find","pick_place"}
                result("M001")
                r=await s.call_tool("status",{});st=data(r);evidence["status"]=st
                assert not r.is_error and st["arm"]=="sim" and not st["so101"]["connected"] and not st["planner"]["ready"] and st["planner"]["setup"]==SETUP
                assert [o["label"] for o in st["scene"]["objects"]] == ["red cube","green cube","blue cube","tray"]
                result("M002")
                r=await s.call_tool("look",{});img=next(c for c in r.content if c.type=="image");b=base64.b64decode(img.data)
                info=json.loads(next(c.text for c in r.content if c.type=="text"));rgb=np.array(Image.open(io.BytesIO(b)).convert("RGB"))
                evidence["look"]={"image":digest(b),"info":info}
                assert not r.is_error and rgb.shape==(480,640,3) and info["coords"]=="[u, v], u right, v down" and len(info["objects"])==4
                result("M003")
                pick=list(simulated_click(rgb,"blue cube"));drop=list(simulated_drop_clicks(rgb,1)[0])
                r=await s.call_tool("find",{"point":pick,"label":"blue cube"});f=data(r);evidence["find"]=f
                assert not r.is_error and f["target"] and f["mask_pixels"]>0 and f["iou_vs_truth"]>.6 and f["label"]=="blue cube" and "mask_png" not in f
                result("M004")
                evidence["errors"]=[]
                for args,message in (({},"ValueError: give a point [u, v] or a label"),({"point":[1]},"ValueError: point is [u, v]"),({"point":[640,480]},"outside the 640 x 480 frame")):
                    r=await s.call_tool("find",args);text=" ".join(c.text for c in r.content if c.type=="text")
                    evidence["errors"].append({"args":args,"isError":r.is_error,"text":text})
                    assert r.is_error and message in text
                result("M005")
                r=await s.call_tool("find",{"label":"red cube"});text=" ".join(c.text for c in r.content if c.type=="text")
                evidence["setup"]={"isError":r.is_error,"text":text};assert r.is_error and SETUP in text
                result("M006")
                r=await s.call_tool("pick_place",{"pick_point":pick,"place_point":drop});p=data(r);evidence["pick_place"]=p
                assert not r.is_error and p["result"]["ok"] and p["result"]["in_tray"] and p["phases"] and p["pick_iou_vs_truth"]>.6
                record=tmp/"mcp-runs"/f"{p['run_id']}.json";assert record.is_file()
                evidence["record"]={**digest(record.read_bytes()),"run_id":p["run_id"],"result":json.loads(record.read_text())["steps"][0]["result"]}
                result("M007")
    write(CLI/"mcp-client.json",evidence)
    lines=[json.loads(x) for x in (CLI/"mcp.jsonl").read_text().splitlines()]
    assert [l["tool"] for l in lines]==["status","look","find","find","find","find","find","pick_place"]
    assert [l["ok"] for l in lines]==[True,True,True,False,False,False,False,True]
    assert all({"ts","tool","args","ok","ms"}<=l.keys() and ("result" in l or "error" in l) for l in lines)
    result("M008")


def command(row,args,env=None):
    p=subprocess.Popen(args,cwd=ROOT,env=env or os.environ.copy(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
    CHILDREN.append(p)
    out,err=p.communicate(timeout=600)
    write(CLI/f"{row}.json",{"command":args,"exit_code":p.returncode,"stdout":out,"stderr":err})
    return p.returncode,out,err


def cli_audit(tmp):
    env=os.environ.copy();env["HASAMI_WALKER_POSES"]=str(tmp/"poses.json")
    code,out,err=command("C001",[sys.executable,"-m","hasami_walker","poses"],env)
    assert code==0 and out==f"({tmp/'poses.json'})\n"
    write(tmp/"poses.json",{"zeta":[1,2,3,4,5,6],"alpha":[6,5,4,3,2,1]})
    code,out,err=command("C001-populated",[sys.executable,"-m","hasami_walker","poses"],env)
    assert code==0 and out.splitlines()[0].startswith("alpha") and out.splitlines()[1].startswith("zeta")
    result("C001")
    image=tmp/"look.jpg";Image.open(ROOT/"packages/landing/assets/frame-hero.webp").convert("RGB").save(image,format="JPEG",quality=90)
    code,out,err=command("C002",[sys.executable,"-m","hasami_walker","look","red cube","--image",str(image),"--point","353,261","--out",str(tmp/"look.png")],env)
    assert code==0 and "next: reaching it needs the camera-to-arm calibration (CLAUDE.md open item 3); nothing moved" in out
    info=json.loads(out.splitlines()[0]);assert info["how"]=="sam-2.1 point" and info["pixels"]>=150 and (tmp/"look.png").is_file()
    write(CLI/"C002-image.json",{"input":digest(image.read_bytes()),"output":digest((tmp/"look.png").read_bytes()),"size":Image.open(tmp/"look.png").size})
    result("C002")
    code,out,err=command("C003",["bash","scripts/setup.sh","--help"])
    assert code==0 and "--no-dashboard" in out and "--no-weights" in out
    code,out,err=command("C003-syntax",["bash","-n","scripts/setup.sh"]);assert code==0
    result("C003")
    env["HASAMI_RUNS_DIR"]=str(tmp/"fetch-runs")
    code,out,err=command("C004",[sys.executable,"scripts/run_fetch.py","the juice carton","--seed","0","--no-video"],env)
    assert code==0 and SETUP in out and "fetching: juice carton  (from: user words)" in out and "OK: the orange juice carton is in the basket (upright)" in out
    paths=list((tmp/"fetch-runs").glob("*.json"));assert len(paths)==1
    rec=json.loads(paths[0].read_text());res=rec["steps"][0]["result"]
    assert rec["plan"]["source"]=="user words" and res["in_basket"] and res["upright"] and rec["verify"]["agree"]==rec["verify"]["checks"]
    assert not list((tmp/"fetch-runs").rglob("*.mp4"))
    assert all((tmp/"fetch-runs"/rec["run_id"]/v).is_file() for v in rec["steps"][0]["attempts"][0]["images"].values())
    write(CLI/"C004-record.json",rec);result("C004");result("K008")
    code,out,err=command("C005",[sys.executable,"scripts/bench.py","--family","all-to-tray","--seeds","1","--start","0","--planner","clicks","--out",str(tmp/"bench-one.json")],env)
    assert code==0 and "all-to-tray: 1/1 tasks, 3/3 steps" in out
    rec=json.loads((tmp/"bench-one.json").read_text());assert list(rec["families"])==["all-to-tray"] and rec["planner"]=="clicks"
    f=rec["families"]["all-to-tray"];assert f["ran"]==1 and f["success"]==1 and f["seeds"]==[0]
    write(CLI/"C005-record.json",rec);result("C005")
    from hasami_core.fetch import FridgeFetch,HEAD,FETCH_PARK_Q,item_named
    from hasami_sim.scene import fridge_scene
    fetch=FridgeFetch(seed=0)
    understood=fetch.understand("bring me the juice from the fridge")
    assert understood["item"]=="juice carton" and understood["muse"]==SETUP and item_named("bring me the juice from the fridge")=="juice carton"
    before=fetch.arm.data.qpos.copy()
    try:fetch.run("the cheese")
    except ValueError as e:assert str(e)=="no fridge item named in 'the cheese'; the fridge holds: juice carton, can, bottle"
    else:raise AssertionError("unknown item accepted")
    assert np.array_equal(before,fetch.arm.data.qpos)
    fetch.session.load_scene(fridge_scene(0,items=["juice carton","can"]),camera=HEAD,park_q=FETCH_PARK_Q)
    positions={n:fetch.arm.object_pos(n).copy() for n in fetch.session.movables}
    rec=fetch.run("the bottle")
    assert not rec["ok"] and rec["steps"][0]["result"]["failure"]=="segment"
    assert "phases" not in rec["steps"][0]["attempts"][0]
    assert all(np.linalg.norm(fetch.arm.object_pos(n)-p)<.001 for n,p in positions.items())
    write(CLI/"C006.json",{"understand":understood,"unknown_item_recorded_error":"ValueError: no fridge item named in 'the cheese'; the fridge holds: juice carton, can, bottle","qpos_unchanged":True,"missing_item":rec,"positions_unchanged":True})
    result("C006")
    result("C007","UNTESTED","Historical Phase 1 claim cannot be established by this runtime audit; no historical command/phase attestation supplied.")


async def main():
    with tempfile.TemporaryDirectory(prefix="hasami-audit-cli-") as folder:
        tmp=Path(folder);(tmp/"runs").mkdir();(tmp/"twins").mkdir()
        part = os.environ.get("HASAMI_AUDIT_PART", "all")
        if part in ("all", "api"): await api_audit(tmp)
        if part in ("all", "mcp"): await mcp_audit(tmp)
        if part in ("all", "cli"): cli_audit(tmp)
        if part in ("all", "setup"):
            from hasami_planner import Planner, SetupRequired, planner_status
            try:
                Planner()
            except SetupRequired as error:
                assert str(error) == SETUP
                st = planner_status()
                assert st["ready"] is False and st["setup"] == SETUP
                write(CLI/"K001.json", {"exception":"SetupRequired", "message":str(error), "planner_status":st})
                result("K001")
            else:
                raise AssertionError("Planner accepted an absent key")
        if part == "tests":
            code, out, err = command("routing-regression", [sys.executable, "-m", "pytest", "-q", "tests/test_server.py",
                         "-k", "dashboard_mount_preserves_api_method or root_serves_dashboard_or_a_note or dashboard_page_is_revalidated or frame_unknown_camera"])
            assert code == 0, out + err
        if part == "twin-adjust":
            async with async_playwright() as pw:
                context = await pw.request.new_context(base_url="http://127.0.0.1:8420")
                h = HTTP(context)
                p = await start_server(h, tmp)
                try:
                    h.row = "A063"
                    sample = await h.get("/api/twin/sample.png?seed=0")
                    hfov = float(sample.headers["x-hasami-hfov"])
                    twin = await h.jpost(f"/api/twin?source=synthetic&hfov_deg={hfov}", data=await sample.body(),
                                         headers={"Content-Type":"image/png"})
                    tid = twin["twin_id"]
                    corners = [[round(x+2,2), round(y+2,2)] for x,y in twin["sheet"]["corners_px"]]
                    changed = await h.jpost(f"/api/twin/{tid}/adjust", {"heights_mm":{"0":40},"side":"left",
                                              "hfov_deg":hfov+2,"corners":corners,"default_height_mm":30})
                    assert changed["twin_id"] == tid and changed["arm"]["side"] == "left"
                    assert changed["sheet"]["hfov_deg"] == hfov+2 and changed["sheet"]["corners_px"] == corners
                    assert changed["objects"][0]["size_mm"][2] == 40 and changed["objects"][0]["height_source"] == "typed"
                    latest = (await h.jget("/api/twin"))["twin"]
                    assert latest == changed
                    result("A063", note="Changed side, FOV, corner coordinates and default-height option; typed object height is 40 mm and latest twin matches.")
                finally:
                    stop(p)
                    await context.dispose()


if __name__ == "__main__":
    os.chdir(ROOT)
    try:asyncio.run(main())
    finally:cleanup()
