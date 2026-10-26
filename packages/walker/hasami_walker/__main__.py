"""Talk to the robot from the brain's computer. The robot's address: --robot or HASAMI_ROBOT (default 192.168.4.1).

    python -m hasami_walker status                 config, telemetry, round trip, camera, who has control
    python -m hasami_walker teach look_shelf       store the arm's current angles under a name (move it with the phone)
    python -m hasami_walker poses                  list the taught poses
    python -m hasami_walker go look_shelf          move the (switched-on) arm to a taught pose, wait until it is there
    python -m hasami_walker snap shelf.jpg         save one camera frame
    python -m hasami_walker drive 40 0 1.0         stick 40 % forward for 1 s, then stop (wheels off the ground first!)
    python -m hasami_walker limp                   every joint limp
    python -m hasami_walker resume                 after a phone STOP: only when a person has decided it may move again
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
                print(f"  {JOINTS[i]:14s} {t.q[i]:7.1f}  (allowed {lo:.0f} .. {hi:.0f})")
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


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m hasami_walker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd")
    ap.add_argument("args", nargs="*")
    ap.add_argument("--robot", default=os.environ.get("HASAMI_ROBOT", "192.168.4.1"))
    ap.add_argument("--brain-port", type=int, default=83)
    ap.add_argument("--http-port", type=int, default=80)
    a = ap.parse_args()
    try:
        sys.exit(asyncio.run(run(a)))
    except (RobotError, KeyError) as e:
        print(f"error: {e}" if not isinstance(e, PhoneHasControl) else f"waiting for the operator: {e}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
