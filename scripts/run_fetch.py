"""Record one simulated fridge fetch: "bring me the juice" -> head camera + SAM 2.1 -> SO-101 (MuJoCo) -> basket.

    HF_HOME=... .venv/bin/python scripts/run_fetch.py "the juice carton" [--seed 0] [--no-video]
    HASAMI_DISTURB=short_mm:30 .venv/bin/python scripts/run_fetch.py "the can"   # labelled disturbance: a retry

With MODEL_API_KEY set, Muse (the existing planner) reads the sentence and the head-camera frame and names the
item. Without it the Muse step prints "Setup required: MODEL_API_KEY" and the item you named is used as given.

Writes deployments/runs/<run_id>.json (the run record, same format as the tabletop runs), the step images in
deployments/runs/<run_id>/ and deployments/runs/<run_id>-fetch.mp4 (the fridge_demo camera, 960x720, ~30 fps,
captioned "MuJoCo simulation · official SO-101 model"; videos are not committed). Prints the outcome; exits 0
only if the item ended up in the basket.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_demo import FPS, SIZE, captioned  # noqa: E402  (the same caption burn-in as the tabletop demo)

from hasami_core import Session, new_run_id, runs_dir  # noqa: E402
from hasami_core.fetch import DEMO, FridgeFetch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("sentence", help='what to fetch, e.g. "the juice carton" or "bring me the juice from the fridge"')
    ap.add_argument("--seed", type=int, default=0, help="fridge layout (which shelf slot holds which item)")
    ap.add_argument("--no-video", action="store_true", help="skip the MP4 (faster)")
    args = ap.parse_args()

    session = Session(connect_real=False)
    session.store_images = True
    fetch = FridgeFetch(session, seed=args.seed)
    _ = session.sam  # load SAM 2.1 before anything is recorded
    extra = None
    if session.disturb is not None:
        bits = [f"jaws stop {session.disturb.short_mm:g} mm short on the first attempt" if session.disturb.short_mm else None,
                f"slip p={session.disturb.slip:g}" if session.disturb.slip else None]
        extra = "injected disturbance: " + ", ".join(b for b in bits if b)

    run_id = new_run_id()
    out = runs_dir()
    out.mkdir(parents=True, exist_ok=True)
    video = out / f"{run_id}-fetch.mp4"
    writer = None
    n_frames = 0
    arm = fetch.arm
    if not args.no_video:
        import imageio.v2 as imageio

        writer = imageio.get_writer(video, fps=FPS, codec="libx264", quality=8, macro_block_size=16,
                                    ffmpeg_log_level="error")

        def sink(img: np.ndarray) -> None:
            nonlocal n_frames
            writer.append_data(captioned(img, extra))
            n_frames += 1

        arm.record_camera, arm.record_size, arm.record_every, arm.frame_sink = DEMO, SIZE, 17, sink

    def on_event(kind: str, data: dict) -> None:
        if kind == "plan":
            muse = data.get("muse") or ""
            print(f"Muse: {muse}" if data["source"] != "muse" else f"Muse ({muse}) chose: {data['item']}")
            print(f"fetching: {data['item']}  (from: {data['source']})")
        elif kind == "target" and data["role"] == "pick":
            c = data["target"]["center"]
            print(f"  found it on the shelf at x={c[0]:.3f} y={c[1]:.3f} m, {data['width_mm']:.0f} mm wide, "
                  f"{data['height_mm']:.0f} mm tall")
        elif kind == "verify":
            print(f"  attempt {data['attempt']}: gripper held={data['grasp_held']} shelf clear={data['shelf_clear']} "
                  f"in basket (camera)={data['in_basket_seen']} -> {'ok' if data['ok'] else 'failed'}"
                  + (f"  [injected: {data['injected']}]" if data["injected"] else ""))
        elif kind == "retry":
            print(f"  retry {data['attempt']}: {data['reason']}")
        elif kind == "error":
            print(f"  error: {data['message']}", file=sys.stderr)

    t0 = time.perf_counter()
    rec = None
    try:
        if writer is not None:
            for _ in range(FPS):
                sink(arm.render(DEMO, *SIZE))
        rec = fetch.run(args.sentence, on_event=on_event, run_id=run_id)
        if writer is not None:
            img = arm.render(DEMO, *SIZE)
            for _ in range(int(1.5 * FPS)):
                sink(img)
    except Exception as e:  # noqa: BLE001 - say what failed, record it, exit non-zero
        print(f"fetch failed: {type(e).__name__}: {e}", file=sys.stderr)
        rec = session.last_run
    finally:
        arm.record_camera, arm.frame_sink = None, None
        if writer is not None:
            writer.close()
    if rec is None:
        return 2
    if writer is not None:
        rec["video"] = {"file": video.name, "camera": DEMO, "fps": FPS, "size": list(SIZE), "frames": n_frames,
                        "seconds": round(n_frames / FPS, 1), "caption": "MuJoCo simulation · official SO-101 model"
                        + (f" / {extra}" if extra else "")}
    path = session.record(rec)
    res = rec["steps"][0]["result"] if rec.get("steps") else {}
    if rec.get("ok"):
        print(f"OK: the {res['object']} is in the basket ({'upright' if res['upright'] else 'not upright'}), "
              f"{res['attempts']} attempt(s), mask IoU {rec['iou']['mean']}, wall {time.perf_counter() - t0:.1f}s")
    else:
        print(f"NOT DONE: {res.get('note') or rec.get('error')}")
    print(f"record: {path}")
    if writer is not None:
        print(f"video:  {video} ({n_frames} frames, {n_frames / FPS:.1f}s)")
    return 0 if rec.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
