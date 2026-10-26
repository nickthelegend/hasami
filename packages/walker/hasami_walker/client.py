"""Hasami's link to the Thenar Walker robot (thenar-walker/pi, `python -m thenar_pi` on the robot's Raspberry Pi).

The brain connects to the robot's brain WebSocket (port 83) and speaks the phone's messages: `D thr turn`, `S speed`,
`X`, `A 1|0`, `M mask`, `Q joint deg`, `V joint dps`, `P n`, `G`; the robot answers `C{config}`, `T bat on speed latch
q0..q5` at 10 Hz and `P n`. The phone always has priority: while a person drives, the robot answers `E manual`; after a
phone STOP it answers `E paused` until `resume()` (a human decision, never automatic).

Like the phone, the client sends its stick every 40 ms, so the robot's 300 ms failsafe stops the wheels if the brain
hangs or the network drops. Camera frames come from `GET http://<robot>/snap.jpg`.
"""
from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

import websockets

HEARTBEAT_S = 0.04
MANUAL_HOLD_S = 2.2   # the robot gives control back 2 s after the operator's last input (it says nothing then)
JOINTS = ("J1 base pan", "J2 shoulder", "J3 elbow", "J4 wrist flex", "J5 wrist roll", "J6 gripper")


class RobotError(RuntimeError):
    pass


class PhoneHasControl(RobotError):
    """The operator is driving (`E manual`) or pressed STOP (`E paused`): the brain must wait."""


class SetupRequired(RobotError):
    pass


@dataclass
class Telemetry:
    battery_v: float | None = None
    on_mask: int = 0
    speed: int = 1
    latched: bool = False
    q: list[float] = field(default_factory=lambda: [0.0] * 6)
    at: float = 0.0


class Walker:
    def __init__(self, host: str, brain_port: int = 83, http_port: int = 80):
        self.host, self.brain_port, self.http_port = host, brain_port, http_port
        self.config: dict = {}
        self.tele = Telemetry()
        self.state = "idle"            # idle | manual | paused (what the robot last said about the phone)
        self._manual_at = 0.0
        self.rtt_ms: float | None = None
        self._ws = None
        self._stick = (0, 0)
        self._tasks: list[asyncio.Task] = []
        self._pings: dict[int, float] = {}
        self._tele_event = asyncio.Event()

    # ------------------------------------------------------------- connection
    async def connect(self, timeout: float = 5.0) -> "Walker":
        try:
            self._ws = await asyncio.wait_for(websockets.connect(f"ws://{self.host}:{self.brain_port}/"), timeout)
        except (OSError, asyncio.TimeoutError) as e:
            raise RobotError(f"no robot at {self.host}:{self.brain_port} ({e})") from e
        first = await asyncio.wait_for(self._ws.recv(), timeout)
        if not first.startswith("C"):
            raise RobotError(f"unexpected first message {first[:40]!r}")
        self.config = json.loads(first[1:])
        self._tasks = [asyncio.create_task(self._reader()), asyncio.create_task(self._heartbeat())]
        return self

    async def close(self) -> None:
        """Stick to zero, then close: the robot holds the arm where it is."""
        self._stick = (0, 0)
        if self._ws is not None:
            try:
                await self._ws.send("D 0 0")
            except websockets.ConnectionClosed:
                pass
        for t in self._tasks:
            t.cancel()
        if self._ws is not None:
            await self._ws.close()
        self._ws = None

    async def __aenter__(self) -> "Walker":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def _reader(self) -> None:
        try:
            async for m in self._ws:
                if not isinstance(m, str) or not m:
                    continue
                if m[0] == "T":
                    a = m.split()
                    bat = float(a[1])
                    self.tele = Telemetry(bat if bat > 1 else None, int(a[2]), int(a[3]), a[4] == "1",
                                          [float(x) for x in a[5:11]], time.monotonic())
                    self._tele_event.set()
                elif m[0] == "C":
                    self.config = json.loads(m[1:])
                elif m[0] == "P":
                    sent = self._pings.pop(int(m.split()[1]), None)
                    if sent is not None:
                        self.rtt_ms = (time.monotonic() - sent) * 1000
                elif m == "E manual":   # the operator took over: forget our stick, re-command deliberately
                    self._stick = (0, 0)
                    if self.state != "paused":
                        self.state, self._manual_at = "manual", time.monotonic()
                elif m == "E paused":   # phone STOP: never resume an old motion by itself
                    self._stick = (0, 0)
                    self.state = "paused"
                elif m == "E resumed":
                    self.state = "idle"
        except websockets.ConnectionClosed:
            pass

    async def _heartbeat(self) -> None:
        n = 0
        while True:
            try:
                await self._ws.send(f"D {self._stick[0]} {self._stick[1]}")
                n += 1
                if n % 12 == 0:   # ~0.5 s: round trip, like the phone page
                    self._pings[n] = time.monotonic()
                    await self._ws.send(f"P {n}")
            except websockets.ConnectionClosed:
                return
            await asyncio.sleep(HEARTBEAT_S)

    async def _send(self, msg: str) -> None:
        if self._ws is None:
            raise RobotError("not connected")
        await self._ws.send(msg)

    def _refresh_state(self) -> str:
        """'manual' expires on its own: the robot hands control back 2 s after the operator's last input."""
        if self.state == "manual" and time.monotonic() - self._manual_at >= MANUAL_HOLD_S:
            self.state = "idle"
        return self.state

    def _check_control(self) -> None:
        self._refresh_state()
        if self.state == "paused":
            raise PhoneHasControl("the operator pressed STOP on the phone; resume() needs a human's OK")
        if self.state == "manual":
            raise PhoneHasControl("the operator is driving from the phone")

    # ------------------------------------------------------------- wheels
    async def drive(self, thr: int, turn: int) -> None:
        """Stick -100..100 (turn > 0 = left); held until changed, resent every 40 ms."""
        self._check_control()
        self._stick = (max(-100, min(100, int(thr))), max(-100, min(100, int(turn))))
        await self._send(f"D {self._stick[0]} {self._stick[1]}")

    async def drive_for(self, thr: int, turn: int, seconds: float) -> None:
        await self.drive(thr, turn)
        try:
            await asyncio.sleep(seconds)
        finally:
            self._stick = (0, 0)
            await self._send("D 0 0")

    async def stop(self) -> None:
        """STOP: wheels stop and stay stopped until the stick is centred (it is, after this)."""
        self._stick = (0, 0)
        await self._send("X")
        await self._send("D 0 0")

    async def speed(self, s: int) -> None:
        await self._send(f"S {max(0, min(2, int(s)))}")

    async def resume(self) -> None:
        """After a phone STOP: only call this when a person has decided the robot may move again."""
        await self._send("R")
        self.state = "idle"

    # ------------------------------------------------------------- arm
    async def arm_on(self, mask: int | None = None) -> None:
        """Switch joints on; each one jumps to its target, so make sure the targets match the arm first."""
        self._check_control()
        await self._send("A 1" if mask is None else f"M {int(mask)}")

    async def limp(self) -> None:
        await self._send("A 0")

    async def joint(self, i: int, deg: float) -> None:
        self._check_control()
        await self._send(f"Q {int(i)} {float(deg):.1f}")

    async def pose(self, q: list[float]) -> None:
        for i, d in enumerate(q):
            await self.joint(i, d)

    async def wait_pose(self, q: list[float], tol: float = 1.5, timeout: float = 8.0) -> bool:
        """True once every switched-on joint is within tol degrees of q (the robot slews at 120 deg/s)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            on = self.tele.on_mask
            if on and all(abs(self.tele.q[i] - q[i]) <= tol for i in range(6) if (on >> i) & 1):
                return True
            self._tele_event.clear()
            try:
                await asyncio.wait_for(self._tele_event.wait(), 0.5)
            except asyncio.TimeoutError:
                pass
        return False

    async def go(self, q: list[float], tol: float = 1.5, timeout: float = 8.0) -> bool:
        """Move to q. While the operator uses the phone the robot refuses ("E manual"): wait out the operator's hold
        and send again. A phone STOP (paused) raises PhoneHasControl."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self._refresh_state() == "manual":
                await asyncio.sleep(max(0.0, MANUAL_HOLD_S - (time.monotonic() - self._manual_at)) + 0.05)
                continue
            try:
                await self.pose(q)
            except PhoneHasControl:
                if self.state == "paused":
                    raise
                continue
            if await self.wait_pose(q, tol, min(0.6, max(0.0, end - time.monotonic()))):
                return True
            if self.state != "manual":
                return await self.wait_pose(q, tol, max(0.0, end - time.monotonic()))
        return False

    def limits(self) -> list[tuple[float, float]]:
        return list(zip(self.config.get("lo", [0] * 6), self.config.get("hi", [0] * 6)))

    # ------------------------------------------------------------- camera
    def snapshot(self, timeout: float = 5.0) -> bytes:
        try:
            with urllib.request.urlopen(f"http://{self.host}:{self.http_port}/snap.jpg", timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 503:
                raise SetupRequired(e.read().decode(errors="replace").strip() or "Setup required: no camera") from e
            raise RobotError(f"snapshot failed: HTTP {e.code}") from e
        except OSError as e:
            raise RobotError(f"snapshot failed: {e}") from e

    def health(self, timeout: float = 3.0) -> dict:
        with urllib.request.urlopen(f"http://{self.host}:{self.http_port}/health", timeout=timeout) as r:
            return json.loads(r.read())
