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
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from hasami_core import Session, SetupRequired, list_runs, new_run_id, runs_dir, setup_name, status
from hasami_core.session import ROOT
from hasami_planner import Plan, Planner, PlanError, plan_from_clicks

DASHBOARD_DIST = ROOT / "packages" / "dashboard" / "dist"
TERMINAL = {"done", "error"}

# ---- local only: DNS rebinding and cross-site writes ---------------------------------------------------
LOCAL_HOSTS = ("127.0.0.1", "localhost", "[::1]")
DEV_PORT = 5190  # packages/dashboard: the Vite dev server, which proxies /api here
WRITE_TYPES = {"application/json", "image/png", "image/jpeg"}  # none is CORS-safelisted: a preflight comes first
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_name(host: str) -> str:
    """The name in a Host header, without its port ("[::1]:8420" -> "[::1]", "localhost:8420" -> "localhost")."""
    host = host.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end > 0 else host
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LocalOnly:
    """ASGI guard in front of everything. Any request whose Host is not this machine's (DNS rebinding: a page on
    another name that resolves to 127.0.0.1) gets 400. A GET / HEAD naming a foreign Origin gets 403 (so a proxy
    or dev server that relaxes CORS cannot hand a foreign page the API's answers). A write (any method but GET / HEAD / OPTIONS) gets 403 when
    its Origin is not one of `allowed_origins` (the dashboard's own origin and its dev server: another app on another
    localhost port is foreign too), or when it carries neither the X-Hasami header nor a JSON / PNG / JPEG
    Content-Type: a foreign page can send neither without a CORS preflight, which only those origins pass."""

    def __init__(self, app, allowed_hosts, allowed_origins):
        self.app = app
        self.allowed = {h.strip().lower() for h in allowed_hosts if h.strip()}
        self.origins = {o.strip().lower().rstrip("/") for o in allowed_origins if o.strip()}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        host = _host_name(headers.get("host", ""))
        if host not in self.allowed:
            msg = (f"unknown Host header {host!r}: Hasami answers only on {', '.join(sorted(self.allowed))} "
                   "(HASAMI_ALLOWED_HOSTS adds names)")
            return await JSONResponse(status_code=400, content={"error": msg})(scope, receive, send)
        origin = headers.get("origin")
        foreign = origin is not None and origin.strip().lower().rstrip("/") not in self.origins
        if foreign and scope["method"] in ("GET", "HEAD"):
            # A browser names its Origin on a cross-origin read only; the dashboard's own reads are same-origin.
            msg = ("cross-site read refused: a page on another origin cannot read this API "
                   "(HASAMI_ALLOWED_ORIGINS adds origins)")
            return await JSONResponse(status_code=403, content={"error": msg})(scope, receive, send)
        if scope["method"] not in SAFE_METHODS:
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if foreign or (headers.get("x-hasami") != "1" and ctype not in WRITE_TYPES):
                msg = ("cross-site request refused: writes come from this machine's pages with the X-Hasami: 1 "
                       "header, or with Content-Type application/json (image/png or image/jpeg for a photo)")
                return await JSONResponse(status_code=403, content={"error": msg})(scope, receive, send)
        await self.app(scope, receive, send)


# ---- request bodies ------------------------------------------------------------------------
Num = Annotated[float, Field(allow_inf_nan=False)]  # NaN / Infinity in JSON is refused (422)
Pair = Annotated[list[Num], Field(min_length=2, max_length=2)]  # a pixel [u, v]


class SegmentReq(BaseModel):
    point: Pair | None = None
    text: str | None = None
    segmenter: Literal["local", "meta"] = "local"


class PlanReq(BaseModel):
    instruction: str = Field(min_length=1, max_length=500)


class ClickStep(BaseModel):
    pick_point: Pair
    place_point: Pair
    pick: str | None = None
    place: str | None = None


class RunReq(BaseModel):
    instruction: str | None = Field(default=None, max_length=500)
    steps: list[ClickStep] | None = None
    segmenter: Literal["local", "meta"] = "local"


class CalibrateReq(BaseModel):
    corners: list[Pair] = Field(min_length=4, max_length=4, description="A4 corners in click order (TL, TR, BR, BL as seen)")
    sheet: Literal["a4", "a4-landscape", "auto"] = Field(
        default="a4", description="auto: portrait or landscape, whichever gives a camera pose that is a rotation")
    hfov_deg: float | None = Field(default=70.0, ge=20, le=150, description="camera field of view; null: no parallax correction")


class RealSegmentReq(BaseModel):
    point: Pair
    height_mm: float = Field(ge=0, le=300)


class ResetReq(BaseModel):
    seed: int | None = Field(default=None, ge=0, le=2**32 - 1)  # numpy seeds are non-negative; 32 bits stay exact in JS
    family: str | None = None  # a benchmark task family (hasami_core.tasks): load its scene for this seed


class TwinAdjustReq(BaseModel):
    heights_mm: dict[int, float] | None = Field(default=None, description="object index -> typed height (mm)")
    hfov_deg: float | None = Field(default=None, ge=20, le=150)
    side: Literal["far", "near", "left", "right"] | None = None
    offset_mm: float | None = Field(default=None, ge=100, le=400)
    corners: list[Pair] | None = Field(default=None, min_length=4, max_length=4)
    default_height_mm: float | None = Field(default=None, ge=5, le=150)

class TwinRunReq(BaseModel):
    task: str = Field(min_length=1, max_length=40)


# ---- events --------------------------------------------------------------------------------
class EventBus:
    """Thread-safe event log. Runs publish from a worker thread; SSE readers poll by sequence number.
    `boot` is new for every server process: SSE ids are "<boot>-<seq>", so a browser reconnecting with the
    Last-Event-ID of an earlier process (whose numbers mean nothing here) is not mistaken for a cursor."""

    def __init__(self, keep: int = 5000):
        import secrets

        self.boot = secrets.token_hex(4)
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

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def cursor_from(self, last_event_id: str | None) -> int:
        """Where a stream resumes. A Last-Event-ID from this process ("<boot>-<seq>", seq not ahead of the log)
        resumes after it; anything else (none, another process's id, a bare or future number, garbage) starts
        at the latest run's first event, so a reconnect after a server restart still sees the current run."""
        m = re.fullmatch(r"([0-9a-f]{8})-(\d{1,18})", (last_event_id or "").strip())
        if m and m.group(1) == self.boot and int(m.group(2)) <= self.seq:
            return int(m.group(2))
        return self.run_start_seq - 1


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

    @contextmanager
    def exclusive(self):
        """Hold the run slot while the scene changes (reset, twin load): RuntimeError("busy") when a run is going or
        another change holds it, and no run can start until the block ends (start() refuses rather than wait)."""
        if not self._lock.acquire(blocking=False):
            raise RuntimeError("scene")
        try:
            if self.busy:
                raise RuntimeError("busy")
            yield
        finally:
            self._lock.release()

    def start(self, *, plan: Plan | None, instruction: str | None, planner: Planner | None,
              segmenter: str, prepare: Callable[[], Plan | None] | None = None,
              finish: Callable[[dict, Callable[[str, dict], None]], None] | None = None) -> str:
        """Start a run in the background. `prepare` runs first in the worker (e.g. load a twin scene);
        `finish(record, emit)` runs after the arm is done and before the record is written (e.g. a goal check)."""
        if not self._lock.acquire(blocking=False):  # the scene is being reset or loaded: its pixels are stale
            raise RuntimeError("scene")
        try:
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
        finally:
            self._lock.release()

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


# ---- photo bodies (real camera, twin) ------------------------------------------------------------------
MAX_PHOTO_BYTES = 15_000_000
MAX_PHOTO_PIXELS = 40_000_000
PHOTO_SIDE = 1280  # photos are scaled down to fit 1280 x 1280


class PhotoError(ValueError):
    """A photo body that cannot be used; the message is shown to the user."""


async def read_photo_body(request: Request) -> bytes | JSONResponse:
    """The request body, at most 15 MB, read as it streams in (413 above it, without buffering the rest)."""
    too_big = JSONResponse(status_code=413, content={"error": "photo too big: send a PNG or JPEG under 15 MB"})
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_PHOTO_BYTES:
        return too_big
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > MAX_PHOTO_BYTES:
            return too_big
    if not buf:
        return JSONResponse(status_code=400, content={"error": "send a PNG or JPEG photo (under 15 MB) as the body"})
    return bytes(buf)


def decode_photo(data: bytes):
    """PNG / JPEG bytes -> an RGB array that fits 1280 x 1280. The pixel count is checked from the header before
    anything is decoded (decompression bombs: over 40 MP is refused); JPEGs decode at a reduced scale.
    Blocking (PIL): call it from a worker thread."""
    import io
    import warnings

    import numpy as np
    from PIL import Image, UnidentifiedImageError

    def too_large(w: int, h: int) -> PhotoError:
        return PhotoError(f"photo too large: {w} x {h} px ({w * h / 1e6:.0f} MP); send one under "
                          f"{MAX_PHOTO_PIXELS // 1_000_000} MP")

    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            img = Image.open(io.BytesIO(data))
            w, h = img.size
            if w * h > MAX_PHOTO_PIXELS:
                raise too_large(w, h)
            img.draft("RGB", (PHOTO_SIDE, PHOTO_SIDE))  # JPEG: let the decoder scale down (no-op for PNG)
            img = img.convert("RGB")
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
            m = re.search(r"\((\d+) pixels\)", str(e))
            raise PhotoError(f"photo too large ({int(m.group(1)) / 1e6:.0f} MP); send one under "
                             f"{MAX_PHOTO_PIXELS // 1_000_000} MP" if m else "photo too large (MP)") from e
        except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
            if isinstance(e, PhotoError):
                raise
            raise PhotoError("not an image") from e
    img.thumbnail((PHOTO_SIDE, PHOTO_SIDE))
    return np.asarray(img).copy()


def allowed_hosts_from_env() -> list[str]:
    import os

    extra = [h for h in os.environ.get("HASAMI_ALLOWED_HOSTS", "").split(",") if h.strip()]
    return [*LOCAL_HOSTS, *extra]


def allowed_origins_from_env() -> list[str]:
    """The only origins whose pages may call the API from a browser: this server's own (HASAMI_PORT, 8420) and the
    dashboard's Vite dev server (5190), on 127.0.0.1, localhost and [::1], plus HASAMI_ALLOWED_ORIGINS (comma
    separated). Not every localhost port: another local app's page is a foreign site."""
    import os

    port = os.environ.get("HASAMI_PORT", "8420").strip()
    ports = {p for p in ("8420", port if port.isdigit() else "8420", str(DEV_PORT))}
    own = [f"http://{h}:{p}" for h in LOCAL_HOSTS for p in sorted(ports)]
    extra = [o.strip() for o in os.environ.get("HASAMI_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return [*own, *extra]


TWIN_ID = re.compile(r"[0-9A-Za-z_-]{1,64}")


def prune_twin_folders(folder) -> list[str]:
    """Delete the twin folders (<id>/ holding a twin.json) in `folder`: at startup no twin is in memory, so any
    there were left by an earlier process, and a desk photo must not outlive the twin. Other files stay."""
    import shutil
    from pathlib import Path

    gone = []
    folder = Path(folder)
    if folder.is_dir():
        for d in folder.iterdir():
            if d.is_dir() and not d.is_symlink() and TWIN_ID.fullmatch(d.name) and (d / "twin.json").is_file():
                shutil.rmtree(d, ignore_errors=True)
                gone.append(d.name)
    return gone


def create_app(session: Session | None = None, allowed_hosts: list[str] | None = None,
               allowed_origins: list[str] | None = None) -> FastAPI:
    state: dict = {"session": session}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import os
        import shutil
        import tempfile
        from pathlib import Path

        if state["session"] is None:
            state["session"] = Session()
        s: Session = state["session"]
        s.snapshot_on_phase = True
        s.store_images = True
        app.state.session = s
        app.state.bus = EventBus()
        app.state.runner = Runner(s, app.state.bus)
        # Twins (with the desk photo) are written to HASAMI_TWINS_DIR, whose old twin folders are pruned now, or
        # else to a temporary folder that is removed at shutdown: never kept past the server's own memory of them.
        if os.environ.get("HASAMI_TWINS_DIR"):
            prune_twin_folders(os.environ["HASAMI_TWINS_DIR"])
        app.state.twins_tmp = Path(tempfile.mkdtemp(prefix="hasami-twins-"))
        try:
            yield
        finally:
            runner: Runner = app.state.runner
            if runner.thread is not None:
                runner.thread.join(timeout=120)
            shutil.rmtree(app.state.twins_tmp, ignore_errors=True)

    app = FastAPI(title="Hasami", version="0.1.0", lifespan=lifespan,
                  description="SAM 2.1 sees, a simulated SO-101 picks; Muse plans once MODEL_API_KEY is set "
                              "(Setup required until then).")
    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, e: RequestValidationError) -> JSONResponse:
        """FastAPI's 422, without echoing the input back (a NaN in it could not even be written as JSON)."""
        errs = [{k: err[k] for k in ("type", "loc", "msg") if k in err} for err in e.errors()]
        summary = "; ".join(f"{'.'.join(str(x) for x in err.get('loc', ())[1:]) or 'body'}: {err.get('msg', '')}"
                            for err in errs)
        return JSONResponse(status_code=422, content={"error": summary, "detail": errs})

    @app.exception_handler(Exception)
    async def unhandled(request: Request, e: Exception) -> JSONResponse:
        """Anything not caught on purpose still answers JSON that says what failed (and is logged)."""
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})

    origins = allowed_origins or allowed_origins_from_env()
    # an exact list, never a localhost pattern: the dashboard is same-origin (or behind the dev server's proxy)
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])
    app.add_middleware(LocalOnly, allowed_hosts=allowed_hosts or allowed_hosts_from_env(),
                       allowed_origins=origins)  # outermost

    def sess(request: Request) -> Session:
        return request.app.state.session

    def runner_busy() -> bool:
        runner = getattr(app.state, "runner", None)
        return bool(runner and runner.busy)

    def busy_response(request: Request, why: str = "busy") -> JSONResponse:
        """409: a run holds the arm, or (why == "scene") the scene is being reset or loaded right now."""
        msg = "the scene is being reset or loaded; try again" if why == "scene" else "a run is in progress"
        return JSONResponse(status_code=409, content={"error": msg, "run_id": request.app.state.runner.run_id})

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
        except (ValueError, IndexError) as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        except Exception as e:  # noqa: BLE001 - the segmenter failed (e.g. SAM 3.1 unreachable); say how
            return JSONResponse(status_code=502, content={"error": f"{type(e).__name__}: {e}"})
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
        except RuntimeError as e:
            return busy_response(request, str(e))
        return {"run_id": run_id}

    @app.get("/api/events")
    async def get_events(request: Request, once: bool = Query(False, description="end the stream after the run finishes"),
                         since: int | None = Query(None, description="replay events after this sequence number (an SSE id is <boot>-<seq>)")):
        bus: EventBus = request.app.state.bus
        cursor = since if since is not None else bus.cursor_from(request.headers.get("last-event-id"))

        async def stream():
            nonlocal cursor
            last_beat = time.monotonic()
            yield "retry: 2000\n\n"
            while True:
                for ev in bus.since(cursor):
                    cursor = ev["id"]
                    payload = json.dumps({"run_id": ev["run_id"], **ev["data"]}, default=str)
                    yield f"id: {bus.boot}-{ev['id']}\nevent: {ev['event']}\ndata: {payload}\n\n"
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
        from hasami_core.tasks import FAMILIES, make_task, setup_task

        if body is not None and body.family and body.family not in FAMILIES:
            return JSONResponse(status_code=422, content={"error": f"unknown family {body.family!r}; known: {FAMILIES}"})
        s = sess(request)
        try:
            with request.app.state.runner.exclusive():  # no run starts on the old scene's pixels meanwhile
                if body is None or not body.family:
                    return s.reset(body.seed if body else None)
                seed = body.seed if body.seed is not None else int(time.time()) % 100000
                try:
                    out = setup_task(task := make_task(body.family, seed), s)
                except ValueError as e:
                    return JSONResponse(status_code=422, content={"error": str(e)})
                return {**out, "scene": body.family, "instruction": task.instruction}
        except RuntimeError as e:
            return busy_response(request, str(e))

    def _record(run_id: str):
        """(record, None) for a run in the runs folder, else (None, a 404 / 503 response)."""
        from hasami_core.session import load_run_record

        path = runs_dir() / f"{run_id}.json"
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", run_id) or not path.is_file():
            return None, JSONResponse(status_code=404, content={"error": f"no run {run_id!r}"})
        try:
            return load_run_record(path), None
        except (OSError, ValueError) as e:
            return None, JSONResponse(status_code=503, content={"error": str(e), "run_id": run_id})

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

        rec, err = _record(run_id)
        if err:
            return err
        video = _video_path(rec)
        if video is None:
            return JSONResponse(status_code=404, content={"error": f"run {run_id} has no video file here"})
        return FileResponse(video, media_type="video/mp4")

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        rec, err = _record(run_id)
        return err or rec

    @app.get("/api/runs/{run_id}/steps/{step}.png")
    def get_step_png(run_id: str, step: int, attempt: int | None = Query(None, ge=1, le=9),
                     kind: Literal["frame", "after", "pick", "place", "pick_truth", "place_truth", "view",
                                   "view_truth"] = "view"):
        """What the agent saw at a step: the overhead frame (before / after), its SAM masks, the simulator's
        truth masks, or a composite (view: SAM masks over the frame; view_truth: truth masks)."""
        rec, err = _record(run_id)
        if err:
            return err
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
        data = await read_photo_body(request)
        if isinstance(data, JSONResponse):
            return data
        try:
            rgb = await run_in_threadpool(decode_photo, data)  # PIL off the event loop
        except PhotoError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        real.update(rgb=rgb, h=None, k=None, source="upload")
        return {"width": rgb.shape[1], "height": rgb.shape[0], "source": "upload", "note": REAL_NOTE}

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
        hh, ww = real["rgb"].shape[:2]
        try:
            if body.sheet == "auto":
                from hasami_perception.twin import pick_orientation

                # the same test the twin uses (fit both ways the sheet can lie, keep the one whose pose is a
                # rotation), on the corners in the order they were clicked: that is the order H is fitted on,
                # so "landscape" means edge 1 -> 2 is the long side whichever corner the user started at
                landscape, h, _ = pick_orientation(body.corners, ww, hh, body.hfov_deg or 70.0)
            else:
                landscape = body.sheet == "a4-landscape"
                h = homography(a4_corners(landscape), np.array(body.corners))
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        real.update(h=h, k=None if body.hfov_deg is None else intrinsics_from_fov(ww, hh, body.hfov_deg))
        return {"ok": True, "sheet": "a4-landscape" if landscape else "a4",
                "orientation": "landscape" if landscape else "portrait",
                "orientation_source": "detected from the clicked corners" if body.sheet == "auto" else "chosen",
                "size_mm": [297.0, 210.0] if landscape else [210.0, 297.0], "hfov_deg": body.hfov_deg,
                "parallax": "corrected with an assumed field of view" if body.hfov_deg else "not corrected"}

    @app.post("/api/realcam/segment")
    def realcam_segment(body: RealSegmentReq, request: Request):
        from hasami_core import mask_png_b64
        from hasami_perception.realcam import locate_on_plane

        if request.app.state.runner.busy:  # SAM 2.1 is the run's: answer now rather than wait for the run to end
            return busy_response(request)
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
    twins: dict = {}  # id -> {"twin": Twin, "rgb": photo}; insertion order = last built, adjusted or loaded last
    twins_lock = threading.Lock()  # builds run in worker threads
    twin_run: dict = {"id": None}  # the twin the current (or last) twin run works in

    def _twins_dir():
        """HASAMI_TWINS_DIR, else this process's temporary twins folder (removed at shutdown)."""
        import os
        from pathlib import Path

        return Path(os.environ["HASAMI_TWINS_DIR"]) if os.environ.get("HASAMI_TWINS_DIR") else app.state.twins_tmp

    def _max_twins() -> int:
        import os

        v = os.environ.get("HASAMI_MAX_TWINS", "8")
        return max(1, int(v)) if v.isdigit() else 8

    def _twin_view(twin) -> dict:
        return twin.to_dict() | {"images": {"side_by_side": f"/api/twin/{twin.id}/side_by_side.png",
                                            "photo": f"/api/twin/{twin.id}/photo.png"}}

    def _touch(twin_id: str) -> None:
        with twins_lock:
            if twin_id in twins:
                twins[twin_id] = twins.pop(twin_id)

    def _evict() -> None:
        """Keep at most HASAMI_MAX_TWINS twins (8): drop the least recently used, from memory and its folder,
        never the one a running twin task works in."""
        import shutil

        busy_id = twin_run["id"] if runner_busy() else None
        with twins_lock:
            gone = []
            for tid in list(twins):
                if len(twins) <= _max_twins():
                    break
                if tid != busy_id:
                    del twins[tid]
                    gone.append(tid)
        for tid in gone:
            folder = _twins_dir() / tid
            if TWIN_ID.fullmatch(tid) and folder.is_dir():
                shutil.rmtree(folder, ignore_errors=True)

    def _twin_or_404(twin_id: str):
        t = twins.get(twin_id)
        if t is None:
            return None, JSONResponse(status_code=404, content={"error": f"no twin {twin_id!r} (a twin lives in server "
                                                                          "memory until it is evicted or the server "
                                                                          "stops; upload the photo again)"})
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
        except IndexError as e:
            return JSONResponse(status_code=400, content={"error": f"IndexError: {e}"})
        save_twin(twin, rgb, out_dir=_twins_dir())
        with twins_lock:
            twins.pop(twin.id, None)
            twins[twin.id] = {"twin": twin, "rgb": rgb, "opts": opts}
        _evict()
        return _twin_view(twin)

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
        work in. The photo is never sent anywhere; SAM 2.1 runs locally. The decode and the build (seconds of
        SAM 2.1 and MuJoCo) run in a worker thread, so the event stream and every other request keep going."""
        if request.app.state.runner.busy:
            return busy_response(request)
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
        data = await read_photo_body(request)
        if isinstance(data, JSONResponse):
            return data
        try:
            rgb = await run_in_threadpool(decode_photo, data)
        except PhotoError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        if request.app.state.runner.busy:  # a run may have started while the body was arriving
            return busy_response(request)
        return await run_in_threadpool(_build_twin, request, rgb, opts)

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

    @app.get("/api/twin")
    def get_latest_twin():
        """The twin built, adjusted, loaded or run last (the dashboard restores it after a reload), or null.
        A twin lives in server memory, its photo in the twins folder, until it is evicted (HASAMI_MAX_TWINS) or the
        server stops; then both are gone."""
        if not twins:
            return {"twin": None, "note": "no twin yet (a twin and its photo are kept until evicted or the server stops)"}
        return {"twin": _twin_view(next(reversed(twins.values()))["twin"])}

    @app.get("/api/twin/{twin_id}")
    def get_twin(twin_id: str):
        t, err = _twin_or_404(twin_id)
        return err or _twin_view(t["twin"])

    @app.get("/api/twin/{twin_id}/{name}.png")
    def get_twin_png(twin_id: str, name: Literal["photo", "side_by_side"]):
        t, err = _twin_or_404(twin_id)
        if err:
            return err
        return Response((_twins_dir() / twin_id / f"{name}.png").read_bytes(), media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/twin/{twin_id}/load")
    def load_twin(twin_id: str, request: Request):
        """Put the twin in the simulator (the live camera then shows it; click-to-pick works on it)."""
        t, err = _twin_or_404(twin_id)
        if err:
            return err
        try:
            with request.app.state.runner.exclusive():
                try:
                    out = sess(request).load_scene(t["twin"].spec)
                except ValueError as e:
                    return JSONResponse(status_code=422, content={"error": str(e)})
        except RuntimeError as e:
            return busy_response(request, str(e))
        _touch(twin_id)
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
        except RuntimeError as e:
            return busy_response(request, str(e))
        twin_run["id"] = twin_id
        _touch(twin_id)
        return {"run_id": run_id, "task": body.task, "instruction": task.instruction, "label": TWIN_LABEL,
                "planner": ORACLE_LABEL}

    if DASHBOARD_DIST.is_dir():
        app.mount("/", StaticFiles(directory=DASHBOARD_DIST, html=True), name="dashboard")
    else:
        @app.get("/")
        def root() -> dict:
            return {"name": "hasami", "api": "/api/status",
                    "note": f"Dashboard not built: run `pnpm install && pnpm build` in packages/dashboard (expected {DASHBOARD_DIST})."}

    return app


app = create_app()


def serve(host: str | None = None, port: int | None = None) -> None:
    import os

    import uvicorn

    uvicorn.run(app, host=host or os.environ.get("HASAMI_HOST", "127.0.0.1"),
                port=port or int(os.environ.get("HASAMI_PORT", "8420")), log_level="info")


__all__ = ["EventBus", "LocalOnly", "Runner", "allowed_origins_from_env", "app", "create_app", "prune_twin_folders",
           "serve"]
