"""Hasami HTTP API (FastAPI): REST + server-sent events, and the dashboard build at /.

Contract: specs/hasami.md ("HTTP contract"). One run at a time; a second POST /api/run gets 409.
Missing credentials come back as 503 {"setup_required": NAME, "message": ...}, never as a fake result.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
import time
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


class ResetReq(BaseModel):
    seed: int | None = None


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
              segmenter: str) -> str:
        with self._lock:
            if self.busy:
                raise RuntimeError("busy")
            run_id = new_run_id()
            self.run_id = run_id
            self.bus.mark_run_start()
            self.thread = threading.Thread(target=self._work, args=(run_id, plan, instruction, planner, segmenter),
                                           name=f"hasami-run-{run_id}", daemon=True)
            self.thread.start()
            return run_id

    def _work(self, run_id: str, plan: Plan | None, instruction: str | None, planner: Planner | None,
              segmenter: str) -> None:
        def emit(kind: str, data: dict) -> None:
            self.bus.publish(run_id, kind, data)

        try:
            if plan is None:
                assert planner is not None and instruction is not None
                png, _ = self.session.frame_png("overhead")
                labels = [o["label"] for o in self.session.scene_objects()]
                plan, latency_ms = planner.plan(instruction, png, labels)
                emit("planned", {"model": plan.model, "latency_ms": round(latency_ms, 1)})
            rec = self.session.run(plan, on_event=emit, segmenter=segmenter, run_id=run_id)
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
        return sess(request).reset(body.seed if body else None)

    @app.get("/api/runs")
    def get_runs() -> dict:
        return {"runs": list_runs(), "dir": str(runs_dir())}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        path = runs_dir() / f"{run_id}.json"
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", run_id) or not path.is_file():
            return JSONResponse(status_code=404, content={"error": f"no run {run_id!r}"})
        return json.loads(path.read_text())

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
