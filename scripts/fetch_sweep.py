"""Fridge fetch success rates: every item over N fridge layouts (seeds), each a full fetch (head camera, SAM 2.1,
front grasp, verify + retry), scored against the physics.

    HF_HOME=... .venv/bin/python scripts/fetch_sweep.py --seeds 10
    HASAMI_DISTURB=short_mm:30 .venv/bin/python scripts/fetch_sweep.py --seeds 5   # every first grasp 30 mm short

Writes deployments/fetch/<UTC>.json: one row per fetch (item, seed, ok, attempts, failure, the check's verdict
against the physics, mask IoU, localisation error, gripper tilt) and a per-item summary. Without MODEL_API_KEY the
item is named directly (the Muse step is Setup required); the JSON says which.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from hasami_core import Session
from hasami_core.fetch import FridgeFetch
from hasami_core.session import ROOT
from hasami_sim.scene import ITEMS


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--first-seed", type=int, default=0)
    ap.add_argument("--items", nargs="*", default=list(ITEMS))
    ap.add_argument("--out", default=str(ROOT / "deployments" / "fetch"))
    args = ap.parse_args()

    session = Session(connect_real=False)
    fetch = FridgeFetch(session, seed=args.first_seed)
    _ = session.sam
    rows = []
    t0 = time.perf_counter()
    for seed in range(args.first_seed, args.first_seed + args.seeds):
        for item in args.items:
            fetch.load(seed)
            t1 = time.perf_counter()
            try:
                rec = fetch.run(f"the {item}")
            except Exception as e:  # noqa: BLE001 - a crash is a row too, with its reason
                rows.append({"item": item, "seed": seed, "ok": False, "error": f"{type(e).__name__}: {e}"})
                print(f"seed {seed} {item:13s} ERROR {e}")
                continue
            st = rec["steps"][0]
            res = st["result"]
            atts = st["attempts"]
            row = {"item": item, "seed": seed, "ok": rec["ok"], "attempts": res["attempts"], "failure": res.get("failure"),
                   "note": res.get("note"), "upright": res.get("upright"), "disturbed": res.get("disturbed", []),
                   "verified": res["verified"], "checks": rec["verify"]["checks"], "agree": rec["verify"]["agree"],
                   "iou": rec["iou"]["mean"], "localisation_error_mm": res.get("localisation_error_mm"),
                   "pitch_deg": [a.get("grasp", {}).get("pitch_deg") for a in atts],
                   "injected": [a.get("injected") for a in atts], "wall_s": round(time.perf_counter() - t1, 1)}
            rows.append(row)
            print(f"seed {seed} {item:13s} ok={row['ok']} attempts={row['attempts']} upright={row['upright']} "
                  f"failure={row['failure']} iou={row['iou']} loc={row['localisation_error_mm']} mm "
                  f"tilt={row['pitch_deg']} check agrees {row['agree']}/{row['checks']}")
    summary = {}
    for item in args.items:
        r = [x for x in rows if x["item"] == item]
        summary[item] = {"ok": sum(1 for x in r if x["ok"]), "runs": len(r),
                         "upright": sum(1 for x in r if x.get("ok") and x.get("upright")),
                         "retries": sum(x.get("attempts", 1) - 1 for x in r),
                         "check_agrees": sum(x.get("agree", 0) for x in r), "checks": sum(x.get("checks", 0) for x in r)}
    out = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "task": "fridge fetch",
           "simulation": "MuJoCo simulation, official SO-101 model", "segmenter": session.sam.name,
           "planner": "Muse" if os.environ.get("MODEL_API_KEY") else "none: the item named directly (Muse: Setup required)",
           "disturb": None if session.disturb is None else session.disturb.to_dict(),
           "seeds": [args.first_seed, args.first_seed + args.seeds - 1], "summary": summary, "runs": rows,
           "wall_s": round(time.perf_counter() - t0, 1)}
    d = Path(args.out)
    d.mkdir(parents=True, exist_ok=True)
    path = d / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.write_text(json.dumps(out, indent=2) + "\n")
    for item, s in summary.items():
        print(f"{item:13s} {s['ok']}/{s['runs']} in the basket ({s['upright']} upright), {s['retries']} retries, "
              f"check agrees {s['check_agrees']}/{s['checks']}")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
