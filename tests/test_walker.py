"""hasami_walker against the real robot runtime: thenar_pi (thenar-walker/pi) runs in-process on its dry-run hardware
(no I2C, GPIO or camera) and the brain talks to it over real WebSockets, exactly as it will to the Raspberry Pi.

thenar_pi comes from a thenar-walker clone next to this repo (or THENAR_WALKER=path); without one these tests skip."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

TW = Path(os.environ.get("THENAR_WALKER", Path(__file__).resolve().parents[2] / "thenar-walker"))
if not (TW / "pi" / "thenar_pi").is_dir():
    pytest.skip(f"no thenar-walker clone at {TW}", allow_module_level=True)
sys.path.insert(0, str(TW / "pi"))

import websockets  # noqa: E402
from thenar_pi import armcal, hw  # noqa: E402
from thenar_pi.robot import Robot  # noqa: E402
from thenar_pi.server import Server  # noqa: E402

from hasami_walker import PhoneHasControl, SetupRequired, Walker, poses  # noqa: E402


def with_robot(tmp_path, body):
    """Run `body(walker, robot, server)` against a fresh robot runtime on free ports."""
    armcal.save(tmp_path, armcal.known())

    async def main():
        robot = Robot(hw.PCA9685(hw.DryBus()), hw.DryPins(), state_dir=str(tmp_path), ssid="Test_TF", dry_run=True)
        robot.boot()
        srv = Server(robot, None, http_port=0, ws_port=0, cal_port=0, brain_port=0, host="127.0.0.1")
        await srv.start()
        try:
            async with Walker("127.0.0.1", brain_port=srv.brain_port, http_port=srv.http_port) as w:
                await body(w, robot, srv)
        finally:
            await srv.stop()
    asyncio.run(main())


async def wait_for(pred, timeout=2.0):
    end = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > end:
            return False
        await asyncio.sleep(0.02)
    return True


def test_connect_config_telemetry_and_round_trip(tmp_path):
    async def body(w, robot, srv):
        assert w.config["usable"] == 63 and w.config["cal"] == 1 and len(w.limits()) == 6
        assert await wait_for(lambda: w.tele.at > 0 and w.rtt_ms is not None, 2.0)
        assert w.tele.battery_v is None                               # no ADC: unknown, not a number
    with_robot(tmp_path, body)


def test_drive_heartbeat_and_disconnect(tmp_path):
    async def body(w, robot, srv):
        await w.drive(60, 0)
        await asyncio.sleep(0.6)                                      # no calls: the 40 ms heartbeat keeps it alive
        assert robot.brain_up and robot.wheel[0] > 0.3
        await w.drive_for(0, 0, 0.1)
        assert await wait_for(lambda: robot.wheel == [0.0, 0.0])
        await w.close()
        assert await wait_for(lambda: not robot.brain_up)
        assert robot.wheel == [0.0, 0.0]
    with_robot(tmp_path, body)


def test_phone_stop_pauses_the_brain_until_resume(tmp_path):
    async def body(w, robot, srv):
        async with websockets.connect(f"ws://127.0.0.1:{srv.ws_port}/") as phone:
            await phone.recv()
            await w.drive(60, 0)
            await asyncio.sleep(0.3)
            await phone.send("X")                                     # the operator presses STOP
            assert await wait_for(lambda: w.state == "paused")        # the brain's heartbeat is refused: "E paused"
            with pytest.raises(PhoneHasControl):
                await w.drive(60, 0)
            assert robot.wheel == [0.0, 0.0]
            for _ in range(60):                                        # phone idle for > 2 s
                await phone.send("D 0 0")
                await asyncio.sleep(0.04)
            await w.resume()
            await asyncio.sleep(0.3)
            assert robot.wheel == [0.0, 0.0]                          # resuming never restarts the old motion
            await w.drive(60, 0)                                      # the brain re-commands it
            assert await wait_for(lambda: robot.wheel[0] > 0.3)
    with_robot(tmp_path, body)


def test_taught_pose_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("HASAMI_WALKER_POSES", str(tmp_path / "poses.json"))

    async def body(w, robot, srv):
        robot.handle("A 1")                                           # a person switches the arm on (phone)
        target = [20.0, -40.0, -30.0, 10.0, 0.0, 30.0]
        assert await w.go(target, tol=1.0, timeout=6.0)               # waits out the phone's 2 s hold, then moves
        assert await wait_for(lambda: abs(w.tele.q[1] + 40) < 1.0)
        poses.save("look_shelf", w.tele.q)
        assert poses.get("look_shelf") == pytest.approx(target, abs=1.0)
        await w.joint(0, 500)                                         # clamped to the calibrated safe end
        assert await wait_for(lambda: robot.goal[0] == pytest.approx(robot.hi[0]))
    with_robot(tmp_path, body)
    with pytest.raises(KeyError, match="teach"):
        poses.get("open_fridge")


def test_snapshot_without_a_camera_says_setup_required(tmp_path):
    async def body(w, robot, srv):
        with pytest.raises(SetupRequired, match="no camera"):
            await asyncio.get_running_loop().run_in_executor(None, w.snapshot)
        h = await asyncio.get_running_loop().run_in_executor(None, w.health)
        assert h["camera"] == "none"
    with_robot(tmp_path, body)
