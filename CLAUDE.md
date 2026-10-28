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
- The current official SAM documentation uses JavaScript `@meta-sam/parser`. Our Python `meta_sam_parser` hook is unverified and not installed; a compatible mask decoder/bridge is an explicit TODO. With a key set, Walker `look` does not fall back after a SAM 3.1 decode error. Laptop internet is needed alongside the robot network for Meta calls.
- A demo video and a public repo are part of the submission; the video shows real runs only (captions, never edited
  output).

## The robot (Oct 26 2026)
The body is the owner's **Thenar Walker** (github.com/nickthelegend/thenar-walker): a 4-wheel skid-steer printed
PETG tub carrying the **thenar-arms** 6-DoF arm (SO-ARM100 derived, so the SO-101 kinematics in `packages/sim`
are the right family). Its MG996Rs are being replaced by six 25 kg·cm digital positional servos. This Hasami build uses a **Raspberry Pi**, not an ESP32: the Pi runs the robot runtime
(`thenar-walker/pi/`, the Python port of `firmware/thenar_phone`) and the camera; Hasami (this repo) is the brain
that plans, sees and commands it over the robot's WebSocket.

| Part | What it is |
|---|---|
| Base | Thenar Walker tub v2 240 × 138, Ø140 wheels, 4 × 370 gear motors, Cytron **MDD10A** (ch1 = left pair, ch2 = right pair) |
| Arm | 6 × **25 kg·cm digital positional servos** on a **PCA9685** (0x40, 50 Hz, OE active-low); channels 0 pan, 1 shoulder, 2 elbow, 3 wrist flex, 4 wrist roll, 5 gripper. Confirm each label: 180° vs 270°, voltage, spline/fit. |
| Brain | **Raspberry Pi** (model not yet confirmed: assume a Pi 5, check `cat /proc/device-tree/model`) |
| Camera | **Top/deck mast planned**: Pi Camera Module 3 or 3 Wide on CSI preferred; USB webcam also works. Parametric tilt mount: `hardware/camera_mount/camera_mount.scad`. Framing must include shelf item and gripper. Not mounted/tested yet. |
| Power | 3S battery through fuse and KCD4 kill to motor, regulated **6.0 V ≥20 A continuous servo supply** and separate **5.0 V 5 A Pi supply**; all common ground. Split servo power at rated distribution block, not PCA9685 terminal. DS3225-class stall up to 2.3 A × 6 = 13.8 A at 6.8 V; confirm actual labels. |
| Kill switch | KCD4 rocker cutting all power |

**Pi pins (BCM, planned: confirm on the real wiring before trusting wheel directions):** PCA9685 on I2C-1
(SDA GPIO2, SCL GPIO3), OE GPIO17 · MDD10A PWM1 GPIO12, PWM2 GPIO13 (hardware PWM, 20 kHz, `dtoverlay=pwm-2chan`),
DIR1 GPIO5, DIR2 GPIO6 · status LED GPIO27 · battery sense needs an ADC (the Pi has none): ADS1115 on I2C 0x48,
optional; without it the battery reads "n/a", never a made-up number.

**Arm calibration for the new servos:** the old MG996R table and `thenar_pi.armcal.KNOWN`/`seed-known` are **invalid**. Do not seed those numbers. The Pi needs every joint measured anew in the Wi-Fi calibrator (`http://192.168.4.1/calibrator`, **Connect Pi (Wi-Fi)**, WebSocket :82): two physical references ≥10° apart → zero/slope/sign; two safe pulse ends 3–5° before contact; mark all six done → **Save to robot**. Verify `sudo .venv/bin/python -m thenar_pi.armcal show` says `mask=3F complete=1`; record lives at `/var/lib/thenar/armcal.json`. A 270° variant has a different µs/degree slope from 180°. First pulse can jump at full speed; support the arm. See README §5 for the full procedure. The prior J2/J4/J6 jitter history was with MG996R clones; test the new servo/power/channel before drawing conclusions.

## How the pieces talk
- **Robot runtime (Pi)**: `python -m thenar_pi` (repo thenar-walker, folder `pi/`). Same protocol as the ESP32
  firmware, so the phone page and the calibrator work unchanged: page on port 80, one WebSocket on **port 81**
  (`D thr turn`, `S`, `X`, `A`, `M`, `Q joint deg`, `V joint dps`, `P n`, `G`; robot sends `C{json}`, `T ...` at
  10 Hz, `P n`), calibrator line protocol on a WebSocket on port 82, camera snapshot `GET /snap.jpg`.
  Safety is unchanged: 300 ms without a message → wheels stop, arm holds with PWM on.
- **The brain's own channel** is port 83: the same messages at lower priority than the phone. Real phone input takes
  control for 2 s (`E manual`); a phone STOP pauses the brain (`E paused`) until a human says resume (`R`); the
  brain can always STOP; each side has its own 300 ms timeout. `hasami_walker.Walker` is the client (40 ms heartbeat,
  forgets its stick when the operator takes over, so it never resumes an old motion by itself).
- **Brain (this repo)**: planner (Muse) → perception (SAM) → skills (drive, look, reach, grasp, verify) → the robot
  over that WebSocket. In simulation the same skills run on the MuJoCo SO-101 scene.

## Repo map
| Path | Status | What |
|---|---|---|
| `packages/sim` | active | MuJoCo SO-101 scene, DLS IK with sag correction, shape-aware grasps, cameras, ground truth |
| `packages/perception` | active | `LocalSam2`, `MetaSam3`, `locate()`, grounding, colour, real-camera geometry, digital twin |
| `packages/planner` | active | Muse client (forced `submit_plan` tool call, strict validation), `plan_from_clicks()` |
| `packages/core` | active | `Session` (observe, segment, ground, run with verify + retry), benchmark task families, twin |
| `packages/robot` | active | LeRobot SO-101 follower adapter (bus servos); the 25 kg servo Walker is driven through `thenar_pi` instead |
| `packages/walker` | active | `hasami_walker`: the real robot. `Walker` client (port 83), taught poses, `look` (find the item in a robot frame: SAM 3.1 text / SAM 2.1 at a click or a named colour), CLI `python -m hasami_walker status|teach|go|look|snap|drive|limp|resume` |
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
On the Pi: from `/opt/thenar-walker/pi`, create a `--system-site-packages` venv, `sudo .venv/bin/python -m pip install -e '.[pi]'`, then `sudo .venv/bin/python -m thenar_pi` (systemd unit in
`thenar-walker/pi/systemd/`). Put `MODEL_API_KEY` in Hasami's git-ignored `.env` and load it into the shell (the code does not auto-load it). The AP script prompts for the robot Wi-Fi password and stores it in root-owned NetworkManager config. Never paste secrets into tracked files or chat.

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
  every later move. A limp servo drops what it holds: on link loss keep PWM on and hold.
- STOP is a latch released only when the stick is centred; keep the last real stick value while latched or it
  releases itself on the next loop (a mock robot that follows the firmware's rules caught this).
- Calibration records: validate on a copy, keep a CRC, never let a bad number make the stored record unloadable.
  Browser localStorage is per origin including the port.
- Phones on the robot's Wi-Fi: mobile data off. Gamepad / Web Serial / wake-lock need https or localhost.
- Servo current sag browns out small computers: separate regulators for logic and servos, common ground.
- SAM 2.1 weights come from Hugging Face on first use (`HF_HOME`); the first run is slow, later runs are cached.

## Open items
1. Confirm actual Pi model and each 25 kg servo label (DS3225-class only as an example): 180°/270°, 4.8–6.8 V if DS3225, 25T horn fit and printed pocket clearance. Replace old 10 A buck with a 6.0 V ≥20 A continuous supply and direct high-current servo distribution; fit fuse/kill/common ground.
2. Print and fit `hardware/camera_mount/camera_mount.scad` on top of the deck. Pi Camera Module 3/Wide CSI preferred, USB fallback. Adjust height/tilt until shelf item and gripper share a `/snap.jpg` frame. Measure actual camera intrinsics and camera-to-arm transform; no metric depth is provided by Module 3.
3. Flash Pi OS Bookworm, enable I2C and `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4`; verify `i2cdetect -y 1` shows 0x40, camera works, AP/ports and MDD10A wheel directions with wheels lifted.
4. Calibrate **all six new servos** from two references and safe ends via `docs/calibrator`; save to `/var/lib/thenar/armcal.json`. Never use MG996R `seed-known`. Teach `look_shelf` and `carry`, run `look` on real frames.
5. Real fetch reach is **not built yet**: add camera-to-arm/metric target calibration and guarded reach in `packages/walker/hasami_walker/reach.py` (proposed `calibrate_camera_to_arm()`, `plan_reach()`) and a real orchestrator in `packages/walker/hasami_walker/fetch.py` using `Walker.pose()/wait_pose()`; create single-camera verification because simulation uses an overhead camera too.
6. Get `MODEL_API_KEY` from dev.meta.ai for Muse Spark and SAM 3.1, validate live calls and resolve the mask parser interface, then film a truthful real run before Nov 4. Do not claim hardware fetch if the reach/verification step remains unbuilt. `Walker` joint telemetry is commanded/slewed angle, not physical position feedback from the three-wire servos; `wait_pose()` alone cannot verify a grasp or arrival.
