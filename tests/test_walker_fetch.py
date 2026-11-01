"""Real Walker/thenar_pi sockets with dry-run I/O and file camera; local SAM 2.1 segments synthetic frames."""
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

from thenar_pi import armcal, hw  # noqa: E402
from thenar_pi.camera import Camera  # noqa: E402
from thenar_pi.robot import Robot  # noqa: E402
from thenar_pi.server import Server  # noqa: E402
from hasami_walker.client import Walker  # noqa: E402
from hasami_walker.fetch import fetch  # noqa: E402
from hasami_walker import poses  # noqa: E402
from hasami_walker.reach import save_grid  # noqa: E402


def _socket_available():
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False


def _frame(path, raised=False):
    rgb = np.full((480, 640, 3), (226, 230, 232), dtype=np.uint8)
    rgb[300:312, :] = (170, 176, 180)
    rgb[300:, :] = (205, 210, 214)
    rgb[170:300, 150:200] = (40, 150, 70)
    y0, y1 = (40, 190) if raised else (150, 300)
    rgb[y0:y1, 360:450] = (236, 128, 28)
    rgb[y0:y0 + 12, 360:450] = (250, 248, 240)
    Image.fromarray(rgb).save(path)


@pytest.mark.parametrize("scenario", ["success", "retry", "phone_stop", "outside"])
def test_fetch_file_camera_sam2(tmp_path, monkeypatch, request, scenario):
    if not _socket_available():
        pytest.skip("loopback sockets are unavailable in this sandbox")
    pytest.importorskip("torch")
    pytest.importorskip("transformers")
    sam = request.getfixturevalue("session").sam
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.setenv("HASAMI_RUNS_DIR", str(tmp_path / "runs"))
    monkeypatch.setenv("HASAMI_GRIDS_DIR", str(tmp_path / "grids"))
    monkeypatch.setenv("HASAMI_WALKER_POSES", str(tmp_path / "poses.json"))
    image = tmp_path / "shelf.png"
    _frame(image)
    monkeypatch.setenv("THENAR_CAMERA", f"file:{image}")
    cal = armcal.known()                       # J1-J5 from the known arm; J6 as a parallel gripper, 36 us/mm, 0-50 mm
    cal.zero_us[5], cal.us_per_deg[5], cal.sign[5] = 600, 36, 1
    cal.min_us[5], cal.max_us[5] = 600, 600 + 36 * 50
    armcal.save(tmp_path, cal)

    async def main():
        robot = Robot(hw.PCA9685(hw.DryBus()), hw.DryPins(), state_dir=str(tmp_path), dry_run=True)
        robot.boot()
        camera = Camera()
        server = Server(robot, camera, http_port=0, ws_port=0, cal_port=0, brain_port=0, host="127.0.0.1")
        await server.start()
        try:
            robot.set_mask(63)
            q = [min(max(0.0, lo + 2), hi - 2) for lo, hi in zip(robot.lo[:5], robot.hi[:5])]
            poses.save("look_shelf", q + [30])
            poses.save("carry", q + [30])
            poses.save("place", q + [30])
            spots = []
            for x, y in ((340, 260), (480, 260), (340, 340), (480, 340)):
                spots.append({"pixel": [x, y], "pre_grasp": q + [30], "grasp": q + [30],
                              "lift": q + [30], "mm_per_px": 0.4, "jaw_region": [350, 35, 460, 205]})
            if scenario == "outside":
                for s in spots:
                    s["pixel"][0] -= 200
            save_grid("fridge", {"version": 1, "resolution": [640, 480], "spots": spots})
            j6 = []
            original = robot.handle
            def record(command, source="phone"):
                if command.startswith("Q 5 "):
                    j6.append(float(command.split()[2]))
                return original(command, source)
            robot.handle = record
            events = []
            lifts = 0
            async with Walker("127.0.0.1", brain_port=server.brain_port, http_port=server.http_port) as walker:
                def callback(event):
                    nonlocal lifts
                    events.append(event)
                    if event["step"] == "lift":
                        lifts += 1
                    if event["step"] == "lift" and scenario == "success":
                        _frame(image, raised=True)
                    if event["step"] == "lift" and scenario == "retry" and lifts == 2:
                        _frame(image, raised=True)
                    if event["step"] == "pre_grasp" and scenario == "phone_stop":
                        original("X", source="phone")
                report = await fetch(walker, "bring me the orange juice", item="orange carton", callback=callback,
                                     sam2=sam)
            assert all(g >= 4 for g in j6)
            assert (Path(report["run_dir"]) / "run.json").exists()
            assert (Path(report["run_dir"]) / "events.jsonl").exists()
            assert (Path(report["run_dir"]) / "before-1.png").exists()
            names = [e["step"] for e in events if e["status"] == "ok"]
            assert names[:3] == ["understand", "look_pose", "look"]
            if scenario == "success":
                assert "grasp verified" in report["verdict"]
                assert (Path(report["run_dir"]) / "after-1.png").exists()
            elif scenario == "retry":
                assert sum(e["step"] == "retry" and e["status"] == "ok" for e in events) == 1
            elif scenario == "phone_stop":
                assert "PhoneHasControl" in report["verdict"]
                assert not any(e["step"] == "carry" and e["status"] == "ok" for e in events)
            else:
                assert "outside the taught grid hull" in report["verdict"]
                assert not any(e["step"] == "grip" and e["status"] == "ok" for e in events)
        finally:
            await server.stop()
            camera.close()
    asyncio.run(main())
