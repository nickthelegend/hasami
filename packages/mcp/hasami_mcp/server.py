"""Hasami as MCP tools (stdio): an agent can look at the table, find objects, and move them.

Tools: status, look, find, pick_place. Every call runs the real pipeline (MuJoCo + SAM 2.1);
nothing is mocked. Set HASAMI_MCP_LOG=/path/to/log.jsonl to append one JSON line per call.
"""

from __future__ import annotations

import functools
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from hasami_core import Session, prune_runs, status as core_status

INSTRUCTIONS = """Hasami drives a simulated SO-101 robot arm over a tabletop with a red, a green and a blue cube
and a tray. Pixel coordinates are [u, v] in the 640x480 overhead image (u right, v down).
Typical loop: look -> pick a cube's pixel and a destination pixel -> find (optional check) -> pick_place."""

mcp = MCPServer("hasami", instructions=INSTRUCTIONS, version="0.1.0")

_session: Session | None = None
_session_lock = threading.Lock()


def session() -> Session:
    global _session
    with _session_lock:
        if _session is None:
            seed = os.environ.get("HASAMI_SEED")
            _session = Session(seed=int(seed) if seed else None)
        return _session


def _log(tool: str, args: dict, ok: bool, ms: float, result: Any = None, error: str | None = None) -> None:
    path = os.environ.get("HASAMI_MCP_LOG")
    if not path:
        return
    line = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "tool": tool, "args": args, "ok": ok, "ms": round(ms, 1)}
    if error is not None:
        line["error"] = error
    if result is not None:
        line["result"] = result
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, default=str) + "\n")


def logged(fn):
    """Log every call (arguments, outcome, duration) to HASAMI_MCP_LOG. Errors become ToolErrors so the
    client sees the real message (e.g. "Setup required: MODEL_API_KEY ..."), not a generic crash."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.perf_counter()
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            _log(fn.__name__, kwargs, False, (time.perf_counter() - t0) * 1000, error=f"{type(e).__name__}: {e}")
            raise ToolError(str(e) if isinstance(e, ToolError) else f"{type(e).__name__}: {e}") from e
        summary = out if isinstance(out, dict) else {"content": [type(x).__name__ for x in out]}
        _log(fn.__name__, kwargs, True, (time.perf_counter() - t0) * 1000, result=summary)
        return out

    return wrapper


@mcp.tool()
@logged
def status() -> dict:
    """What is ready: the arm (sim or SO-101), the Muse planner, the segmenters, and the scene labels."""
    return core_status(session())


@mcp.tool(structured_output=False)
@logged
def look() -> list:
    """The current overhead camera image (PNG, 640x480) and the labels of the objects in the scene."""
    s = session()
    png, _ = s.frame_png("overhead")
    info = {"camera": "overhead", "width": 640, "height": 480, "coords": "[u, v], u right, v down",
            "objects": [o["label"] for o in s.scene_objects()]}
    return [Image(data=png, format="png"), json.dumps(info)]


@mcp.tool()
@logged
def find(label: str | None = None, point: list[float] | None = None) -> dict:
    """Segment one object and locate it in 3D.

    Give `point` ([u, v] pixel on the object) to use SAM 2.1 locally, or only `label` (e.g. "red cube")
    to use SAM 3.1 on the Meta Model API (needs MODEL_API_KEY). Returns the 3D target (centre in metres,
    top height, yaw in radians), the mask size, and how the mask scores against the simulator's ground truth.
    """
    if point is None and not label:
        raise ValueError("give a point [u, v] or a label")
    if point is not None and len(point) != 2:
        raise ValueError("point is [u, v]")
    seg = session().segment(point=tuple(point) if point else None, text=None if point else label,
                            segmenter="local" if point else "meta")
    out = seg.to_dict(with_mask=False)
    if label:
        out["label"] = label
    return out


@mcp.tool()
@logged
def pick_place(pick_point: list[float], place_point: list[float]) -> dict:
    """Pick the object under pick_point and put it at place_point ([u, v] pixels in the overhead image).

    Runs one real step in the physics simulator and reports the outcome from the physics state:
    which object moved, whether it is in the tray, and how far it ended from the target.
    """
    s = session()
    rec = s.run([{"pick_point": pick_point, "place_point": place_point}])
    s.record(rec)
    prune_runs()  # HASAMI_MAX_RUNS, never the evidence runs
    step = rec["steps"][0]
    return {"run_id": rec["run_id"], "result": step["result"], "place_how": step.get("place_how"),
            "phases": [{"phase": p["phase"], "error_mm": p["error_mm"], "ok": p["ok"]} for p in step["phases"]],
            "pick_iou_vs_truth": (step.get("pick") or {}).get("iou_vs_truth"),
            "total_ms": rec["timings"]["total_ms"]}


def main() -> None:
    mcp.run("stdio")


__all__ = ["main", "mcp"]
