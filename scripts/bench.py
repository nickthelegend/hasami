"""Benchmark: run task families on seeded scenes and score them against the physics state.

    HF_HOME=... .venv/bin/python scripts/bench.py --family all-to-tray --seeds 30
    .venv/bin/python scripts/bench.py --family all --seeds 10 --planner clicks
    .venv/bin/python scripts/bench.py --family all --seeds 10 --planner scripted --hint-px 20
    HASAMI_DISTURB=depth_mm:2,cam_mm:3,light:0.3 .venv/bin/python scripts/bench.py --family all --seeds 8
    .venv/bin/python scripts/bench.py --family sort-by-colour --seeds 5 --planner muse   # needs MODEL_API_KEY

Planners:
  clicks    the simulated-click oracle (hasami_core.tasks.ORACLE_LABEL). It is NOT a language model: it
            stands in for a planner that understood the instruction, so these numbers measure perception,
            grasping, placing and recovery, not planning.
  scripted  a Muse-format plan written from the task by code (hasami_core.tasks.SCRIPTED_LABEL): labels
            plus pick hints moved --hint-px off, so the grounding layer has to find each object. Not a
            language model either.
  muse    Muse on the Meta Model API plans from the instruction and the overhead frame. Without
          MODEL_API_KEY this prints "Setup required" and exits 2; nothing is written.

Writes deployments/bench/<UTC timestamp>.json: success rate per family, failure breakdown by the first
failing step's class (segment / implausible / grasp / place; 'goal' when every step passed but the final
arrangement did not), retries, and how often the image check agreed with the physics. 'implausible' is a
step skipped before any move because no located target passed the target check (size, height, workspace,
mask leak; hasami_perception.plausible), even after a boxed re-prompt and a colour / raised-blob search.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from hasami_core import Session, SetupRequired
from hasami_core.session import ROOT, SIMULATION, Disturb, _json_default
from hasami_core.tasks import (FAMILIES, ORACLE_LABEL, SCRIPTED_LABEL, make_task, oracle_steps, scripted_steps,
                               setup_task, world_from)

BENCH_DIR = ROOT / "deployments" / "bench"


def plan_for(task, session, planner, planner_name: str, hint_px: float = 20.0):
    if planner_name == "clicks":
        return oracle_steps(task, session)
    if planner_name == "scripted":
        from hasami_planner import validate_steps

        return validate_steps(scripted_steps(task, session, hint_px, task.seed), source="scripted",
                              model=None, instruction=task.instruction)
    png, _ = session.frame_png("overhead")
    plan, _latency = planner.plan(task.instruction, png, [o["label"] for o in session.scene_objects()])
    return plan


def classify(rec: dict, task, goal_ok: bool) -> str | None:
    """The failure class of a run: the first failed step's class; 'goal' if all steps passed but not the goal."""
    if goal_ok:
        return None
    for step, (obj, _dest) in zip(rec["steps"], task.moves):
        r = step["result"]
        if not r["ok"]:
            if r.get("object_name") and r["object_name"] != obj:
                return "segment"  # picked the wrong object
            return r.get("failure") or "place"
        if r.get("object_name") and r["object_name"] != obj:
            return "segment"
    if len(rec["steps"]) < len(task.moves):
        return "segment"
    return "goal"


def run_family(family: str, seeds: list[int], session: Session, planner, planner_name: str, log,
               hint_px: float = 20.0) -> dict:
    tasks, t0 = [], time.perf_counter()
    for seed in seeds:
        task = make_task(family, seed)
        try:
            setup_task(task, session)
        except ValueError as e:  # hidden object or no layout: skip the seed, say so
            tasks.append({"seed": seed, "skipped": str(e)})
            log(f"  {family} seed {seed}: skipped ({e})")
            continue
        w0 = world_from(session)
        t1 = time.perf_counter()
        try:
            steps = plan_for(task, session, planner, planner_name, hint_px)
            rec = session.run(steps)
        except Exception as e:  # noqa: BLE001 - record and continue; a crash is a failure, not a skip
            tasks.append({"seed": seed, "instruction": task.instruction, "ok": False, "failure": "error",
                          "error": f"{type(e).__name__}: {e}"})
            log(f"  {family} seed {seed}: ERROR {type(e).__name__}: {e}")
            continue
        ok, detail = task.goal(w0, world_from(session))
        failure = classify(rec, task, ok)
        checks = [a["verify"] for s in rec["steps"] for a in s["attempts"] if a.get("verify")]
        tasks.append({
            "seed": seed, "instruction": task.instruction, "ok": ok, "failure": failure, "goal": detail,
            "steps": [{"pick": s["pick_label"], "object": s["result"].get("object"), "ok": s["result"]["ok"],
                       "attempts": s["result"].get("attempts", 1), "failure": s["result"].get("failure"),
                       "localisation_error_mm": s["result"].get("localisation_error_mm"),
                       "note": s["result"].get("note")} for s in rec["steps"]],
            "retries": rec["verify"]["retries"], "checks": len(checks),
            "agree": sum(1 for c in checks if c["agree"]),
            "image_ok_physics_fail": sum(1 for c in checks if c["ok"] and not c["physics_ok"]),
            "image_fail_physics_ok": sum(1 for c in checks if not c["ok"] and c["physics_ok"]),
            "iou_mean": rec["iou"]["mean"], "seconds": round(time.perf_counter() - t1, 1),
        })
        log(f"  {family} seed {seed}: {'ok' if ok else 'FAIL (' + str(failure) + ')'}  "
            f"steps {sum(1 for s in rec['steps'] if s['result']['ok'])}/{len(task.moves)}  "
            f"retries {rec['verify']['retries']}  {time.perf_counter() - t1:.1f}s")
    ran = [t for t in tasks if "skipped" not in t]
    fails = [t["failure"] for t in ran if not t["ok"]]
    checks = sum(t.get("checks", 0) for t in ran)
    ious = [t["iou_mean"] for t in ran if t.get("iou_mean") is not None]
    objects = sum(len(t.get("steps", [])) for t in ran)
    placed = sum(1 for t in ran for s in t.get("steps", []) if s["ok"])
    return {
        "family": family, "seeds": seeds, "ran": len(ran), "skipped": len(tasks) - len(ran),
        "success": sum(1 for t in ran if t["ok"]),
        "rate": round(sum(1 for t in ran if t["ok"]) / len(ran), 3) if ran else None,
        "steps_ok": placed, "steps": objects,
        "steps_implausible": sum(1 for t in ran for s in t.get("steps", []) if s.get("failure") == "implausible"),
        "failures": {k: fails.count(k) for k in ("segment", "implausible", "grasp", "place", "goal", "error")
                     if fails.count(k)},
        "retries": sum(t.get("retries", 0) for t in ran),
        "verify": {"checks": checks, "agree": sum(t.get("agree", 0) for t in ran),
                   "image_ok_physics_fail": sum(t.get("image_ok_physics_fail", 0) for t in ran),
                   "image_fail_physics_ok": sum(t.get("image_fail_physics_ok", 0) for t in ran)},
        "iou_mean": round(float(np.mean(ious)), 4) if ious else None,
        "seconds": round(time.perf_counter() - t0, 1),
        "tasks": tasks,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--family", default="all", help=f"one of {FAMILIES} or 'all' (comma-separated list ok)")
    ap.add_argument("--seeds", type=int, default=10, help="seeds 0..N-1 (from --start)")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--planner", choices=["clicks", "scripted", "muse"], default="clicks")
    ap.add_argument("--hint-px", type=float, default=20.0, help="scripted planner: how far each pick hint is off")
    ap.add_argument("--out", type=Path, default=None, help="output JSON (default deployments/bench/<ts>.json)")
    args = ap.parse_args()

    families = FAMILIES if args.family == "all" else [f.strip() for f in args.family.split(",")]
    for f in families:
        if f not in FAMILIES:
            print(f"unknown family {f!r}; known: {', '.join(FAMILIES)}", file=sys.stderr)
            return 2
    planner = None
    if args.planner == "muse":
        from hasami_planner import Planner

        try:
            planner = Planner()
        except SetupRequired as e:
            print(str(e), file=sys.stderr)
            return 2

    session = Session(seed=0)
    _ = session.sam
    seeds = list(range(args.start, args.start + args.seeds))
    started = datetime.now(timezone.utc)
    out = {
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "simulation": SIMULATION,
        "planner": args.planner,
        "planner_note": {"clicks": ORACLE_LABEL, "scripted": f"{SCRIPTED_LABEL} (hint {args.hint_px:g} px off)"}.get(
            args.planner) or f"Muse ({planner.model}) on the Meta Model API",
        "segmenter": "sam2.1-hiera-tiny (local, point prompts)",
        "max_retries": session.max_retries,
        "disturb": None if session.disturb is None else session.disturb.to_dict(),
        "command": "scripts/bench.py " + " ".join(sys.argv[1:]),
        "families": {},
    }
    print(f"bench: {', '.join(families)}  seeds {seeds[0]}..{seeds[-1]}  planner {args.planner}"
          + (f"  disturb {os.environ.get('HASAMI_DISTURB')}" if session.disturb else ""), flush=True)
    for f in families:
        res = run_family(f, seeds, session, planner, args.planner, lambda m: print(m, flush=True), args.hint_px)
        out["families"][f] = res
        print(f"{f}: {res['success']}/{res['ran']} tasks, {res['steps_ok']}/{res['steps']} steps, "
              f"failures {res['failures'] or '-'}, retries {res['retries']}, "
              f"check agrees {res['verify']['agree']}/{res['verify']['checks']}", flush=True)
    out["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    path = args.out or BENCH_DIR / f"{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2, default=_json_default) + "\n")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
