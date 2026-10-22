"""Build the landing page's images and mask outlines from two recorded runs (and the twin figure).

    python scripts/landing_assets.py
    python scripts/landing_assets.py --twin 1     # the twin figure from synthetic desk photo seed 1
    python scripts/landing_assets.py --run-gif    # the README's opening animation (the hero run, step by step)

Everything comes from deployments/runs/: the overhead frame a run stored before each attempt, the
SAM 2.1 mask it used, and the frame after the attempt. Nothing is drawn by hand: the outlines are the
exact pixel boundary of each stored mask, printed as SVG paths in the frame's own pixel coordinates
(paste them into packages/landing/index.html). Writes packages/landing/assets/*.webp.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "deployments" / "runs"
OUT = ROOT / "packages" / "landing" / "assets"

HERO_RUN = "20261021T053112874Z"  # clean run, 3/3, no disturbance
REPLAY_RUN = "20261021T053203432Z"  # 6 mm offset injected on every first attempt
REPLAY_BOX = (120, 190, 420, 340)  # x0, y0, x1, y1 in overhead pixels: tray, cubes and the jaws


def mask(run: str, name: str) -> np.ndarray:
    return np.asarray(Image.open(RUNS / run / f"{name}.png").convert("L")) > 127


def outline(m: np.ndarray, dx: float = 0, dy: float = 0) -> str:
    """The longest closed pixel-edge boundary of a boolean mask, as an SVG path (pixel-corner coords)."""
    h, w = m.shape
    p = np.pad(m, 1)
    edges: dict[tuple[int, int], list[tuple[int, int]]] = {}

    def add(a, b):
        edges.setdefault(a, []).append(b)

    # Directed edges with the mask on the left (clockwise in image coordinates).
    for y in range(h):
        for x in range(w):
            if not p[y + 1, x + 1]:
                continue
            if not p[y, x + 1]:
                add((x, y), (x + 1, y))  # top
            if not p[y + 1, x + 2]:
                add((x + 1, y), (x + 1, y + 1))  # right
            if not p[y + 2, x + 1]:
                add((x + 1, y + 1), (x, y + 1))  # bottom
            if not p[y + 1, x]:
                add((x, y + 1), (x, y))  # left
    loops = []
    while edges:
        start = next(iter(edges))
        loop = [start]
        cur = start
        while True:
            nxt = edges[cur].pop()
            if not edges[cur]:
                del edges[cur]
            if nxt == start:
                break
            loop.append(nxt)
            cur = nxt
        loops.append(loop)
    best = max(loops, key=len)
    # Drop collinear vertices.
    pts = []
    n = len(best)
    for i, q in enumerate(best):
        a, c = best[i - 1], best[(i + 1) % n]
        if (q[0] - a[0]) * (c[1] - q[1]) != (q[1] - a[1]) * (c[0] - q[0]):
            pts.append(q)
    return "M" + " L".join(f"{x - dx:g} {y - dy:g}" for x, y in pts) + " Z"


def hull(path: str) -> str:
    """Convex hull of an outline's vertices, same start and direction: a smooth track for the scissors."""
    pts = [tuple(map(float, q.split())) for q in path[1:-2].split(" L")]
    P = sorted(set(pts))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower, upper = [], []
    for q in P:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], q) <= 0:
            lower.pop()
        lower.append(q)
    for q in reversed(P):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], q) <= 0:
            upper.pop()
        upper.append(q)
    h = lower[:-1] + upper[:-1]
    # Image y points down: the outline runs clockwise on screen, so the hull must too (negative shoelace).
    area = sum(h[i][0] * h[(i + 1) % len(h)][1] - h[(i + 1) % len(h)][0] * h[i][1] for i in range(len(h)))
    if area < 0:
        h.reverse()
    i = min(range(len(h)), key=lambda k: (h[k][0] - pts[0][0]) ** 2 + (h[k][1] - pts[0][1]) ** 2)
    h = h[i:] + h[:i]
    return "M" + " L".join(f"{x:g} {y:g}" for x, y in h) + " Z"


def webp(src: Path, dst: Path, box=None) -> None:
    im = Image.open(src).convert("RGB")
    if box:
        im = im.crop(box)
    im.save(dst, "WEBP", quality=90, method=6)
    print(f"wrote {dst.relative_to(ROOT)} {im.size} {dst.stat().st_size} bytes")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out: dict = {}

    rec = json.loads((RUNS / f"{HERO_RUN}.json").read_text())
    webp(RUNS / HERO_RUN / "s0a1-frame.png", OUT / "frame-hero.webp")
    out["hero"] = {
        "run": HERO_RUN,
        "pick_red_cube": outline(mask(HERO_RUN, "s0a1-pick")),
        "place_tray": outline(mask(HERO_RUN, "s0a1-place")),
        "pick_red_cube_hull": "",
        "attempt": {k: rec["steps"][0]["attempts"][0][k] for k in ("pick_point", "place_point", "localisation_error_mm", "grasp")},
        "pick": {k: rec["steps"][0]["attempts"][0]["pick"][k] for k in ("latency_ms", "mask_pixels", "iou_vs_truth", "target")},
        "place_iou": rec["steps"][0]["attempts"][0]["place"]["iou_vs_truth"],
    }

    out["hero"]["pick_red_cube_hull"] = hull(out["hero"]["pick_red_cube"])

    rec = json.loads((RUNS / f"{REPLAY_RUN}.json").read_text())
    x0, y0 = REPLAY_BOX[:2]
    out["replay"] = []
    for s in rec["steps"]:
        for a in s["attempts"]:
            tag = f"s{s['step']}a{a['attempt']}"
            webp(RUNS / REPLAY_RUN / f"{tag}-after.png", OUT / f"replay-{tag}.webp", REPLAY_BOX)
            out["replay"].append({
                "tag": tag,
                "object": s["pick_label"],
                "injected": a.get("injected"),
                "failure": a.get("failure"),
                "phases": [(ph["phase"], ph["error_mm"], ph["ok"]) for ph in a.get("phases", [])],
                "held": a["verify"].get("grasp_held"),
                "verified": a["verify"]["ok"],
                "pick_mask": outline(mask(REPLAY_RUN, f"{tag}-pick"), x0, y0),
            })
    print(json.dumps(out, indent=1))


TWIN_SEED = 1


def twin_figure(seed: int = TWIN_SEED) -> None:
    """The landing's twin figure: a synthetic desk photo (MuJoCo render, labelled so) with the fits drawn,
    beside its twin rendered from the same viewpoint. Same code path as POST /api/twin."""
    import io

    from hasami_core.twin import make_twin, overlay_photo, render_twin, side_by_side
    from hasami_perception import LocalSam2
    from hasami_sim.photo import SheetPose, random_desk, render_photo

    objs, cam = random_desk(seed)
    rgb = render_photo(objs, SheetPose(), cam)
    twin = make_twin(rgb, LocalSam2(), hfov_deg=cam.hfov_deg, photo_source="synthetic")
    png = side_by_side(overlay_photo(rgb, twin), render_twin(twin.spec, twin.width, twin.height),
                       left="synthetic photo (MuJoCo render)")
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im = im.resize((1440, round(im.height * 1440 / im.width)), Image.LANCZOS)
    dst = OUT / "twin-sbs.webp"
    im.save(dst, "WEBP", quality=86, method=6)
    print(f"wrote {dst.relative_to(ROOT)} {im.size} {dst.stat().st_size} bytes")
    print(json.dumps({"seed": seed, "hfov_deg": round(cam.hfov_deg, 1), "stats": twin.stats,
                      "objects": [{"label": f"{o.colour} {o.kind}", "size_mm": o.size_mm, "x_m": w["x_m"], "y_m": w["y_m"]}
                                  for o, w in zip(twin.objects, twin.world)],
                      "truth": [{"label": o.label, "dims_mm": [round(v * 1000, 1) for v in o.dims], "x": round(o.x, 4),
                                 "y": round(o.y, 4)} for o in objs]}, indent=1))


def run_animation(run: str = HERO_RUN) -> None:
    """The README's opening animation: each step of a recorded run as the agent saw it (the stored overhead
    frame with its SAM 2.1 masks tinted: pick mint, place lilac, the same composite GET /api/runs/{id}/steps/{i}.png
    serves) and then the frame after the arm placed it. The pixels are the stored images, unedited; a caption
    bar is added under each frame, never drawn on it. Writes packages/landing/assets/run-hero.webp (animated)."""
    from PIL import ImageDraw, ImageFont

    from hasami_server.app import compose_view

    rec = json.loads((RUNS / f"{run}.json").read_text())
    folder = RUNS / run
    try:
        font = ImageFont.load_default(size=15)
    except TypeError:  # Pillow < 10.1
        font = ImageFont.load_default()
    n = len(rec["steps"])
    frames, durations = [], []

    box = (40, 90, 560, 470)  # the work area: tray, cubes, the arm's base (cropped, never scaled or retouched)

    def captioned(img: Image.Image, text: str) -> Image.Image:
        img = img.crop(box)
        bar = 30
        out = Image.new("RGB", (img.width, img.height + bar), (18, 20, 22))
        out.paste(img, (0, 0))
        ImageDraw.Draw(out).text((10, img.height + 7), text, fill=(232, 236, 230), font=font)
        return out

    for s in rec["steps"]:
        a = s["attempts"][-1]
        tag = f"s{s['step']}a{a['attempt']}"
        obj = s["result"]["object"]
        view = Image.open(io.BytesIO(compose_view(folder / f"{tag}-frame.png",
                                                  [folder / f"{tag}-pick.png", folder / f"{tag}-place.png"])))
        iou = a["pick"]["iou_vs_truth"]
        frames.append(captioned(view.convert("RGB"), f"MuJoCo sim · step {s['step'] + 1}/{n}: SAM 2.1 masks {obj} "
                                                     f"(IoU {iou:.2f}) and the tray"))
        durations.append(1600)
        after = Image.open(folder / f"{tag}-after.png").convert("RGB")
        verdict = "placed, check passed" if s["result"]["ok"] else "not placed"
        frames.append(captioned(after, f"MuJoCo sim · step {s['step'] + 1}/{n}: {obj} {verdict} "
                                       f"({s['result']['error_mm']:.1f} mm from target)"))
        durations.append(1300)
    dst = OUT / "run-hero.webp"
    frames[0].save(dst, "WEBP", save_all=True, append_images=frames[1:], duration=durations, loop=0,
                   quality=80, method=6)
    print(f"wrote {dst.relative_to(ROOT)} {frames[0].size} x {len(frames)} frames {dst.stat().st_size} bytes")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "--twin":
        twin_figure(int(sys.argv[2]))
    elif len(sys.argv) > 1 and sys.argv[1] == "--run-gif":
        run_animation()
    else:
        main()
