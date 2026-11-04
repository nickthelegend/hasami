"""Guarded real-robot fetch. All positions are taught commands, not measured positions."""
from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from . import poses
from .client import RobotError, Walker
from .look import FindError, annotate, decode_jpeg, find
from .reach import ReachError, load_grid, plan_reach

EventCallback = Callable[[dict], None]


def _run_dir() -> Path:
    root = Path(os.environ.get("HASAMI_RUNS_DIR", Path.home() / ".hasami" / "runs"))
    path = root / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    path.mkdir(parents=True)
    return path


def _foot(f) -> tuple[float, float]:
    """Bottom centre of actual mask pixels, averaged over the bottom 5 rows."""
    ys, xs = np.nonzero(f.mask)
    bottom = ys.max()
    return float(np.median(xs[ys >= bottom - 4])), float(bottom)


def _valid(f, shape, min_conf: float = 0.35) -> None:
    if f.confidence is not None and (not math.isfinite(f.confidence) or f.confidence < min_conf):
        raise FindError(f"detection confidence {f.confidence!r} below valid threshold {min_conf:.2f}")
    if f.bbox[0] <= 0 or f.bbox[1] <= 0 or f.bbox[2] >= shape[1] - 1 or f.bbox[3] >= shape[0] - 1:
        raise FindError("item mask touches the camera frame edge")


def _verification(before, after, jaw_region: list[float], up_px: float = 12.0) -> dict:
    x0, y0, x1, y1 = (int(round(x)) for x in jaw_region)
    if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0 or x1 >= after.mask.shape[1] or y1 >= after.mask.shape[0]:
        raise ReachError("taught jaw region is invalid for this camera frame")
    moved_up = before.centroid[1] - after.centroid[1] > up_px
    old_area = int(before.mask.sum())
    old_covered = int(np.logical_and(before.mask, after.mask).sum()) / max(1, old_area)
    spot_clear = old_covered < 0.25
    jaw_overlap = int(after.mask[y0:y1 + 1, x0:x1 + 1].sum()) / max(1, int(after.mask.sum()))
    held = (moved_up or spot_clear) and jaw_overlap >= 0.05
    return {"held": bool(held), "moved_up_px": round(before.centroid[1] - after.centroid[1], 1),
            "old_spot_covered": round(old_covered, 3), "jaw_overlap": round(jaw_overlap, 3),
            "reason": "geometry agrees" if held else "item did not move up/clear shelf and overlap taught jaw region"}


def _wrist_verification(before, lift_found, look_found, jaw_region: list[float], min_jaw: float = 0.3) -> dict:
    """Wrist camera: the camera moves with the claw, so 'moved up' means nothing. Held = the item fills the taught jaw
    region in the lift-pose frame AND, seen again from the look pose, its old floor/shelf spot is clear."""
    x0, y0, x1, y1 = (int(round(x)) for x in jaw_region)
    jaw_overlap = 0.0
    if lift_found is not None:
        m = lift_found.mask
        if x1 <= x0 or y1 <= y0 or x0 < 0 or y0 < 0 or x1 >= m.shape[1] or y1 >= m.shape[0]:
            raise ReachError("taught jaw region is invalid for this camera frame")
        jaw_overlap = int(m[y0:y1 + 1, x0:x1 + 1].sum()) / max(1, int(m.sum()))
    old_area = int(before.mask.sum())
    old_covered = (int(np.logical_and(before.mask, look_found.mask).sum()) / max(1, old_area)
                   if look_found is not None else 0.0)
    spot_clear = old_covered < 0.25
    held = spot_clear and jaw_overlap >= min_jaw
    return {"held": bool(held), "camera": "wrist", "jaw_overlap": round(jaw_overlap, 3),
            "old_spot_covered": round(old_covered, 3), "lift_detected": lift_found is not None,
            "reason": "item between the jaws and its old spot is clear" if held else
                      ("item still at its old spot" if not spot_clear else "item not seen between the jaws at lift")}


async def fetch(w: Walker, instruction: str, *, item: str | None = None, grid_name: str = "fridge",
                point: tuple[float, float] | None = None, drive_back: float = 0, dry_plan: bool = False,
                callback: EventCallback | None = None, sam2=None, sam3=None) -> dict:
    """Run one fetch and save the evidence even on error. Returns a report; it never reports a held item by command alone."""
    path = _run_dir()
    wrist = False
    report = {"instruction": instruction, "intent": None, "models": {}, "dry_plan": dry_plan,
              "steps": [], "targets": [], "verdict": "not run", "error": None, "run_dir": str(path)}
    stream = (path / "events.jsonl").open("w")
    last_event_time = time.perf_counter()

    def event(step, status, **data):
        nonlocal last_event_time
        now = time.perf_counter()
        e = {"at": datetime.now(timezone.utc).isoformat(), "step": step, "status": status,
             "elapsed_ms": round((now - last_event_time) * 1000), **data}
        last_event_time = now
        stream.write(json.dumps(e) + "\n")
        stream.flush()
        report["steps"].append(e)
        if callback:
            callback(e)

    async def snap():
        if wrist:
            await asyncio.sleep(0.6)                      # let the arm and the image settle after a move
            await asyncio.to_thread(w.snapshot)           # discard: the webcam may still hold a pre-move frame
        return decode_jpeg(await asyncio.to_thread(w.snapshot))

    def detect(rgb, label):
        f = find(rgb, label, point=point, sam2=sam2, sam3=sam3)
        _valid(f, rgb.shape)
        return f

    async def move(label, q):
        w._check_control()
        started = time.perf_counter()
        from .gripper_geometry import MIN_GRIP_GAP_MM
        if len(q) != 6 or (not w.rotary() and q[5] < MIN_GRIP_GAP_MM):
            raise ReachError(f"{label}: six joint targets and J6 gap >= {MIN_GRIP_GAP_MM:g} mm are required")
        for i, (value, (lo, hi)) in enumerate(zip(q, w.limits())):
            if not lo <= value <= hi:
                raise ReachError(f"{label}: J{i+1} target {value:.1f} outside calibrated [{lo:.1f}, {hi:.1f}]")
        if not await w.go(q, timeout=12, fail_on_manual=True):
            raise RobotError(f"{label}: commanded joint targets were not reached by runtime telemetry")
        event(label, "ok", target=q, ms=round((time.perf_counter() - started) * 1000),
              note="commanded telemetry only; no servo feedback")

    try:
        if not instruction.strip():
            raise ValueError("fetch instruction is empty")
        if os.environ.get("MODEL_API_KEY"):
            from hasami_planner.muse import fetch_intent
            intent, latency, model = await asyncio.to_thread(fetch_intent, instruction)
            label, destination = intent.item_text, intent.destination
            report["models"]["muse"] = model
            event("understand", "ok", item_text=label, destination=destination, model=model, ms=round(latency))
        elif item:
            label, destination = item.strip(), "operator"
            if not label:
                raise ValueError("--item must name a visible object")
            event("understand", "ok", item_text=label, destination=destination, muse="not used: no MODEL_API_KEY")
        else:
            raise ValueError("Setup required: MODEL_API_KEY for Muse, or supply --item explicitly")
        report["intent"] = {"item_text": label, "destination": destination}
        if destination.strip().lower() not in {"operator", "me", "user", "person", "basket", "the basket"}:
            raise ValueError(f"destination {destination!r} has no taught delivery route; only the operator basket is supported")
        if not 0 <= drive_back <= 5:
            raise ValueError("--drive-back must be between 0 and 5 seconds")
        grid = load_grid(grid_name)
        wrist = grid.get("camera", "body") == "wrist"
        report["camera"] = "wrist" if wrist else "body"
        if w.config.get("gripper") not in ("parallel", "rotary") or w.config.get("usable") != 63 or not w.config.get("cal"):
            raise RobotError("Setup required: all six joints and the gripper must be calibrated")
        if w.rotary():
            claw_open, claw_grip = w._claw("claw_open"), w._claw("claw_grip")
            event("claw", "ok", kind="rotary", open_deg=claw_open, grip_deg=claw_grip,
                  note="taught claw poses; item-width fit checked only against HASAMI_CLAW_MAX_MM if set")
        if not w.tele.at:
            try:
                await asyncio.wait_for(w._tele_event.wait(), 1.0)
            except asyncio.TimeoutError:
                raise RobotError("no robot telemetry received") from None
        if not dry_plan:
            if w.tele.on_mask != 63:
                raise RobotError("Setup required: turn all six joints ON from the phone with arm supported")
            await move("look_pose", poses.get("look_shelf"))
        elif wrist:
            look = poses.get("look_shelf")
            if w.tele.on_mask != 63 or any(abs(a - b) > 2.0 for a, b in zip(w.tele.q[:5], look[:5])):
                raise RobotError("Setup required: wrist camera dry plan looks from look_shelf; with all joints ON run "
                                 "'python -m hasami_walker go look_shelf' first (dry plan itself sends no arm command)")
            event("look_pose", "ok", note="dry plan: arm already at look_shelf; no command sent")
        else:
            event("look_pose", "not run", reason="dry plan")
        attempt = 0
        held = False
        while attempt < 2:
            attempt += 1
            w._check_control()
            if wrist and attempt > 1:
                await move("look_pose", poses.get("look_shelf")[:5] + [open_gap])
            t0 = time.perf_counter()
            before_rgb = await snap()
            before = await asyncio.to_thread(detect, before_rgb, label)
            (path / f"before-{attempt}.png").write_bytes(annotate(before_rgb, before, label))
            report["models"]["segmenter"] = (getattr(sam3, "model", "sam-3.1") if before.how == "sam-3.1 text"
                                                 else getattr(sam2, "name", "sam2.1-hiera-tiny (local)"))
            event("look", "ok", attempt=attempt, **before.summary(), ms=round((time.perf_counter() - t0) * 1000))
            reach = plan_reach(grid, _foot(before), resolution=(before_rgb.shape[1], before_rgb.shape[0]))
            width = reach.width_mm(before.bbox[2] - before.bbox[0] + 1)
            if w.rotary():
                claw_max = float(os.environ.get("HASAMI_CLAW_MAX_MM", "0") or 0)
                if claw_max and width > claw_max:
                    raise ReachError(f"item width {width:.1f} mm exceeds the measured claw opening {claw_max:.1f} mm")
                gap = claw_max or width + 6
            else:
                gap = w._parallel_limits()[1]
            if width > gap:
                raise ReachError(f"item width {width:.1f} mm exceeds calibrated open gap {gap:.1f} mm")
            if width <= 4:
                raise ReachError(f"item width {width:.1f} mm is too small for safe 4 mm minimum gap")
            targets = {"pre_grasp": reach.pre_grasp, "grasp": reach.grasp, "lift": reach.lift,
                       "width_mm": round(width, 1), "pixel": list(_foot(before)), "triangle": reach.triangle,
                       "jaw_region": reach.jaw_region}
            report["targets"].append(targets)
            for target_name, q in (("pre_grasp", reach.pre_grasp), ("grasp", reach.grasp), ("lift", reach.lift)):
                for j, (value, (lo, hi)) in enumerate(zip(q[:5], w.limits()[:5])):
                    if not lo <= value <= hi:
                        raise ReachError(f"{target_name}: J{j+1} target {value:.1f} outside calibrated [{lo:.1f}, {hi:.1f}]")
            event("plan", "ok", attempt=attempt, **targets)
            if dry_plan:
                report["verdict"] = "dry plan complete; no arm command sent"
                break
            open_gap = claw_open if w.rotary() else min(gap, width + 6)
            await w.open_gripper(open_gap)
            open_target = list(w.tele.q[:5]) + [open_gap]
            if not await w.wait_pose(open_target, timeout=max(8, abs(open_gap - w.gripper_gap()) / 14 + 3)):
                raise RobotError("gripper open command did not reach its commanded target")
            event("open", "ok", attempt=attempt, gap_mm=open_gap)
            for step, target in (("pre_grasp", reach.pre_grasp), ("grasp", reach.grasp)):
                await move(step, target[:5] + [open_gap])
            grip_gap = await w.grip(width)
            if not await w.wait_pose(reach.grasp[:5] + [grip_gap], timeout=max(8, abs(open_gap - grip_gap) / 14 + 3)):
                raise RobotError("grip command did not reach its commanded gap")
            event("grip", "ok", attempt=attempt, gap_mm=grip_gap, note="commanded gap, no force feedback")
            await move("lift", reach.lift[:5] + [grip_gap])
            after_rgb = await snap()
            if wrist:
                try:   # held item is close to the lens and may touch the frame edge: no _valid() here
                    lift_found = await asyncio.to_thread(find, after_rgb, label, None, sam2, sam3)
                    (path / f"after-{attempt}.png").write_bytes(annotate(after_rgb, lift_found, label))
                except FindError:
                    lift_found = None
                    (path / f"after-{attempt}.png").write_bytes(_raw_png(after_rgb))
                await move("verify_look", poses.get("look_shelf")[:5] + [grip_gap])
                look_rgb = await snap()
                try:
                    look_found = await asyncio.to_thread(find, look_rgb, label, None, sam2, sam3)
                    (path / f"check-{attempt}.png").write_bytes(annotate(look_rgb, look_found, label))
                except FindError:
                    look_found = None   # nothing left on the shelf where the camera looks
                    (path / f"check-{attempt}.png").write_bytes(_raw_png(look_rgb))
                verdict = _wrist_verification(before, lift_found, look_found, reach.jaw_region)
            else:
                try:
                    after = await asyncio.to_thread(detect, after_rgb, label)
                    (path / f"after-{attempt}.png").write_bytes(annotate(after_rgb, after, label))
                    verdict = _verification(before, after, reach.jaw_region)
                except FindError as e:
                    (path / f"after-{attempt}.png").write_bytes(_raw_png(after_rgb))
                    verdict = {"held": False, "reason": f"post-lift item detection failed: {e}"}
            event("verify", "ok" if verdict["held"] else "failed", attempt=attempt, **verdict)
            if os.environ.get("MODEL_API_KEY"):
                try:
                    from hasami_planner.muse import Planner
                    def opinion():
                        planner = Planner()
                        image_url = "data:image/png;base64," + base64.b64encode(_raw_png(after_rgb)).decode()
                        response = planner.client.chat.completions.create(
                            model=planner.model, messages=[{"role": "user", "content": [
                                {"type": "text", "text": f"Is the {label} held between the gripper jaws? Answer yes/no with one sentence."},
                                {"type": "image_url", "image_url": {"url": image_url}}]}])
                        return response.choices[0].message.content or ""
                    sentence = await asyncio.to_thread(opinion)
                    event("muse_opinion", "ok", sentence=sentence, note="does not override geometric verification")
                except Exception as e:
                    event("muse_opinion", "failed", reason=str(e))
            else:
                event("muse_opinion", "not run", reason="no MODEL_API_KEY")
            if verdict["held"]:
                held = True
                break
            if attempt == 1:
                await w.open_gripper(open_gap)
                await move("retry_back", reach.pre_grasp[:5] + [open_gap])
                event("retry", "ok", reason=verdict["reason"])
        if dry_plan:
            for step in ("reach", "grip", "lift", "verify", "retry", "carry", "drive_back", "place", "release"):
                event(step, "not run", reason="dry plan")
        elif not held:
            report["verdict"] = "failed: grasp not verified after one retry"
            await w.stop()  # hold current arm targets and zero the wheels; do not drop an uncertain payload
            event("stop", "ok", reason="grasp not verified; arm holds at last commanded pose")
            for step in ("carry", "drive_back", "place", "release"):
                event(step, "not run", reason="grasp not verified")
        else:
            await move("carry", poses.get("carry")[:5] + [grip_gap])
            if drive_back:
                await w.drive_for(-15, 0, drive_back)
                w._check_control()
                event("drive_back", "ok", seconds=drive_back, throttle=-15)
            else:
                event("drive_back", "not run", reason="off by default")
            await move("place", poses.get("place")[:5] + [grip_gap])
            released_gap = await w.open_gripper()
            if not await w.wait_pose(poses.get("place")[:5] + [released_gap], timeout=8):
                raise RobotError("release command did not reach its commanded gap")
            event("release", "ok", gap_mm=released_gap, note="release commanded; no basket camera verification")
            await move("return_carry", poses.get("carry")[:5] + [released_gap])
            report["verdict"] = "grasp verified by camera; release commanded, delivery unverified"
    except Exception as e:
        report["error"] = f"{type(e).__name__}: {e}"
        report["verdict"] = "stopped: " + report["error"]
        event("error", "failed", error=report["error"])
        for step in ("carry", "drive_back", "place", "release"):
            if not any(x["step"] == step for x in report["steps"]):
                event(step, "not run", reason=report["error"])
        try:
            await w.stop()
        except Exception:
            pass
    finally:
        for step in ("understand", "look_pose", "look", "plan", "open", "pre_grasp", "grasp", "grip",
                     "lift", "verify_look", "verify", "muse_opinion", "retry", "carry", "drive_back", "place", "release", "return_carry"):
            if not any(x["step"] == step for x in report["steps"]):
                event(step, "not run", reason=report["error"] or report["verdict"])
        (path / "run.json").write_text(json.dumps(report, indent=2))
        stream.close()
    return report


def _raw_png(rgb):
    from io import BytesIO
    from PIL import Image
    out = BytesIO()
    Image.fromarray(rgb).save(out, format="PNG")
    return out.getvalue()
