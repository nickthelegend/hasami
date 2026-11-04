"""Rotary claw (THENAR_GRIPPER=rotary): the real fetch over real Walker/thenar_pi sockets with dry-run I/O and a
file camera. J6 is degrees; open and grip come only from the taught claw_open / claw_grip poses. A colour stand-in
replaces SAM 2.1 so this runs without torch; the segmenter is not what is under test here."""
import asyncio
import os
import socket
import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

TW = Path(os.environ.get("THENAR_WALKER", Path(__file__).resolve().parents[2] / "thenar-walker"))
if not (TW / "pi" / "thenar_pi").is_dir():
    pytest.skip("thenar-walker clone missing", allow_module_level=True)
sys.path.insert(0, str(TW / "pi"))

from thenar_pi import armcal, config, hw  # noqa: E402
from thenar_pi.camera import Camera  # noqa: E402
from thenar_pi.robot import Robot  # noqa: E402
from thenar_pi.server import Server  # noqa: E402
from hasami_perception.colour import colour_mask  # noqa: E402
from hasami_walker import poses  # noqa: E402
from hasami_walker.client import SetupRequired, Walker  # noqa: E402
from hasami_walker.fetch import fetch  # noqa: E402
from hasami_walker.reach import save_grid  # noqa: E402


class ColourSam:
    name = "colour stand-in"

    def segment(self, rgb, point=None):
        return colour_mask(rgb, "orange")


def _frame(path, raised=False):
    rgb = np.full((480, 640, 3), (226, 230, 232), dtype=np.uint8)
    rgb[300:, :] = (205, 210, 214)
    y0, y1 = (40, 190) if raised else (150, 300)
    rgb[y0:y1, 360:450] = (236, 128, 28)
    Image.fromarray(rgb).save(path)


def _socket_available():
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


@pytest.mark.parametrize("scenario", ["success", "retry", "untaught"])
def test_fetch_rotary_claw(tmp_path, monkeypatch, scenario):
    if not _socket_available():
        pytest.skip("loopback sockets are unavailable in this sandbox")
    monkeypatch.setattr(config, "GRIPPER", "rotary")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("HASAMI_CLAW_MAX_MM", raising=False)
    monkeypatch.setenv("HASAMI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("HASAMI_GRIDS_DIR", str(tmp_path / "grids"))
    monkeypatch.setenv("HASAMI_WALKER_POSES", str(tmp_path / "poses.json"))
    image = tmp_path / "shelf.png"
    _frame(image)
    monkeypatch.setenv("THENAR_CAMERA", f"file:{image}")
    armcal.save(tmp_path, armcal.known())        # rotary MG996R record: J6 in degrees

    async def main():
        robot = Robot(hw.PCA9685(hw.DryBus()), hw.DryPins(), state_dir=str(tmp_path), dry_run=True)
        robot.boot()
        camera = Camera()
        server = Server(robot, camera, http_port=0, ws_port=0, cal_port=0, brain_port=0, host="127.0.0.1")
        await server.start()
        try:
            robot.set_mask(63)
            lo6, hi6 = robot.lo[5], robot.hi[5]
            claw_open, claw_grip = round(lo6 + 0.75 * (hi6 - lo6), 1), round(lo6 + 0.3 * (hi6 - lo6), 1)
            q = [min(max(0.0, lo + 2), hi - 2) for lo, hi in zip(robot.lo[:5], robot.hi[:5])]
            for name in ("look_shelf", "carry", "place"):
                poses.save(name, q + [claw_open])
            if scenario != "untaught":
                poses.save("claw_open", q + [claw_open])
                poses.save("claw_grip", q + [claw_grip])
            spots = [{"pixel": [x, y], "pre_grasp": q + [claw_open], "grasp": q + [claw_open],
                      "lift": q + [claw_open], "mm_per_px": 0.4, "jaw_region": [350, 35, 460, 205]}
                     for x, y in ((340, 260), (480, 260), (340, 340), (480, 340))]
            save_grid("fridge", {"version": 1, "resolution": [640, 480], "spots": spots})
            j6 = []
            original = robot.handle

            def record(command, source="phone"):
                if command.startswith("Q 5 "):
                    j6.append(float(command.split()[2]))
                return original(command, source)
            robot.handle = record
            events, lifts = [], 0
            async with Walker("127.0.0.1", brain_port=server.brain_port, http_port=server.http_port) as walker:
                assert walker.rotary()

                def callback(event):
                    nonlocal lifts
                    events.append(event)
                    if event["step"] == "lift":
                        lifts += 1
                        if scenario == "success" or lifts == 2:
                            _frame(image, raised=True)
                report = await fetch(walker, "bring me the orange juice", item="orange carton",
                                     callback=callback, sam2=ColourSam(), sam3=None)
            if scenario == "untaught":
                assert "claw_open" in report["verdict"] and "Setup required" in report["verdict"]
                assert not any(e["step"] == "grip" and e["status"] == "ok" for e in events)
                return
            assert set(j6) <= {claw_open, claw_grip} and claw_grip in j6
            grips = [e for e in events if e["step"] == "grip" and e["status"] == "ok"]
            assert grips and all(e["gap_mm"] == claw_grip for e in grips)
            assert "grasp verified" in report["verdict"], report["verdict"]
            assert any(e["step"] == "release" and e["status"] == "ok" for e in events)
            if scenario == "retry":
                assert sum(e["step"] == "retry" and e["status"] == "ok" for e in events) == 1
        finally:
            await server.stop()
            camera.close()
    asyncio.run(main())


def test_rotary_claw_pose_outside_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("HASAMI_WALKER_POSES", str(tmp_path / "poses.json"))
    w = Walker("127.0.0.1")
    w.config = {"gripper": "rotary", "usable": 63, "lo": [0] * 6, "hi": [90] * 6}
    poses.save("claw_grip", [0, 0, 0, 0, 0, 120])
    with pytest.raises(SetupRequired, match="outside calibrated"):
        w._claw("claw_grip")


def _wrist_frame(path, kind):
    """Wrist camera views: 'floor' = item on the floor seen from look_shelf, 'empty' = floor with nothing,
    'held' = item filling the space between the jaws (lift pose), 'jaws_empty' = lift pose, nothing held."""
    rgb = np.full((480, 640, 3), (205, 210, 214), dtype=np.uint8)
    if kind == "floor":
        rgb[200:330, 360:430] = (236, 128, 28)
    elif kind == "held":
        rgb[300:470, 250:400] = (236, 128, 28)
    Image.fromarray(rgb).save(path)


@pytest.mark.parametrize("scenario", ["success", "retry", "dry_not_at_look", "dry"])
def test_fetch_wrist_camera(tmp_path, monkeypatch, scenario):
    if not _socket_available():
        pytest.skip("loopback sockets are unavailable in this sandbox")
    monkeypatch.setattr(config, "GRIPPER", "rotary")
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setenv("HASAMI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("HASAMI_GRIDS_DIR", str(tmp_path / "grids"))
    monkeypatch.setenv("HASAMI_WALKER_POSES", str(tmp_path / "poses.json"))
    image = tmp_path / "view.png"
    _wrist_frame(image, "floor")
    monkeypatch.setenv("THENAR_CAMERA", f"file:{image}")
    armcal.save(tmp_path, armcal.known())

    async def main():
        robot = Robot(hw.PCA9685(hw.DryBus()), hw.DryPins(), state_dir=str(tmp_path), dry_run=True)
        robot.boot()
        camera = Camera()
        server = Server(robot, camera, http_port=0, ws_port=0, cal_port=0, brain_port=0, host="127.0.0.1")
        await server.start()
        try:
            robot.set_mask(63)
            lo6, hi6 = robot.lo[5], robot.hi[5]
            claw_open, claw_grip = round(lo6 + 0.75 * (hi6 - lo6), 1), round(lo6 + 0.3 * (hi6 - lo6), 1)
            mid = [round((lo + hi) / 2, 1) for lo, hi in zip(robot.lo[:5], robot.hi[:5])]
            look = mid[:]
            reach = [x + 5 for x in mid]
            poses.save("look_shelf", look + [claw_open])
            for name in ("carry", "place", "claw_open"):
                poses.save(name, look + [claw_open])
            poses.save("claw_grip", look + [claw_grip])
            spots = [{"pixel": [x, y], "pre_grasp": reach + [claw_open], "grasp": reach + [claw_open],
                      "lift": reach + [claw_open], "mm_per_px": 0.4, "jaw_region": [200, 280, 450, 479 - 1]}
                     for x, y in ((300, 300), (480, 300), (300, 400), (480, 400))]
            save_grid("floor", {"version": 1, "resolution": [640, 480], "spots": spots, "camera": "wrist"})
            events, lifts = [], 0
            async with Walker("127.0.0.1", brain_port=server.brain_port, http_port=server.http_port) as walker:
                if scenario == "dry":
                    assert await walker.go(look + [claw_open], timeout=10)

                def callback(event):
                    nonlocal lifts
                    events.append(event)
                    ok_attempt = scenario == "success" or lifts == 2
                    if event["step"] == "lift":
                        lifts += 1
                        ok_attempt = scenario == "success" or lifts == 2
                        _wrist_frame(image, "held" if ok_attempt else "jaws_empty")
                    elif event["step"] == "verify_look":
                        _wrist_frame(image, "empty" if ok_attempt else "floor")
                    elif event["step"] == "look_pose":
                        _wrist_frame(image, "floor")
                report = await fetch(walker, "bring me the orange juice", item="orange carton", grid_name="floor",
                                     dry_plan=scenario.startswith("dry"), callback=callback,
                                     sam2=ColourSam(), sam3=None)
            steps_ok = [e["step"] for e in events if e["status"] == "ok"]
            if scenario == "dry_not_at_look":
                assert "go look_shelf" in report["verdict"]
                return
            if scenario == "dry":
                assert report["verdict"].startswith("dry plan complete"), report["verdict"]
                assert "grip" not in steps_ok
                return
            verifies = [e for e in events if e["step"] == "verify"]
            assert "grasp verified" in report["verdict"], (report["verdict"], verifies)
            assert verifies[-1]["camera"] == "wrist" and verifies[-1]["held"]
            if scenario == "retry":
                assert not verifies[0]["held"] and verifies[0]["reason"] == "item still at its old spot"
                assert sum(e["step"] == "retry" and e["status"] == "ok" for e in events) == 1
                assert steps_ok.count("look_pose") == 2
            assert (Path(report["run_dir"]) / "check-1.png").exists()
        finally:
            await server.stop()
            camera.close()
    asyncio.run(main())
