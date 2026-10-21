# Hasami — Meta Global AI Developer Hackathon submission draft

> Draft. Event Oct 26 – Nov 4, 2026; submissions due **Nov 4, 9:00 PM ET** (Nov 5 02:00 UTC).
> Applications close **Oct 25, 11:59 PM ET**. Check the official rules (published before Oct 26) for any
> build-window clause: this repo's first commit is Oct 20, 2026 (UTC).

**Project name:** Hasami (鋏)

**Track:** Indie developer

**Theme:** Push the Frontier (to confirm against the official rules when they are published)

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
- Benchmark of eight task families (all-to-tray, sort by colour, stack two, clear all but one, tower of
  three, sort by shape, line up by size, pick from a bin) on seeded procedural scenes, 20 seeds each:
  **146/160 tasks** with a simulated-click oracle as the planner (labelled; it is not Muse). Six families
  are 20/20; towers of three 12/20 and picking out of a bin 14/20 are the open problems.
  `scripts/bench.py --family all --seeds 20`.
- The same plans in Muse's format with every pick hint 20 px off (written by code, not a model): 76/80, the
  same as with exact clicks, so the grounding layer absorbs a planner's misplaced pixels.
- A less kind camera (2 mm depth noise, 3 mm calibration error, ±30 % lighting): 63/80. Injected grasp
  faults (6 mm offset on every first grasp, 20 % slips): 41/64 with 184 retries.
- Every step is checked the way a real robot could (the gripper's own position, then SAM on the new
  frame) and retried up to twice; the check agreed with the physics in 1172 of 1175 checks.
- SAM 2.1 single-click IoU 0.71–0.92; centre error ≤ 0.6 mm in a 9-pick run (tests assert ≤ 3 mm); yaw
  error max 1.76°, mean 0.18°.
- Agent trace: replay any run step by step (frame, SAM mask vs truth, 3D target, each arm phase's error,
  the check, why it retried).
- **Your desk, twinned:** one desk photo with an A4 sheet becomes a MuJoCo scene ("digital twin from one
  photo · simulated arm"): the sheet gives the plane and camera pose, SAM 2.1 automatic masks find the
  objects, each is fitted as an upright box or cylinder, and the simulated arm runs a task on the twin. On
  20 MuJoCo-rendered photos: 86/86 objects found, 0 false positives, centre error mean 1.2 mm, height mean
  1.6 mm; clear-desk met its goal on 16/20 twins. Not yet measured on a real photo.
- Real-camera tab: webcam or photo, A4 sheet for the table plane, SAM 2.1 on a clicked real object, its
  position and yaw on the table (segment and locate only; the arm stays simulated).
- 181 automated tests: 180 pass (real physics, real SAM weights, real HTTP and MCP, MuJoCo-rendered photos for the twin), 1 skipped (live Muse call).
- MCP server so any agent can `look`, `find` and `pick_place`.

**Honest status:**
- The arm is a MuJoCo simulation of the official SO-101 model (force-limited servos) with perfect depth,
  no sensor noise and ideal lighting by default (one benchmark sheet adds noise, a calibration error and
  lighting jitter: 63/80 instead of 76/80 on the same seeds). A LeRobot follower can be attached in mirror mode (it repeats the
  simulated arm's joint targets); wired and unit-tested, never run on hardware.
- Muse and SAM 3.1 need a Meta Model API key (`MODEL_API_KEY`); the hackathon provides credits on
  acceptance. The real-camera geometry is tested on synthetic cameras; a ruler check on real objects is
  pending. The digital twin is measured only on MuJoCo-rendered photos so far.

**Links:**
- Code: https://github.com/nickthelegend/hasami (create before submitting)
- Demo videos (captioned "MuJoCo simulation · official SO-101 model"): the clean 3-cube run
  (`deployments/runs/20261021T053112874Z`, 84 s) and the run with an injected 6 mm offset where every step
  recovers on its retry (`20261021T053203432Z`, 128 s). Record a narrated 2–3 min version with Muse live
  once the key arrives.
- Landing: `packages/landing/index.html` (host before submitting)

**Team:** Nivesh Gajengi (solo)
