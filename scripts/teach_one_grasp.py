"""One taught floor grasp. The wheels bring the item to the taught pixel, and the arm repeats the taught grasp.
The code needs a triangle of spots, so three spots around that pixel all carry the same joint targets.
Phase 1: empty-claw photo at lift, then arm down at the grasp with the claw open; a person puts the item between the jaws.
Phase 2: arm up to the look pose, photo of the item, grid saved."""
import asyncio
import json
import sys
from hasami_walker.client import Walker
from hasami_walker.look import annotate, decode_jpeg, find
from hasami_walker.fetch import _foot, _valid, _raw_png, jaw_thumbnail
from hasami_walker.reach import save_grid
from hasami_walker import poses

LABEL = "blue glue stick"
WIDTH = 20.0
JAW = [700.0, 150.0, 1020.0, 550.0]
GRASP = [-15.1, -153.9, 106.2, -35.0, 12.1, -7.3]
PRE = list(GRASP)
PRE[1] = GRASP[1] + 14      # shoulder up about 5 cm
LIFT = list(GRASP)
LIFT[1] = -100.0            # shoulder well up, elbow unchanged


async def snap(w):
    await asyncio.sleep(0.6)
    await asyncio.to_thread(w.snapshot)
    return decode_jpeg(await asyncio.to_thread(w.snapshot))


async def go(w, q, name):
    if not await w.go(q, timeout=20):
        raise SystemExit(f"arm did not reach {name}")
    print("at", name, flush=True)


async def phase1(w):
    await go(w, LIFT[:5] + [w._claw("claw_grip")], "lift, claw closed")
    rgb = await snap(w)
    open("one-lift.png", "wb").write(_raw_png(rgb))
    json.dump(jaw_thumbnail(rgb, JAW), open("one-empty.json", "w"))
    await go(w, LIFT[:5] + [GRASP[5]], "lift, claw open")
    await go(w, GRASP, "grasp")


async def phase2(w):
    await go(w, poses.get("look_shelf"), "look")
    rgb = await snap(w)
    f = await asyncio.to_thread(find, rgb, LABEL, None)
    _valid(f, rgb.shape)
    open("one-look.png", "wb").write(annotate(rgb, f, LABEL))
    fx, fy = _foot(f)
    px_w = f.bbox[2] - f.bbox[0] + 1
    print("foot", fx, fy, "width px", px_w, flush=True)
    h, wd = rgb.shape[:2]
    from PIL import Image; import numpy as np; empty = jaw_thumbnail(np.asarray(Image.open("one-lift.png").convert("RGB")), JAW)
    spots = []
    for dx, dy in ((-250, -50), (250, -50), (0, 100)):
        x, y = fx + dx, fy + dy
        if not (0 <= x < wd and 0 <= y < h):
            print("spot off frame", x, y, flush=True)
        x, y = min(max(0, x), wd - 1), min(max(0, y), h - 1)
        spots.append({"pixel": [x, y], "pixel_width": px_w, "item_width_mm": WIDTH, "mm_per_px": WIDTH / px_w,
                      "grasp": GRASP, "pre_grasp": PRE, "lift": LIFT, "jaw_region": JAW, "empty_lift": empty})
    path = save_grid("floor", {"version": 1, "name": "floor", "resolution": [wd, h], "spots": spots, "camera": "wrist",
                               "note": "One taught grasp; the wheels bring the item to this pixel. Commanded telemetry, no servo feedback."})
    print("saved", path, flush=True)
    poses.save("carry", [0, -70, 80, -35, 12.1, 8.5])
    poses.save("place", [35, -93, 99.2, -35, 12.1, 8.5])


async def phase3(w):
    OPEN = -25.0
    await go(w, GRASP[:5] + [5.0], "grasp, still holding")
    await go(w, GRASP[:5] + [OPEN], "released, wide open")
    await go(w, PRE[:5] + [OPEN], "pre-grasp, wide open")
    await go(w, LIFT[:5] + [OPEN], "lift, wide open")
    p = poses.get("claw_open"); p[5] = OPEN; poses.save("claw_open", p)
    GRASP[5] = PRE[5] = LIFT[5] = OPEN
    await phase2(w)


OPEN = -25.0


async def down(w):
    GRASP[5] = PRE[5] = LIFT[5] = OPEN
    await go(w, LIFT, "lift, wide open")
    await go(w, PRE, "pre-grasp, wide open")
    await go(w, GRASP, "grasp, wide open")


async def up(w):
    GRASP[5] = PRE[5] = LIFT[5] = OPEN
    await phase2(w)


async def main():
    async with Walker("127.0.0.1") as w:
        await asyncio.sleep(2.5)
        await {"1": phase1, "2": phase2, "3": phase3, "down": down, "up": up}[sys.argv[1]](w)


asyncio.run(main())
