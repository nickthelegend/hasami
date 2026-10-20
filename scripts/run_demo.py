"""Record a 3-cube demo: clicked plan -> SAM 2.1 -> depth -> SO-101 (MuJoCo) -> tray.

The click points are "simulated clicks": derived from the rendered overhead RGB image the way a
person would pick them (colour thresholding, then the middle of each cube's visible blob, and three
separate spots inside the tray). The simulator's ground-truth poses are NOT used as input; they are
only used afterwards to score the run (IoU, localisation error, in-tray check).

Writes deployments/runs/<run_id>.json and deployments/runs/<run_id>-demo.mp4 (demo camera, 960x720,
~30 fps, close to real time).

    HF_HOME=... .venv/bin/python scripts/run_demo.py [--seed 2]
"""

from __future__ import annotations

import argparse
import sys
import time

import imageio.v2 as imageio
import numpy as np

from hasami_core import Session, new_run_id, runs_dir, simulated_click, simulated_drop_clicks

FPS = 30
SIZE = (960, 720)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--seed", type=int, default=2)
    args = ap.parse_args()

    session = Session(seed=args.seed)
    frame = session.observe()
    labels = ["red cube", "green cube", "blue cube"]
    picks = [simulated_click(frame.rgb, label) for label in labels]
    drops = simulated_drop_clicks(frame.rgb, len(labels))
    steps = [{"pick": label, "pick_point": [round(p[0], 1), round(p[1], 1)], "place": "tray",
              "place_point": [round(d[0], 1), round(d[1], 1)], "reason": "simulated click"}
             for label, p, d in zip(labels, picks, drops)]
    for s in steps:
        print(f"simulated click: {s['pick']:>10} at {s['pick_point']} -> tray at {s['place_point']}")

    _ = session.sam  # load SAM before recording starts
    run_id = new_run_id()
    out = runs_dir()
    out.mkdir(parents=True, exist_ok=True)
    video = out / f"{run_id}-demo.mp4"
    writer = imageio.get_writer(video, fps=FPS, codec="libx264", quality=8, macro_block_size=16,
                                ffmpeg_log_level="error")
    arm = session.arm
    n_frames = 0

    def sink(img: np.ndarray) -> None:
        nonlocal n_frames
        writer.append_data(img)
        n_frames += 1

    def hold(seconds: float) -> None:
        img = arm.render("demo", *SIZE)
        for _ in range(int(seconds * FPS)):
            sink(img)

    # One recorded frame every ~1/30 s of simulated time (timestep 2 ms -> every 17 steps).
    arm.record_camera, arm.record_size, arm.record_every, arm.frame_sink = "demo", SIZE, 17, sink

    def on_event(kind: str, data: dict) -> None:
        if kind == "result":
            print(f"  step {data['step']}: {data['object']} in_tray={data['in_tray']} "
                  f"error={data['error_mm']} mm ok={data['ok']}")
        elif kind == "error":
            print(f"  error: {data['message']}", file=sys.stderr)

    t0 = time.perf_counter()
    try:
        hold(1.0)
        rec = session.run(steps, on_event=on_event, run_id=run_id)
        hold(1.5)
    finally:
        arm.record_camera, arm.frame_sink = None, None
        writer.close()
    rec["video"] = {"file": video.name, "camera": "demo", "fps": FPS, "size": list(SIZE), "frames": n_frames,
                    "seconds": round(n_frames / FPS, 1)}
    rec["clicks"] = "simulated: colour-threshold blob centres from the rendered overhead RGB"
    path = session.record(rec)
    print(f"placed {rec['placed']}/{rec['total']}  iou mean {rec['iou']['mean']} min {rec['iou']['min']}  "
          f"wall {time.perf_counter() - t0:.1f}s")
    print(f"record: {path}")
    print(f"video:  {video} ({n_frames} frames, {n_frames / FPS:.1f}s)")
    return 0 if rec["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
