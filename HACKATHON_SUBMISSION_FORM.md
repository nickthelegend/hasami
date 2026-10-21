# Hasami — Meta Global AI Developer Hackathon submission draft

> Draft. Event Oct 26 – Nov 4, 2026; submissions due **Nov 4, 9:00 PM ET** (Nov 5 02:00 UTC).
> Applications close **Oct 25, 11:59 PM ET**. Check the official rules (published before Oct 26) for any
> build-window clause: this repo's first commit is Oct 20, 2026 (UTC).

**Project name:** Hasami (鋏)

**Track:** Indie developer

**Theme:** Push the Frontier

**One line:** Say what you want moved: Muse plans from the camera frame, SAM segments each object, depth
turns the mask into a grasp, and an SO-101 arm picks and places it.

**What it does:** Hasami closes the loop from language to a physical grasp. Muse (`muse-spark-1.3` on the
Meta Model API) reads the overhead camera frame and the instruction and returns a validated plan of
pick/place steps with pixel anchors. SAM (2.1 locally, or 3.1 on the Meta Model API) turns each anchor
into a precise mask. Depth lifts the mask to a 3D centre and yaw, and closed-loop inverse kinematics
drives a simulated SO-101 (MuJoCo) to pick and place. Success is read from the physics state.

**Meta models used:**
- *Run (every number below comes from it):* **SAM 2.1** hiera-tiny, open weights, on device, point
  prompts: segmentation for picking and placing, and for checking each step from the camera.
- *Integrated, pending a key (never called yet):* **Muse Spark 1.3** (`muse-spark-1.3`) as the planner:
  instruction + overhead frame -> a validated plan of named objects with pixel hints, which Hasami then
  grounds and checks. **SAM 3.1** (`sam-3.1`, `POST /v1/responses`) for text-prompted segmentation.
  Both model ids and the endpoint are confirmed in the dev.meta.ai docs (read 2026-10-21); the SAM 3.1
  mask payload decoding and Muse's image + forced-tool call on Chat Completions are unverified until the
  key arrives. Without the key the app says "Setup required"; it never fakes a plan.

**How we built it:** Python (MuJoCo + MjSpec, PyTorch + transformers, FastAPI, MCP SDK), a Vite + React
dashboard, the official SO-101 model from TheRobotStudio, LeRobot for the real arm.

**What works today (measured in simulation, scored against ground truth):**
- Benchmark of five task families (all-to-tray, sort by colour into two bins, stack two, clear all but
  one, tower of three) on seeded procedural scenes: **69/70 tasks** with a simulated-click oracle as the
  planner (labelled; it is not Muse). All-to-tray: **90/90 cubes** over 30 scenes, reproducible with
  `scripts/bench.py --family all-to-tray --seeds 30`.
- Every step is checked the way a real robot could (the gripper's own position, then SAM on the new
  frame) and retried up to twice; with injected disturbances (6 mm grasp offset, 20% slips) 78 retries
  recover 64/73 steps. The check agreed with the physics in all 333 checks.
- SAM 2.1 single-click IoU 0.71–0.92; centre error ≤ 0.6 mm in a 9-pick run (tests assert ≤ 3 mm); yaw
  error max 1.76°, mean 0.18°.
- Agent trace: replay any run step by step (frame, SAM mask vs truth, 3D target, each arm phase's error,
  the check, why it retried).
- Real-camera tab: webcam or photo, A4 sheet for the table plane, SAM 2.1 on a clicked real object, its
  position and yaw on the table (segment and locate only; the arm stays simulated).
- 138 automated tests (real physics, real SAM weights, real HTTP and MCP), 1 skipped (live Muse call).
- MCP server so any agent can `look`, `find` and `pick_place`.

**Honest status:**
- The arm is a MuJoCo simulation of the official SO-101 model (force-limited servos) with perfect depth,
  no sensor noise and ideal lighting. A LeRobot follower can be attached in mirror mode (it repeats the
  simulated arm's joint targets); wired and unit-tested, never run on hardware.
- Muse and SAM 3.1 need a Meta Model API key (`MODEL_API_KEY`); the hackathon provides credits on
  acceptance. The real-camera geometry is tested on synthetic cameras; a ruler check on real objects is
  pending.

**Links:**
- Code: https://github.com/nickthelegend/hasami (create before submitting)
- Demo videos (captioned "MuJoCo simulation · official SO-101 model"): the clean 3-cube run
  (`deployments/runs/20261021T053112874Z`, 84 s) and the run with an injected 6 mm offset where every step
  recovers on its retry (`20261021T053203432Z`, 128 s). Record a narrated 2–3 min version with Muse live
  once the key arrives.
- Landing: `packages/landing/index.html` (host before submitting)

**Team:** Nivesh Gajengi (solo)
