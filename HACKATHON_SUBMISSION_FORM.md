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
drives an SO-101 to pick and place. Success is read from the physics state.

**Meta models used:** Muse Spark 1.3 (planning and visual grounding), SAM 3.1 (text-prompted
segmentation), SAM 2.1 (open weights, point-prompted segmentation on device).

**How we built it:** Python (MuJoCo, PyTorch + transformers, FastAPI, MCP SDK), a Vite + React
dashboard, the official SO-101 model from TheRobotStudio, LeRobot for the real arm.

**What works today (measured):**
- 90/90 cubes placed across 30 randomised scenes (simulated clicks → SAM 2.1 → depth → pick/place)
- SAM 2.1 single-click IoU 0.71–0.92; centre error ≤ 0.6 mm in a 9-pick validation run (tests assert ≤ 3 mm); yaw error max 1.76°
- 53 automated tests (real physics, real SAM weights, real HTTP and MCP), 1 skipped (live Muse call)
- Dashboard: click a cube, click the tray, Run; or type an instruction (Muse)
- MCP server so any agent can `look`, `find` and `pick_place`

**Honest status:**
- Muse and SAM 3.1 calls need a Meta Model API key (`MODEL_API_KEY`); the hackathon provides credits on
  acceptance. Until then the app says "Setup required" and clicked runs work.
- The arm is simulated (official model, force-limited servos). The LeRobot path for a real SO-101 is
  written and its unit conversion tested, but no real arm has been attached.

**Links:**
- Code: https://github.com/nickthelegend/hasami (create before submitting)
- Demo video: `hasami-demo.mp4` (65 s sim run; record a narrated 2–3 min version with Muse live once the
  key arrives)
- Landing: `packages/landing/index.html` (host before submitting)

**Team:** Nivesh Gajengi (solo)
