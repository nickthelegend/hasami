"""Hasami HTTP API (FastAPI): REST + server-sent events, and the dashboard build at /.

Contract: specs/hasami.md ("HTTP contract"). One run at a time; a second POST /api/run gets 409.
SSE events: plan, segment, target, phase, verify, retry, result, done, error.
Missing credentials come back as 503 {"setup_required": NAME, "message": ...}, never as a fake result.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from hasami_core import Session, SetupRequired, list_runs, new_run_id, runs_dir, setup_name, status
from hasami_core.session import ROOT
from hasami_planner import Plan, Planner, PlanError, plan_from_clicks

DASHBOARD_DIST = ROOT / "packages" / "dashboard" / "dist"
TERMINAL = {"done", "error"}


# ---- request bodies ------------------------------------------------------------------------
class SegmentReq(BaseModel):
    point: list[float] | None = Field(default=None, min_length=2, max_length=2)
    text: str | None = None
    segmenter: Literal["local", "meta"] = "local"


class PlanReq(BaseModel):
    instruction: str = Field(min_length=1, max_length=500)


class ClickStep(BaseModel):
    pick_point: list[float] = Field(min_length=2, max_length=2)
    place_point: list[float] = Field(min_length=2, max_length=2)
    pick: str | None = None
    place: str | None = None


class RunReq(BaseModel):
    instruction: str | None = Field(default=None, max_length=500)
    steps: list[ClickStep] | None = None
    segmenter: Literal["local", "meta"] = "local"


class CalibrateReq(BaseModel):
    corners: list[list[float]] = Field(min_length=4, max_length=4, description="A4 corners clicked TL, TR, BR, BL")
    sheet: Literal["a4", "a4-landscape"] = "a4"
    hfov_deg: float | None = Field(default=70.0, ge=20, le=150, description="camera field of view; null: no parallax correction")


class RealSegmentReq(BaseModel):
    point: list[float] = Field(min_length=2, max_length=2)
    height_mm: float = Field(ge=0, le=300)


class ResetReq(BaseModel):
    seed: int | None = None
    family: str | None = None  # a benchmark task family (hasami_core.tasks): load its scene for this seed


class TwinAdjustReq(BaseModel):
    heights_mm: dict[int, float] | None = Field(default=None, description="object index -> typed height (mm)")
    hfov_deg: float | None = Field(default=None, ge=20, le=150)
    side: Literal["far", "near", "left", "right"] | None = None
    offset_mm: float | None = Field(default=None, ge=100, le=400)
    corners: list[list[float]] | None = Field(default=None, min_length=4, max_length=4)
    default_height_mm: float | None = Field(default=None, ge=5, le=150)

class TwinRunReq(BaseModel):
    task: str = Field(min_length=1, max_length=40)


# ---- events --------------------------------------------------------------------------------
class EventBus:
    """Thread-safe event log. Runs publish from a worker thread; SSE readers poll by sequence number."""

    def __init__(self, keep: int = 5000):
        self._lock = threading.Lock()
        self._events: list[dict] = []
        self._seq = 0
        self._keep = keep
        self.run_start_seq = 0  # first sequence number of the latest run

    def publish(self, run_id: str, kind: str, data: dict) -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"id": self._seq, "run_id": run_id, "event": kind, "data": data})
            if len(self._events) > self._keep:
                del self._events[: len(self._events) - self._keep]

    def mark_run_start(self) -> None:
        with self._lock:
            self.run_start_seq = self._seq + 1

    def since(self, seq: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["id"] > seq]


class Runner:
    """Owns the single background run."""

    def __init__(self, session: Session, bus: EventBus):
        self.session = session
        self.bus = bus
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.run_id: str | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self, *, plan: Plan | None, instruction: str | None, planner: Planner | None,
              segmenter: str, prepare: Callable[[], Plan | None] | None = None,
              finish: Callable[[dict, Callable[[str, dict], None]], None] | None = None) -> str:
        """Start a run in the background. `prepare` runs first in the worker (e.g. load a twin scene);
        `finish(record, emit)` runs after the arm is done and before the record is written (e.g. a goal check)."""
        with self._lock:
            if self.busy:
                raise RuntimeError("busy")
            run_id = new_run_id()
            self.run_id = run_id
            self.bus.mark_run_start()
            self.thread = threading.Thread(target=self._work,
                                           args=(run_id, plan, instruction, planner, segmenter, prepare, finish),
                                           name=f"hasami-run-{run_id}", daemon=True)
            self.thread.start()
            return run_id

    def _work(self, run_id: str, plan: Plan | None, instruction: str | None, planner: Planner | None,
              segmenter: str, prepare=None, finish=None) -> None:
        def emit(kind: str, data: dict) -> None:
            self.bus.publish(run_id, kind, data)

        try:
            if prepare is not None:
                prepared = prepare()  # may return the plan (it can depend on the scene prepare loads)
                plan = prepared if prepared is not None else plan
            if plan is None:
                assert planner is not None and instruction is not None
                png, _ = self.session.frame_png("overhead")
                labels = [o["label"] for o in self.session.scene_objects()]
                plan, latency_ms = planner.plan(instruction, png, labels)
                emit("planned", {"model": plan.model, "latency_ms": round(latency_ms, 1)})
            if finish is None:
                rec = self.session.run(plan, on_event=emit, segmenter=segmenter, run_id=run_id)
            else:  # hold `done` back until the finish hook has emitted its own events (clients stop at done)
                held: list[dict] = []

                def emit_hold(kind: str, data: dict) -> None:
                    held.append(data) if kind == "done" else emit(kind, data)

                rec = self.session.run(plan, on_event=emit_hold, segmenter=segmenter, run_id=run_id)
                finish(rec, emit)
                for data in held:
                    emit("done", data | {k: rec[k] for k in ("goal",) if k in rec})
            path = self.session.record(rec)
            emit("recorded", {"file": path.name})
        except PlanError as e:
            emit("error", {"run_id": run_id, "message": str(e), "raw": e.raw})
        except SetupRequired as e:
            # Session.run already emitted the error event if it got that far; emit here otherwise.
            if not self._last_is_terminal(run_id):
                emit("error", {"run_id": run_id, "message": str(e), "setup_required": setup_name(e)})
        except Exception as e:  # noqa: BLE001 - surface every failure to the client
            if not self._last_is_terminal(run_id):
                emit("error", {"run_id": run_id, "message": f"{type(e).__name__}: {e}"})
            rec = self.session.last_run
            if rec and rec.get("run_id") == run_id:
                self.session.record(rec)

    def _last_is_terminal(self, run_id: str) -> bool:
        evs = [e for e in self.bus.since(0) if e["run_id"] == run_id]
        return bool(evs) and evs[-1]["event"] in TERMINAL


MASK_TINTS = [(56, 242, 196), (167, 139, 250)]  # pick: mint, place: lilac


def compose_view(frame_path, mask_paths) -> bytes:
    """The frame with each mask tinted (pick mint, place lilac) and outlined."""
    import io

    import numpy as np
    from PIL import Image

    img = np.asarray(Image.open(frame_path).convert("RGB")).astype(np.float32)
    for tint, mp in zip(MASK_TINTS, mask_paths):
        m = np.asarray(Image.open(mp).convert("L")) > 127
        img[m] = 0.45 * img[m] + 0.55 * np.array(tint, np.float32)
        edge = m & ~(np.roll(m, 1, 0) & np.roll(m, -1, 0) & np.roll(m, 1, 1) & np.roll(m, -1, 1))
        img[edge] = tint
    buf = io.BytesIO()
    Image.fromarray(img.clip(0, 255).astype(np.uint8)).save(buf, format="PNG")
    return buf.getvalue()


def _setup_response(e: SetupRequired) -> JSONResponse:
    return JSONResponse(status_code=503, content={"setup_required": setup_name(e), "message": str(e)})


def create_app(session: Session | None = None) -> FastAPI:
    state: dict = {"session": session}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state["session"] is None:
            state["session"] = Session()
        s: Session = state["session"]
        s.snapshot_on_phase = True
        s.store_images = True
        app.state.session = s
        app.state.bus = EventBus()
        app.state.runner = Runner(s, app.state.bus)
        yield
        runner: Runner = app.state.runner
        if runner.thread is not None:
            runner.thread.join(timeout=120)

    app = FastAPI(title="Hasami", version="0.1.0", lifespan=lifespan,
                  description="Muse plans, SAM sees, an SO-101 picks.")
    app.add_middleware(CORSMiddleware, allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
                       allow_methods=["*"], allow_headers=["*"])

    def sess(request: Request) -> Session:
        return request.app.state.session

    def busy_response(request: Request) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "a run is in progress",
                                                      "run_id": request.app.state.runner.run_id})

    @app.get("/api/status")
    def get_status(request: Request) -> dict:
        out = status(sess(request))
        runner: Runner = request.app.state.runner
        out["run"] = {"busy": runner.busy, "run_id": runner.run_id}
        return out

    @app.get("/api/frame.png")
    def get_frame(request: Request, camera: Literal["overhead", "demo"] = "overhead") -> Response:
        png, live = sess(request).frame_png(camera)
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "no-store", "X-Hasami-Live": "1" if live else "0"})

    @app.post("/api/segment")
    def post_segment(body: SegmentReq, request: Request):
        if request.app.state.runner.busy:
            return busy_response(request)
        try:
            seg = sess(request).segment(point=tuple(body.point) if body.point else None, text=body.text,
                                        segmenter=body.segmenter)
        except SetupRequired as e:
            return _setup_response(e)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return seg.to_dict()

    @app.post("/api/plan")
    def post_plan(body: PlanReq, request: Request):
        try:
            planner = Planner()
        except SetupRequired as e:
            return _setup_response(e)
        s = sess(request)
        png, _ = s.frame_png("overhead")
        try:
            plan, latency_ms = planner.plan(body.instruction, png, [o["label"] for o in s.scene_objects()])
        except PlanError as e:
            return JSONResponse(status_code=502, content={"error": str(e), "raw": e.raw})
        except Exception as e:  # noqa: BLE001 - the API call failed; say how
            return JSONResponse(status_code=502, content={"error": f"{type(e).__name__}: {e}"})
        return {"plan": [st.model_dump() for st in plan.steps], "model": plan.model,
                "latency_ms": round(latency_ms, 1)}

    @app.post("/api/run")
    def post_run(body: RunReq, request: Request):
        runner: Runner = request.app.state.runner
        if runner.busy:
            return busy_response(request)
        if (body.instruction is None) == (body.steps is None):
            return JSONResponse(status_code=422, content={"error": "send either instruction or steps"})
        plan = planner = None
        if body.steps is not None:
            try:
                plan = plan_from_clicks([st.model_dump() for st in body.steps])
            except PlanError as e:
                return JSONResponse(status_code=422, content={"error": str(e)})
        else:
            try:
                planner = Planner()
            except SetupRequired as e:
                return _setup_response(e)
        if body.segmenter == "meta":
            try:
                sess(request).segmenter("meta")
            except SetupRequired as e:
                return _setup_response(e)
        try:
            run_id = runner.start(plan=plan, instruction=body.instruction, planner=planner,
                                  segmenter=body.segmenter)
        except RuntimeError:
            return busy_response(request)
        return {"run_id": run_id}

    @app.get("/api/events")
    async def get_events(request: Request, once: bool = Query(False, description="end the stream after the run finishes"),
                         since: int | None = Query(None, description="replay events after this id")):
        bus: EventBus = request.app.state.bus
        last_id = request.headers.get("last-event-id")
        cursor = since if since is not None else (int(last_id) if last_id and last_id.isdigit()
                                                  else bus.run_start_seq - 1)

        async def stream():
            nonlocal cursor
            last_beat = time.monotonic()
            yield "retry: 2000\n\n"
            while True:
                for ev in bus.since(cursor):
                    cursor = ev["id"]
                    payload = json.dumps({"run_id": ev["run_id"], **ev["data"]}, default=str)
                    yield f"id: {ev['id']}\nevent: {ev['event']}\ndata: {payload}\n\n"
                    if once and ev["event"] in TERMINAL:
                        return
                if once and not request.app.state.runner.busy and not bus.since(cursor):
                    # nothing running and nothing left: a finished or never-started run
                    evs = bus.since(0)
                    if not evs or evs[-1]["event"] in TERMINAL or evs[-1]["event"] == "recorded":
                        return
                if await request.is_disconnected():
                    return
                if time.monotonic() - last_beat > 15:
                    last_beat = time.monotonic()
                    yield ": keep-alive\n\n"
                await asyncio.sleep(0.1)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.post("/api/reset")
    def post_reset(request: Request, body: ResetReq | None = None):
        if request.app.state.runner.busy:
            return busy_response(request)
        s = sess(request)
        if body is None or not body.family:
            return s.reset(body.seed if body else None)
        from hasami_core.tasks import FAMILIES, make_task, setup_task

        if body.family not in FAMILIES:
            return JSONResponse(status_code=422, content={"error": f"unknown family {body.family!r}; known: {FAMILIES}"})
        seed = body.seed if body.seed is not None else int(time.time()) % 100000
        task = make_task(body.family, seed)
        try:
            out = setup_task(task, s)
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        return {**out, "scene": body.family, "instruction": task.instruction}

    def _video_path(rec: dict):
        """The run's recorded video file in the runs folder, if it is there (videos are not kept in git)."""
        v = rec.get("video")
        name = v.get("file") if isinstance(v, dict) else v if isinstance(v, str) else None
        if not isinstance(name, str) or not re.fullmatch(r"[0-9A-Za-z_.-]{1,96}\.mp4", name):
            return None
        path = runs_dir() / name
        return path if path.is_file() else None

    @app.get("/api/runs")
    def get_runs() -> dict:
        runs = list_runs()
        for r in runs:
            if r.get("run_id") and _video_path(r) is not None:
                r["video_url"] = f"/api/runs/{r['run_id']}/video"
        return {"runs": runs, "dir": str(runs_dir())}

    @app.get("/api/runs/{run_id}/video")
    def get_run_video(run_id: str):
        """The run's recorded MP4 (scripts/run_demo.py records one), when the file is in the runs folder."""
        from fastapi.responses import FileResponse

        path = runs_dir() / f"{run_id}.json"
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", run_id) or not path.is_file():
            return JSONResponse(status_code=404, content={"error": f"no run {run_id!r}"})
        video = _video_path(json.loads(path.read_text()))
        if video is None:
            return JSONResponse(status_code=404, content={"error": f"run {run_id} has no video file here"})
        return FileResponse(video, media_type="video/mp4")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        path = runs_dir() / f"{run_id}.json"
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", run_id) or not path.is_file():
            return JSONResponse(status_code=404, content={"error": f"no run {run_id!r}"})
        return json.loads(path.read_text())

    @app.get("/api/runs/{run_id}/steps/{step}.png")
    def get_step_png(run_id: str, step: int, attempt: int | None = Query(None, ge=1, le=9),
                     kind: Literal["frame", "after", "pick", "place", "pick_truth", "place_truth", "view",
                                   "view_truth"] = "view"):
        """What the agent saw at a step: the overhead frame (before / after), its SAM masks, the simulator's
        truth masks, or a composite (view: SAM masks over the frame; view_truth: truth masks)."""
        path = runs_dir() / f"{run_id}.json"
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", run_id) or not path.is_file():
            return JSONResponse(status_code=404, content={"error": f"no run {run_id!r}"})
        rec = json.loads(path.read_text())
        steps = rec.get("steps") or []
        if not 0 <= step < len(steps):
            return JSONResponse(status_code=404, content={"error": f"run {run_id} has no step {step}"})
        attempts = steps[step].get("attempts") or []
        if not attempts:
            return JSONResponse(status_code=404, content={"error": "this run was recorded before per-step images"})
        a = attempts[(attempt or len(attempts)) - 1] if (attempt or len(attempts)) <= len(attempts) else None
        if a is None:
            return JSONResponse(status_code=404, content={"error": f"step {step} has no attempt {attempt}"})
        imgs = a.get("images") or {}
        folder = runs_dir() / run_id
        if kind in ("view", "view_truth"):
            if "frame" not in imgs:
                return JSONResponse(status_code=404, content={"error": "no stored frame for this step"})
            suffix = "_truth" if kind == "view_truth" else ""
            png = compose_view(folder / imgs["frame"], [folder / imgs[k] for k in (f"pick{suffix}", f"place{suffix}")
                                                         if k in imgs])
            return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=3600"})
        if kind not in imgs:
            return JSONResponse(status_code=404, content={"error": f"no {kind} image stored for this step"})
        return Response((folder / imgs[kind]).read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "max-age=3600"})

    # ---- real camera (segment + locate only; the arm stays simulated) --------------------------
    real: dict = {"rgb": None, "h": None, "k": None, "source": None}
    REAL_NOTE = "real camera: segment and locate only; the arm is simulated and not connected to this camera"

    @app.post("/api/realcam/grab")
    def realcam_grab():
        from hasami_perception.realcam import grab_frame

        try:
            rgb = grab_frame()
        except SetupRequired as e:
            return _setup_response(e)
        except Exception as e:  # noqa: BLE001
            return JSONResponse(status_code=502, content={"error": f"{type(e).__name__}: {e}"})
        real.update(rgb=rgb, h=None, k=None, source="webcam")
        return {"width": rgb.shape[1], "height": rgb.shape[0], "source": "webcam", "note": REAL_NOTE}

    @app.post("/api/realcam/upload")
    async def realcam_upload(request: Request):
        import io

        import numpy as np
        from PIL import Image, UnidentifiedImageError

        data = await request.body()
        if not data or len(data) > 15_000_000:
            return JSONResponse(status_code=400, content={"error": "send a PNG or JPEG photo (under 15 MB) as the body"})
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except (UnidentifiedImageError, OSError):
            return JSONResponse(status_code=400, content={"error": "not an image"})
        img.thumbnail((1280, 1280))
        real.update(rgb=np.asarray(img).copy(), h=None, k=None, source="upload")
        return {"width": img.width, "height": img.height, "source": "upload", "note": REAL_NOTE}

    @app.get("/api/realcam/frame.png")
    def realcam_frame():
        from hasami_core import png_bytes

        if real["rgb"] is None:
            return JSONResponse(status_code=404, content={"error": "no real-camera frame yet: grab or upload one"})
        return Response(png_bytes(real["rgb"]), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/realcam/calibrate")
    def realcam_calibrate(body: CalibrateReq):
        import numpy as np

        from hasami_perception.realcam import a4_corners, homography, intrinsics_from_fov

        if real["rgb"] is None:
            return JSONResponse(status_code=409, content={"error": "grab or upload a frame first"})
        try:
            h = homography(a4_corners(body.sheet == "a4-landscape"), np.array(body.corners))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        hh, ww = real["rgb"].shape[:2]
        real.update(h=h, k=None if body.hfov_deg is None else intrinsics_from_fov(ww, hh, body.hfov_deg))
        return {"ok": True, "sheet": body.sheet, "hfov_deg": body.hfov_deg,
                "parallax": "corrected with an assumed field of view" if body.hfov_deg else "not corrected"}

    @app.post("/api/realcam/segment")
    def realcam_segment(body: RealSegmentReq, request: Request):
        from hasami_core import mask_png_b64
        from hasami_perception.realcam import locate_on_plane

        if real["rgb"] is None or real["h"] is None:
            return JSONResponse(status_code=409, content={"error": "grab or upload a frame and click the sheet's corners first"})
        hh, ww = real["rgb"].shape[:2]
        if not (0 <= body.point[0] < ww and 0 <= body.point[1] < hh):
            return JSONResponse(status_code=400, content={"error": "point is outside the frame"})
        s = sess(request)
        t0 = time.perf_counter()
        with s.lock:
            mask = s.sam.segment(real["rgb"], point=(body.point[0], body.point[1]))
        ms = (time.perf_counter() - t0) * 1000
        try:
            target = locate_on_plane(mask, real["h"], real["k"], body.height_mm).to_dict()
            err = None
        except ValueError as e:
            target, err = None, str(e)
        return {"mask_png": mask_png_b64(mask), "target": target, "target_error": err, "latency_ms": round(ms, 1),
                "segmenter": s.sam.name, "source": real["source"], "arm": "sim", "note": REAL_NOTE}

    # ---- digital twin from one photo (the arm is simulated) -----------------------------------------
    twins: dict = {}  # id -> {"twin": Twin, "rgb": photo}

    def _twin_or_404(twin_id: str):
        t = twins.get(twin_id)
        if t is None:
            return None, JSONResponse(status_code=404, content={"error": f"no twin {twin_id!r} (twins live until the "
                                                                          "server restarts; upload the photo again)"})
        return t, None

    def _build_twin(request: Request, rgb, opts: dict, twin_id: str | None = None):
        from hasami_core.twin import ArmPlacement, make_twin, save_twin

        s = sess(request)
        try:
            placement = ArmPlacement(opts.get("side") or "far", float(opts.get("offset_mm") or 215.0))
            with s.lock:
                twin = make_twin(rgb, s.sam, corners=opts.get("corners"), hfov_deg=float(opts.get("hfov_deg") or 70.0),
                                 placement=placement, heights_mm=opts.get("heights_mm"),
                                 default_height_mm=opts.get("default_height_mm"), twin_id=twin_id,
                                 photo_source=opts.get("source") or "upload")
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        save_twin(twin, rgb)
        twins[twin.id] = {"twin": twin, "rgb": rgb, "opts": opts}
        return twin.to_dict() | {"images": {"side_by_side": f"/api/twin/{twin.id}/side_by_side.png",
                                            "photo": f"/api/twin/{twin.id}/photo.png"}}

    @app.get("/api/twin/sample.png")
    def twin_sample(seed: int = Query(0, ge=0, le=10_000)):
        """A synthetic desk photo rendered by MuJoCo (A4 sheet, 3-5 objects, a random tilted camera; no arm) to
        try the twin without a camera. Its field of view is in X-Hasami-Hfov. Labelled synthetic wherever used."""
        from hasami_core import png_bytes
        from hasami_sim.photo import SheetPose, random_desk, render_photo

        objs, cam = random_desk(seed)
        return Response(png_bytes(render_photo(objs, SheetPose(), cam)), media_type="image/png",
                        headers={"Cache-Control": "no-store", "X-Hasami-Hfov": f"{cam.hfov_deg:.3f}",
                                 "X-Hasami-Photo": "synthetic (MuJoCo render)",
                                 "Access-Control-Expose-Headers": "X-Hasami-Hfov, X-Hasami-Photo"})

    @app.post("/api/twin")
    async def post_twin(request: Request, hfov_deg: float = Query(70.0, ge=20, le=150),
                        side: Literal["far", "near", "left", "right"] = "far",
                        offset_mm: float = Query(215.0, ge=100, le=400),
                        default_height_mm: float | None = Query(None, ge=5, le=150),
                        corners: str | None = Query(None, description="u,v,u,v,u,v,u,v: the sheet's corners (else found)"),
                        source: Literal["upload", "synthetic"] = Query("upload", description="synthetic: a /api/twin/sample.png render")):
        """A photo of a desk with an A4 sheet on it (PNG or JPEG body) -> a digital twin the simulated arm can
        work in. The photo is never sent anywhere; SAM 2.1 runs locally."""
        import io

        import numpy as np
        from PIL import Image, UnidentifiedImageError

        if request.app.state.runner.busy:
            return busy_response(request)
        data = await request.body()
        if not data or len(data) > 15_000_000:
            return JSONResponse(status_code=400, content={"error": "send a PNG or JPEG photo (under 15 MB) as the body"})
        try:
            img = Image.open(io.BytesIO(data)).convert("RGB")
        except (UnidentifiedImageError, OSError):
            return JSONResponse(status_code=400, content={"error": "not an image"})
        img.thumbnail((1280, 1280))
        pts = None
        if corners:
            try:
                v = [float(x) for x in corners.split(",")]
                if len(v) != 8:
                    raise ValueError
                pts = [v[i:i + 2] for i in range(0, 8, 2)]
            except ValueError:
                return JSONResponse(status_code=422, content={"error": "corners: eight numbers u,v,u,v,u,v,u,v"})
        opts = {"hfov_deg": hfov_deg, "side": side, "offset_mm": offset_mm, "default_height_mm": default_height_mm,
                "corners": pts, "source": source}
        return _build_twin(request, np.asarray(img).copy(), opts)

    @app.post("/api/twin/{twin_id}/adjust")
    def adjust_twin(twin_id: str, body: TwinAdjustReq, request: Request):
        """Rebuild a twin from its photo with typed heights, clicked corners, another field of view or arm side."""
        t, err = _twin_or_404(twin_id)
        if err:
            return err
        if request.app.state.runner.busy:
            return busy_response(request)
        if body.heights_mm and any(not 5 <= h <= 150 for h in body.heights_mm.values()):
            return JSONResponse(status_code=422, content={"error": "heights must be 5-150 mm"})
        opts = dict(t["opts"])
        for k, v in body.model_dump().items():
            if v is not None:
                opts[k] = v
        return _build_twin(request, t["rgb"], opts, twin_id=twin_id)

    @app.get("/api/twin/{twin_id}")
    def get_twin(twin_id: str):
        t, err = _twin_or_404(twin_id)
        return err or t["twin"].to_dict()

    @app.get("/api/twin/{twin_id}/{name}.png")
    def get_twin_png(twin_id: str, name: Literal["photo", "side_by_side"]):
        from hasami_core.twin import twins_dir

        t, err = _twin_or_404(twin_id)
        if err:
            return err
        return Response((twins_dir() / twin_id / f"{name}.png").read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/twin/{twin_id}/load")
    def load_twin(twin_id: str, request: Request):
        """Put the twin in the simulator (the live camera then shows it; click-to-pick works on it)."""
        t, err = _twin_or_404(twin_id)
        if err:
            return err
        if request.app.state.runner.busy:
            return busy_response(request)
        try:
            out = sess(request).load_scene(t["twin"].spec)
        except ValueError as e:
            return JSONResponse(status_code=422, content={"error": str(e)})
        return {**out, "scene": "twin", "twin_id": twin_id, "label": t["twin"].label}

    @app.post("/api/twin/{twin_id}/run")
    def run_twin(twin_id: str, body: TwinRunReq, request: Request):
        """Run one of the twin's tasks: the twin is loaded, the simulated-click oracle plans the steps, the
        arm runs them with verify + retry, and the goal predicate is checked on the twin's physics (SSE as
        for /api/run, plus a `goal` event; the run record carries `twin` and `goal`)."""
        from hasami_core.tasks import ORACLE_LABEL, oracle_steps, world_from
        from hasami_core.twin import TWIN_LABEL, twin_tasks

        t, err = _twin_or_404(twin_id)
        if err:
            return err
        runner: Runner = request.app.state.runner
        if runner.busy:
            return busy_response(request)
        tasks = twin_tasks(t["twin"])
        task = tasks.get(body.task)
        if task is None:
            return JSONResponse(status_code=422, content={"error": f"this twin has no task {body.task!r}; it has "
                                                                   f"{sorted(tasks) or 'none (no reachable, graspable object)'}"})
        s = sess(request)
        state: dict = {}

        def prepare():
            s.load_scene(t["twin"].spec)
            state["w0"] = world_from(s)
            plan = plan_from_clicks(oracle_steps(task, s))
            plan.instruction = task.instruction
            return plan

        def finish(rec: dict, emit) -> None:
            ok, detail = task.goal(state["w0"], world_from(s))
            rec["twin"] = {"twin_id": twin_id, "task": body.task, "instruction": task.instruction,
                           "label": TWIN_LABEL, "planner": ORACLE_LABEL}
            rec["goal"] = {"ok": ok, "detail": detail}
            emit("goal", {"run_id": rec["run_id"], "ok": ok, "detail": detail, "task": body.task,
                          "label": TWIN_LABEL})

        try:
            run_id = runner.start(plan=None, instruction=task.instruction, planner=None, segmenter="local",
                                  prepare=prepare, finish=finish)
        except RuntimeError:
            return busy_response(request)
        return {"run_id": run_id, "task": body.task, "instruction": task.instruction, "label": TWIN_LABEL,
                "planner": ORACLE_LABEL}

    if DASHBOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    else:
        @app.get("/")
        def root() -> dict:
            return {"name": "hasami", "api": "/api/status",
                    "note": f"Dashboard not built: run `npm run build` in packages/dashboard (expected {DASHBOARD_DIST})."}

    return app


app = create_app()


def serve(host: str | None = None, port: int | None = None) -> None:
    import os

    import uvicorn

    uvicorn.run(app, host=host or os.environ.get("HASAMI_HOST", "127.0.0.1"),
                port=port or int(os.environ.get("HASAMI_PORT", "8420")), log_level="info")


__all__ = ["EventBus", "Runner", "app", "create_app", "serve"]
