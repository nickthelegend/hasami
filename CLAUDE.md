# Hasami (鋏) — project brief for Claude

Meta **Global AI Developer Hackathon 2026** entry (Indie track, theme "Push the Frontier"; event Oct 26 – Nov 4,
submit by **Nov 4 9 PM ET = Nov 5 07:30 IST**). It is the owner's **only** Meta entry: the other three applications
were rejected for lack of seats. Goal: **say "bring me the juice from the fridge" and a small mobile robot goes,
opens nothing it can't, finds the item with SAM, picks it with a 6-DoF SO-101-class arm, and brings it back.**
Muse Spark plans, SAM 3.1 (Meta) or SAM 2.1 (on device) sees, the robot checks its own grasp and retries.
Owner: nickthelegend (India, builds with a friend, local parts, Bambu P1S + Creality SparkX i7 printers).
Reusable methods and pitfalls from the last robot: [docs/PLAYBOOK.md](docs/PLAYBOOK.md).

## Rules that drive the design
- Commits after the tag `pre-event-2026-10-25` must be dated Oct 26 or later (`git log --format='%h %cI'
  pre-event-2026-10-25..main`). Work done before the event stays clearly before that tag.
- Meta's models are the point: **Muse Spark** (`muse-spark-1.3`, planning) and **SAM 3.1** (`sam-3.1`, segmentation)
  through the Meta Model API (`MODEL_API_KEY` from dev.meta.ai; base URL `MODEL_API_BASE_URL`, default
  `https://api.meta.ai/v1`). Without the key every Meta step says **Setup required** and the on-device SAM 2.1 path
  runs instead. Never fake a model answer.
- A demo video and a public repo are part of the submission; the video shows real runs only (captions, never edited
  output).

## The robot (Oct 26 2026)
The body is the owner's **Thenar Walker** (github.com/nickthelegend/thenar-walker): a 4-wheel skid-steer printed
PETG tub carrying the **thenar-arms** 6-DoF MG996R arm (SO-ARM100 derived, so the SO-101 kinematics in `packages/sim`
are the right family). The brain moves from the ESP32-S3 to a **Raspberry Pi**: the Pi runs the robot runtime
(`thenar-walker/pi/`, the Python port of `firmware/thenar_phone`) and the camera; Hasami (this repo) is the brain
that plans, sees and commands it over the robot's WebSocket.

| Part | What it is |
|---|---|
| Base | Thenar Walker tub v2 240 × 138, Ø140 wheels, 4 × 370 gear motors, Cytron **MDD10A** (ch1 = left pair, ch2 = right pair) |
| Arm | 6 × MG996R on a **PCA9685** (0x40, 50 Hz, OE active-low); channels 0 pan, 1 shoulder, 2 elbow, 3 wrist flex, 4 wrist roll, 5 gripper |
| Brain | **Raspberry Pi** (model not yet confirmed: assume a Pi 5, check `cat /proc/device-tree/model`) |
| Camera | not yet chosen: Pi Camera Module 3 (CSI, Picamera2) or a USB webcam (OpenCV); both are supported |
| Power | ≤ 24 V DC; the Pi needs its own clean 5 V 5 A buck (servo current sag resets a Pi) |
| Kill switch | KCD4 rocker cutting all power |

**Pi pins (BCM, planned: confirm on the real wiring before trusting wheel directions):** PCA9685 on I2C-1
(SDA GPIO2, SCL GPIO3), OE GPIO17 · MDD10A PWM1 GPIO12, PWM2 GPIO13 (hardware PWM, 20 kHz, `dtoverlay=pwm-2chan`),
DIR1 GPIO5, DIR2 GPIO6 · status LED GPIO27 · battery sense needs an ADC (the Pi has none): ADS1115 on I2C 0x48,
optional; without it the battery reads "n/a", never a made-up number.

**Arm calibration** (same arm, same numbers as thenar-walker; the Pi keeps them in `/var/lib/thenar/armcal.json`):
pulse_us = zero + q_deg · us_per_deg · sign, clamped to the safe ends.

| Joint | zero µs | µs/° | sign | safe ends µs | allowed q |
|---|---|---|---|---|---|
| J1 pan | 2360.0 | 10.000 | −1 | 1250–2500 | −14 … 111° |
| J2 shoulder | 755.7 | 8.222 | −1 | 500–1920 | −142 … 31° |
| J3 elbow | 1669.2 | 11.778 | +1 | 500–1860 | −99 … 16° |
| J4 wrist flex | 796.2 | 14.889 | +1 | 500–1660 | −20 … 58° |
| J5 wrist roll | 1920.0 | 10.556 | −1 | 500–2450 | −50 … 134° |
| J6 gripper | 1460.0 | 10.000 | −1 | 500–1640 | −18 … 96° |

Known issue carried over: J2, J4, J6 jumped/oscillated on small steps; do the servo-swap test before blaming code.

## How the pieces talk
- **Robot runtime (Pi)**: `python -m thenar_pi` (repo thenar-walker, folder `pi/`). Same protocol as the ESP32
  firmware, so the phone page and the calibrator work unchanged: page on port 80, one WebSocket on **port 81**
  (`D thr turn`, `S`, `X`, `A`, `M`, `Q joint deg`, `V joint dps`, `P n`, `G`; robot sends `C{json}`, `T ...` at
  10 Hz, `P n`), calibrator line protocol on a WebSocket on port 82, camera snapshot `GET /snap.jpg`.
  Safety is unchanged: 300 ms without a message → wheels stop, arm holds with PWM on.
- **Brain (this repo)**: planner (Muse) → perception (SAM) → skills (drive, look, reach, grasp, verify) → the robot
  over that WebSocket. In simulation the same skills run on the MuJoCo SO-101 scene.

## Repo map
| Path | Status | What |
|---|---|---|
| `packages/sim` | active | MuJoCo SO-101 scene, DLS IK with sag correction, shape-aware grasps, cameras, ground truth |
| `packages/perception` | active | `LocalSam2`, `MetaSam3`, `locate()`, grounding, colour, real-camera geometry, digital twin |
| `packages/planner` | active | Muse client (forced `submit_plan` tool call, strict validation), `plan_from_clicks()` |
| `packages/core` | active | `Session` (observe, segment, ground, run with verify + retry), benchmark task families, twin |
| `packages/robot` | active | LeRobot SO-101 follower adapter (bus servos); the MG996R walker is driven through `thenar_pi` instead |
| `packages/server` | active | FastAPI on :8420, REST + SSE, serves the dashboard |
| `packages/mcp` | active | MCP stdio server: `status`, `look`, `find`, `pick_place` |
| `packages/dashboard` | active | Vite + React viewfinder + lab notebook |
| `packages/landing`, `packages/docs` | active | landing from real run data; method, checks, limits |
| `deployments/runs/` | evidence | recorded runs (JSON + frames); videos stay out of git |

## Build, run, test
Owner's PC is **Windows 11**: give `python` commands, never `.bat` files; write source with the editor tool, not
shell heredocs. The Pi is Linux (reach it with `ssh pi@<host>`).

```
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]" -c constraints.txt
.venv\Scripts\python -m hasami_server          :: http://127.0.0.1:8420
.venv\Scripts\python -m pytest -q              :: see TESTING.md
```
On the Pi: `python3 -m pip install -e thenar-walker/pi` then `python3 -m thenar_pi` (systemd unit in
`thenar-walker/pi/systemd/`). Secrets (`MODEL_API_KEY`, the robot's Wi-Fi password) live in `.env` files that are
git-ignored; never paste them into tracked files or chat.

## Working with this owner
- Wants fast, end-to-end results: build it, test it, run it on the real robot when it's there, push it, then report
  in plain words. Commit + push to `main` of the public repo is expected; big files (videos) go to GitHub releases.
- Short casual messages, often a screenshot or a voice note. Answer the actual question first.
- Often asleep while you work: carry the work through, don't stop to ask; leave a short morning report.
- Never show fake/demo motion where real input is expected; mark anything untested on hardware as untested.
- India: suggest parts sold locally (Robu, The Engineer Store, Kitsguru), prices in ₹.

## Hard-won lessons (details in docs/PLAYBOOK.md)
- Git Bash on the owner's PC mangles backslashes in heredocs: write source files with the editor tool. Windows policy
  blocks `.bat` launchers: give `python ...` commands.
- A hobby servo has no feedback: the first pulse moves at full speed. Start limp, make "arm on" explicit, slew-limit
  every later move. A limp MG996R drops what it holds: on link loss keep PWM on and hold.
- STOP is a latch released only when the stick is centred; keep the last real stick value while latched or it
  releases itself on the next loop (a mock robot that follows the firmware's rules caught this).
- Calibration records: validate on a copy, keep a CRC, never let a bad number make the stored record unloadable.
  Browser localStorage is per origin including the port.
- Phones on the robot's Wi-Fi: mobile data off. Gamepad / Web Serial / wake-lock need https or localhost.
- Servo current sag browns out small computers: separate regulators for logic and servos, common ground.
- SAM 2.1 weights come from Hugging Face on first use (`HF_HOME`); the first run is slow, later runs are cached.

## Open items
1. Confirm the Pi model, camera and pin wiring; flash Raspberry Pi OS Bookworm 64-bit; enable I2C and `pwm-2chan`.
2. Bring up `thenar_pi` on the real robot with the wheels off the ground; confirm wheel directions.
3. Fridge task: mount the camera, calibrate camera-to-arm, record the reach envelope against a real fridge shelf.
4. Get `MODEL_API_KEY` (dev.meta.ai) for Muse Spark + SAM 3.1; until then SAM 2.1 on device.
5. Demo video of a real fetch before Nov 4.
