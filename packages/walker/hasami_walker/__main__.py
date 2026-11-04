"""Talk to the robot from the brain's computer. The robot's address: --robot or HASAMI_ROBOT (default 192.168.4.1).

    python -m hasami_walker status                 config, telemetry, round trip, camera, who has control
    python -m hasami_walker teach look_shelf       store the arm's current angles under a name (move it with the phone)
    python -m hasami_walker poses                  list the taught poses
    python -m hasami_walker go look_shelf          move the (switched-on) arm to a taught pose, wait until it is there
    python -m hasami_walker snap shelf.jpg         save one camera frame
    python -m hasami_walker drive 40 0 1.0         stick 40 % forward for 1 s, then stop (wheels off the ground first!)
    python -m hasami_walker limp                   every joint limp
    python -m hasami_walker resume                 after a phone STOP: only when a person has decided it may move again
    python -m hasami_walker open [gap_mm]          command the parallel jaws open in mm
    python -m hasami_walker grip <width_mm>        close toward a measured item width, with a 2 mm squeeze
    python -m hasami_walker teach-grid fridge --item 'orange juice carton'   teach 3–12 shelf spots
    python -m hasami_walker fetch 'bring me the juice from the fridge' --grid fridge --dry-plan
                                                   look/plan without moving; omit --dry-plan after inspecting targets
    python -m hasami_walker look "orange juice" [--pose look_shelf] [--point 410,260] [--image photo.jpg] [--out look.png]
                                                   find the item in a camera frame (SAM 3.1 text with MODEL_API_KEY,
                                                   else SAM 2.1 at your click or at the colour you named); moves
                                                   nothing except the optional taught pose
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import poses
from .client import JOINTS, PhoneHasControl, RobotError, SetupRequired, Walker


async def run(a: argparse.Namespace) -> int:
    w = Walker(a.robot, brain_port=a.brain_port, http_port=a.http_port)
    if a.cmd == "poses":
        for name, q in sorted(poses.load().items()):
            print(f"{name:16s} " + "  ".join(f"{x:7.1f}" for x in q))
        print(f"({poses.store()})")
        return 0
    if a.cmd == "fetch":
        from .fetch import fetch
        instruction = " ".join(a.args)
        if not instruction:
            raise ValueError('fetch needs an instruction, e.g. fetch "bring me the juice from the fridge"')
        async with w:
            await asyncio.sleep(0.2)
            result = await fetch(w, instruction, item=a.item, grid_name=a.grid, point=_point(a.point),
                                 drive_back=a.drive_back, dry_plan=a.dry_plan, approach=a.approach,
                                 callback=lambda e: print(f"{e['step']}: {e['status']}"))
        import json
        print(json.dumps({"verdict": result["verdict"], "error": result["error"],
                          "targets": result["targets"], "run_dir": result["run_dir"]}, indent=2))
        return 0 if result["error"] is None and (a.dry_plan or "grasp verified" in result["verdict"]) else 2
    if a.cmd == "look" and a.image:   # a photo instead of the robot's camera: no robot needed
        from .look import decode_jpeg
        rgb = decode_jpeg(open(a.image, "rb").read())
        return _look(rgb, a)
    if a.cmd == "snap":
        try:
            jpg = w.snapshot()
        except SetupRequired as e:
            print(e)
            return 2
        with open(a.args[0] if a.args else "snap.jpg", "wb") as f:
            f.write(jpg)
        print(f"saved {len(jpg)} bytes")
        return 0
    async with w:
        await asyncio.sleep(0.3)   # first telemetry and a round trip
        if a.cmd == "status":
            c = w.config
            print(f"robot {c.get('ssid')}  pca={c.get('pca')}  calibration={c.get('cal')}  usable joints mask={c.get('usable')}")
            t = w.tele
            print(f"battery {t.battery_v if t.battery_v else '--'} V  joints on {t.on_mask:06b}  speed {t.speed}  "
                  f"STOP latched {t.latched}  round trip {w.rtt_ms and round(w.rtt_ms)} ms")
            for i, (lo, hi) in enumerate(w.limits()):
                unit = c.get("units", ["deg"] * 6)[i]
                print(f"  {JOINTS[i]:14s} {t.q[i]:7.1f} {unit}  (allowed {lo:.0f} .. {hi:.0f} {unit})")
            try:
                h = w.health()
                print(f"camera {h.get('camera')}  phone connected {h.get('phone')}  brain paused {h.get('brain_paused')}")
            except OSError as e:
                print(f"health: {e}")
        elif a.cmd == "teach":
            if not a.args:
                print("teach needs a name")
                return 1
            p = poses.save(a.args[0], w.tele.q)
            print(f"taught {a.args[0]} = {w.tele.q} -> {p}")
        elif a.cmd == "teach-grid":
            if len(a.args) != 1:
                raise ValueError("teach-grid needs one shelf name")
            await _teach_grid(w, a)
        elif a.cmd == "go":
            q = poses.get(a.args[0])
            if w.tele.on_mask == 0:   # switching joints on makes them jump to their targets: a person does that
                print("the arm is limp: hold it near its targets and press Arm ON on the phone, then run go again")
                return 1
            await w.pose(q)
            ok = await w.wait_pose(q)
            print("at", a.args[0] if ok else "NOT there yet (blocked? limits?)", w.tele.q)
            return 0 if ok else 1
        elif a.cmd == "drive":
            thr, turn, secs = int(a.args[0]), int(a.args[1]), float(a.args[2])
            await w.drive_for(thr, turn, min(secs, 5.0))
        elif a.cmd in ("open", "grip"):
            if w.tele.on_mask & 32 == 0:
                print("the gripper is limp: press Arm ON on the phone with jaws near the shown target first")
                return 1
            if a.cmd == "grip":
                if len(a.args) != 1:
                    raise ValueError("grip needs the measured item width in mm")
                target = await w.grip(float(a.args[0]))
            else:
                if len(a.args) > 1:
                    raise ValueError("open accepts at most one gap in mm")
                target = await w.open_gripper(float(a.args[0]) if a.args else None)
            print(f"commanded gripper gap {target:.1f} mm (no position or force feedback)")
        elif a.cmd == "look":
            from .look import decode_jpeg
            if a.pose:
                if w.tele.on_mask == 0:
                    print("the arm is limp: press Arm ON on the phone first (or leave out --pose)")
                    return 1
                if not await w.go(poses.get(a.pose)):
                    print(f"could not reach pose {a.pose}")
                    return 1
                await asyncio.sleep(0.4)   # let the arm settle before the frame
            try:
                jpg = await asyncio.get_running_loop().run_in_executor(None, w.snapshot)
            except SetupRequired as e:
                print(e)
                return 2
            open_gap = w.config["hi"][5] if w.config.get("gripper") == "parallel" and w.config.get("cal") else None
            return _look(decode_jpeg(jpg), a, open_gap)
        elif a.cmd == "limp":
            await w.limp()
        elif a.cmd == "resume":
            await w.resume()
            await asyncio.sleep(0.2)
            print("robot says:", w.state)
        else:
            print(__doc__)
            return 1
    return 0


def _look(rgb, a, open_gap_mm: float | None = None) -> int:
    import json

    from .look import FindError, annotate, find
    from .gripper_geometry import estimate_width
    if not a.args:
        print('look needs what to find, e.g.  look "orange juice"')
        return 1
    label = " ".join(a.args)
    point = tuple(float(x) for x in a.point.split(",")) if a.point else None
    try:
        f = find(rgb, label, point=point)
    except FindError as e:
        print(f"not found: {e}")
        return 3
    with open(a.out, "wb") as fh:
        fh.write(annotate(rgb, f, label))
    width = estimate_width(f.bbox[2] - f.bbox[0] + 1, mm_per_pixel=a.mm_per_pixel,
                           distance_mm=a.distance_mm, focal_px=a.focal_px, open_gap_mm=open_gap_mm)
    print(json.dumps({"label": label, **f.summary(), "width_estimate": width,
                      "frame_px": [rgb.shape[1], rgb.shape[0]], "annotated": a.out}))
    if width["scale"] == "unknown":
        print("item width unknown: supply --mm-per-pixel or --distance-mm with --focal-px; no depth sensor is available")
    print("next: teach a shelf grid and run fetch --dry-plan; look itself did not reach or grip")
    return 0


def _point(value):
    if value is None:
        return None
    parts = value.split(",")
    if len(parts) != 2:
        raise ValueError("--point must be x,y")
    return tuple(float(x) for x in parts)


async def _teach_grid(w, a):
    from .look import annotate, decode_jpeg, find
    from .reach import save_grid

    if not 3 <= a.spots <= 12:
        raise ValueError("--spots must be between 3 and 12")
    if w.tele.on_mask != 63:
        raise RobotError("Setup required: press Arm ON on the phone; all six joints must be on before teaching")
    name = a.args[0]
    label = a.item or await asyncio.to_thread(input, "Visible item phrase (include colour without Meta key): ")
    width = float(await asyncio.to_thread(input, "Measure this item's width across the jaws in mm: "))
    if width <= 0:
        raise ValueError("item width must be positive")
    wrist = a.camera == "wrist"
    look = poses.get("look_shelf") if wrist else None
    spots = []
    resolution = None
    jaw_reuse = None
    for i in range(a.spots):
        if wrist:
            await asyncio.to_thread(input, f"Spot {i+1}/{a.spots}: put item there, hands clear of the arm; Enter: the arm "
                                           "moves to look_shelf and takes the photo: ")
            if not await w.go(look, timeout=15):
                raise RobotError("arm did not reach look_shelf")
            await asyncio.sleep(0.6)
            await asyncio.to_thread(w.snapshot)        # discard a frame buffered before the move
        else:
            await asyncio.to_thread(input, f"Spot {i+1}/{a.spots}: put item there, place OPEN jaws around it with phone; Enter to capture grasp: ")
            await asyncio.sleep(0.15)
        rgb = decode_jpeg(await asyncio.to_thread(w.snapshot))
        current_resolution = [rgb.shape[1], rgb.shape[0]]
        if resolution is not None and current_resolution != resolution:
            raise ValueError("camera resolution changed while teaching")
        resolution = current_resolution
        point = _point(a.point) if i == 0 and a.point else None
        if point is None and not os.environ.get("MODEL_API_KEY") and not __import__("hasami_perception.colour", fromlist=["colour_word"]).colour_word(label):
            point = _point(await asyncio.to_thread(input, "Click item in a saved frame; type its x,y pixel: "))
        f = await asyncio.to_thread(find, rgb, label, point)
        from .fetch import _foot, _valid
        _valid(f, rgb.shape)
        image = f"grid-{name}-{i+1}-grasp.png"
        with open(image, "wb") as out:
            out.write(annotate(rgb, f, label))
        if wrist:
            print(f"  photo saved ({image}); item foot at {list(_foot(f))}")
            await asyncio.to_thread(input, "Arm only, wheels must not move. Open the claw (J6 to claw_open) and bring it around "
                                           "the item with the phone, tips just on the floor, don't move the item; "
                                           "Enter to capture grasp: ")
            await asyncio.sleep(0.15)
        grasp = list(w.tele.q)
        await asyncio.to_thread(input, ("Raise the claw straight up about 5 cm with phone" if wrist else
                                        "Back jaws straight out about 5 cm with phone") + "; Enter to capture pre-grasp: ")
        await asyncio.sleep(0.15)
        pre = list(w.tele.q)
        await asyncio.to_thread(input, ("Leave the item on the floor. Move the claw to a safe lifted pose with phone, hands off; Enter: "
                                        "the claw closes on nothing for the empty-claw photo: " if wrist else
                                        "Move jaws to a safe lifted pose with phone; Enter to capture lift: "))
        await asyncio.sleep(0.15)
        lift = list(w.tele.q)
        if wrist:
            await asyncio.sleep(2.5)                   # the phone keeps control for 2 s after its last input
            if not await w.go(lift[:5] + [w._claw("claw_grip")], timeout=12):
                raise RobotError("arm did not hold the lift pose with the claw at claw_grip")
            await asyncio.sleep(0.6)
            await asyncio.to_thread(w.snapshot)        # discard a frame buffered before the move
        jaw_rgb = decode_jpeg(await asyncio.to_thread(w.snapshot))
        with open(f"grid-{name}-{i+1}-lift.png", "wb") as out:
            from .fetch import _raw_png
            out.write(_raw_png(jaw_rgb))
        if wrist and jaw_reuse is not None:
            jaw = jaw_reuse   # the camera rides on the claw: the jaw region is the same in every lift photo
        else:
            prompt = ("In this lift photo, type the region BETWEEN the jaws where a held item would show, x0,y0,x1,y1: "
                      if wrist else "In lift photo, type jaw region x0,y0,x1,y1 (must contain held item): ")
            jaw_text = await asyncio.to_thread(input, prompt)
            jaw = [float(x) for x in jaw_text.split(",")]
            jaw_reuse = jaw
        if len(jaw) != 4 or not (0 <= jaw[0] < jaw[2] < resolution[0] and 0 <= jaw[1] < jaw[3] < resolution[1]):
            raise ValueError("jaw region must be four in-frame coordinates x0,y0,x1,y1")
        pixel_width = f.bbox[2] - f.bbox[0] + 1
        spots.append({"pixel": list(_foot(f)), "pixel_width": pixel_width, "item_width_mm": width,
                      "mm_per_px": width / pixel_width, "grasp": grasp, "pre_grasp": pre, "lift": lift,
                      "jaw_region": jaw})
        if wrist:
            from .fetch import jaw_thumbnail
            spots[-1]["empty_lift"] = jaw_thumbnail(jaw_rgb, jaw)
        print(f"spot {i+1}: foot {spots[-1]['pixel']}, scale {width/pixel_width:.3f} mm/px; photos {image} and lift")
    grid = {"version": 1, "name": name, "resolution": resolution, "spots": spots, "camera": a.camera,
            "note": "Joint targets are commanded telemetry, not measured physical servo angles or gaps."}
    path = save_grid(name, grid)
    print(f"saved {path}; inspect all grid photos and dry-plan before moving the arm autonomously")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m hasami_walker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--robot", default=os.environ.get("HASAMI_ROBOT", "192.168.4.1"))
    ap.add_argument("--brain-port", type=int, default=83)
    ap.add_argument("--http-port", type=int, default=80)
    ap.add_argument("--pose", help="look: taught pose to move to before the frame")
    ap.add_argument("--point", help="look: the item's pixel x,y (an operator's click)")
    ap.add_argument("--image", help="look: use this photo instead of the robot's camera")
    ap.add_argument("--out", default="look.png", help="look: where to write the annotated frame")
    ap.add_argument("--mm-per-pixel", type=float, help="look: measured local image scale at the item")
    ap.add_argument("--distance-mm", type=float, help="look: measured camera-to-item distance")
    ap.add_argument("--focal-px", type=float, help="look: calibrated horizontal focal length in pixels")
    ap.add_argument("--item", help="fetch without Muse: explicit item phrase; teach-grid: visible item phrase")
    ap.add_argument("--grid", default="fridge", help="fetch: taught shelf grid name")
    ap.add_argument("--spots", type=int, default=6, help="teach-grid: number of spots, 3 to 12 (default 6)")
    ap.add_argument("--approach", action="store_true",
                    help="fetch, wrist camera: nudge the wheels until the item sits in the taught area, then use the arm")
    ap.add_argument("--drive-back", type=float, default=0, help="fetch: reverse at low throttle for up to 5 s")
    ap.add_argument("--dry-plan", action="store_true", help="fetch: look and print joint targets, do not command motion")
    ap.add_argument("--camera", choices=("body", "wrist"), default=os.environ.get("HASAMI_CAMERA", "body"),
                    help="teach-grid: camera fixed to the body, or riding on the wrist/claw (photos from look_shelf)")
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(a)))
    except (RobotError, KeyError, ValueError) as e:
        print(f"error: {e}" if not isinstance(e, PhoneHasControl) else f"waiting for the operator: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
