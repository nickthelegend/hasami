"""Digital-twin accuracy on synthetic photos: a real round trip, not a mock.

For each seed: lay out 3-5 objects (benchmark shapes and colours, a layout the arm can work in), render a
desk photo with MuJoCo (table, A4 sheet, objects, a tilted camera at a random pose; no arm, no depth),
feed only that RGB image to the twin pipeline (sheet -> plane, SAM 2.1 automatic masks, box / cylinder
fits), and compare the recovered objects with the poses that were rendered.

Each photo is measured twice: with the camera's true field of view, and with the default assumption
(70 deg horizontal) a real photo of unknown focal length gets. With --tasks, "clear-desk" (everything into
the tray) is run on the twin by the simulated-click oracle and scored on the twin's physics.

    HF_HOME=... .venv/bin/python scripts/twin_accuracy.py --seeds 20 --tasks
Writes deployments/twins/accuracy-<UTC>.json.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import numpy as np

from hasami_core import Session
from hasami_core.session import ROOT, SIMULATION
from hasami_core.tasks import ORACLE_LABEL, oracle_steps, world_from
from hasami_core.twin import TWIN_LABEL, make_twin, twin_tasks
from hasami_sim.photo import SheetPose, random_desk, render_photo
from hasami_sim.scene import ObjectSpec

OUT_DIR = ROOT / "deployments" / "twins"


def _yaw_err(kind: str, dims, yaw_t: float, yaw_r: float) -> float | None:
    if kind == "cylinder":
        return None
    period = np.pi / 2 if abs(dims[0] - dims[1]) < 0.004 else np.pi
    d = (yaw_r - yaw_t + period / 2) % period - period / 2
    return float(np.degrees(abs(d)))


def compare(truth: list[ObjectSpec], twin) -> dict:
    rec = [(o, w) for o, w in zip(twin.objects, twin.world)]
    used: set[int] = set()
    rows = []
    for t in truth:
        best, bd = None, 0.015
        for i, (o, w) in enumerate(rec):
            d = float(np.hypot(w["x_m"] - t.x, w["y_m"] - t.y))
            if i not in used and d < bd:
                best, bd = i, d
        if best is None:
            rows.append({"truth": t.label, "found": False})
            continue
        used.add(best)
        o, w = rec[best]
        dims_t = sorted(t.dims[:2], reverse=True)
        dims_r = sorted(o.size_mm[:2], reverse=True)
        rows.append({
            "truth": t.label, "found": True, "kind_truth": "cylinder" if t.shape == "cylinder" else "box",
            "kind": o.kind, "colour_truth": t.colour, "colour": o.colour,
            "centre_err_mm": round(bd * 1000, 2),
            "yaw_err_deg": None if (e := _yaw_err(t.shape, t.dims, t.yaw, w["yaw_world"])) is None else round(e, 2),
            "long_err_mm": round(dims_r[0] - dims_t[0] * 1000, 2), "short_err_mm": round(dims_r[1] - dims_t[1] * 1000, 2),
            "height_err_mm": round(o.size_mm[2] - t.height * 1000, 2), "fit_iou": o.fit_iou,
        })
    return {"objects": rows, "false_positives": len(rec) - len(used)}


def summarise(rows: list[dict], fps: int) -> dict:
    found = [r for r in rows if r["found"]]

    def stat(key):
        v = [abs(r[key]) for r in found if r.get(key) is not None]
        return {"mean": round(float(np.mean(v)), 2), "median": round(float(np.median(v)), 2),
                "max": round(float(np.max(v)), 2), "n": len(v)} if v else None

    return {"objects": len(rows), "found": len(found), "false_positives": fps,
            "kind_correct": sum(r["kind"] == r["kind_truth"] for r in found),
            "colour_correct": sum(r["colour"] == r["colour_truth"] for r in found),
            "centre_err_mm": stat("centre_err_mm"), "yaw_err_deg": stat("yaw_err_deg"),
            "long_err_mm": stat("long_err_mm"), "short_err_mm": stat("short_err_mm"),
            "height_err_mm": stat("height_err_mm")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--tasks", action="store_true", help="also run clear-desk on each twin (simulated arm)")
    args = ap.parse_args()

    session = Session(seed=0)
    sam = session.sam
    started = datetime.now(timezone.utc)
    scenes, rows_true, rows_70, fp_true, fp_70 = [], [], [], 0, 0
    tasks_ok = tasks_run = 0
    for seed in range(args.start, args.start + args.seeds):
        objs, cam = random_desk(seed)
        rgb = render_photo(objs, SheetPose(), cam)
        entry: dict = {"seed": seed, "camera": {"pos": np.round(cam.pos, 3).tolist(), "fovy_deg": round(cam.fovy_deg, 1),
                                                "hfov_deg": round(cam.hfov_deg, 1)},
                       "truth": [{"label": o.label, "x": round(o.x, 4), "y": round(o.y, 4), "yaw": round(o.yaw, 4),
                                  "dims": list(o.dims)} for o in objs]}
        for key, hfov in (("true_fov", cam.hfov_deg), ("assumed_fov_70", 70.0)):
            t0 = time.perf_counter()
            try:
                twin = make_twin(rgb, sam, hfov_deg=hfov)
            except ValueError as e:
                entry[key] = {"error": str(e)}
                print(f"seed {seed} {key}: ERROR {e}", flush=True)
                continue
            cmp = compare(objs, twin)
            entry[key] = cmp | {"seconds": round(time.perf_counter() - t0, 2), "stats": twin.stats,
                                "sheet": twin.sheet.to_dict()}
            if key == "true_fov":
                rows_true += cmp["objects"]
                fp_true += cmp["false_positives"]
            else:
                rows_70 += cmp["objects"]
                fp_70 += cmp["false_positives"]
            found = sum(r["found"] for r in cmp["objects"])
            errs = [r["centre_err_mm"] for r in cmp["objects"] if r["found"]]
            print(f"seed {seed} {key}: found {found}/{len(objs)}  fp {cmp['false_positives']}  "
                  f"centre max {max(errs) if errs else '-'} mm  {time.perf_counter() - t0:.1f}s", flush=True)
            if key == "true_fov" and args.tasks:
                task = twin_tasks(twin).get("clear-desk")
                if task is None:
                    entry["task"] = {"skipped": "no reachable, graspable object"}
                else:
                    try:
                        session.load_scene(twin.spec)
                    except ValueError as e:  # an object hidden from the simulated overhead camera
                        tasks_run += 1
                        entry["task"] = {"id": "clear-desk", "ok": False, "error": str(e)}
                        print(f"   clear-desk: could not load the twin ({e})", flush=True)
                        continue
                    w0 = world_from(session)
                    rec = session.run(oracle_steps(task, session))
                    ok, detail = task.goal(w0, world_from(session))
                    tasks_run += 1
                    tasks_ok += int(ok)
                    entry["task"] = {"id": "clear-desk", "instruction": task.instruction, "ok": ok, "goal": detail,
                                     "retries": rec["verify"]["retries"],
                                     "placed": rec["placed"], "steps": rec["total"]}
                    print(f"   clear-desk on the twin: {'ok' if ok else 'FAIL'}  {rec['placed']}/{rec['total']} placed, "
                          f"retries {rec['verify']['retries']}", flush=True)
        scenes.append(entry)
    out = {
        "started_at": started.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "label": TWIN_LABEL, "photos": "synthetic: rendered by MuJoCo (table, A4 sheet, objects, tilted camera; "
                                       "no arm, no depth); only the RGB image is given to the twin",
        "simulation": SIMULATION, "segmenter": "sam2.1-hiera-tiny (local, automatic masks from point prompts)",
        "command": "scripts/twin_accuracy.py " + " ".join(sys.argv[1:]),
        "summary": {"true_fov": summarise(rows_true, fp_true), "assumed_fov_70": summarise(rows_70, fp_70)},
        "scenes": scenes,
    }
    if args.tasks:
        out["summary"]["clear_desk_on_twin"] = {"ok": tasks_ok, "run": tasks_run, "planner": ORACLE_LABEL}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"accuracy-{started.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps(out["summary"], indent=2))
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
