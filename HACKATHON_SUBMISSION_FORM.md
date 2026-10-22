# Hasami — Meta Global AI Developer Hackathon submission draft

> Draft. Event Oct 26 (12:00 PM, per the event page) – Nov 4, 2026; submissions due **Nov 4, 9:00 PM ET** (Nov 5 02:00 UTC).
> Applications close **Oct 25, 11:59 PM ET**.

## Built before the event, built during it

Hasami was started before the event. Its first commit is `5390ae3` (2026-10-20 21:14 UTC), and every commit
up to the tag **`pre-event-2026-10-25`** predates the Oct 26 – Nov 4 window. `pre-event-2026-10-22` marks the
Oct 22 head (980f6cf); commits landed after it on Oct 23 (a hardening round), so the last pre-event commit is
tagged `pre-event-2026-10-25`, the tag to use. It is moved forward if more commits land before the event
opens, so `git log pre-event-2026-10-25..main` is empty today, and
`git log --format='%h %cI' pre-event-2026-10-25..main` must show only commits dated Oct 26 or later.

**Official rules:** the event page says "Official Rules will be published before the Hackathon begins on
October 26, 2026" (read 2026-10-22). They are not out yet, and the page does not say whether work started
before the event may be entered. When they appear, Nick reads them for a build-during-the-event clause and
quotes it here. If there is one, only the in-window work below is claimed.

- **Built before the event (up to `pre-event-2026-10-25`):** the MuJoCo SO-101 simulation and pick/place
  skill, SAM 2.1 perception and grounding, the target check, verify + retry, eight benchmark task families
  and their result sheets, the digital twin from one photo (measured on MuJoCo-rendered photos), the real
  camera tab, the agent trace, the MCP server, the dashboard, the landing, the unit and browser E2E suites.
- **To build during the event (Oct 26 – Nov 4):** the live Muse plan and SAM 3.1 text grounding (both need
  `MODEL_API_KEY`; credits come on acceptance), a Muse row in the benchmark beside the oracle and scripted
  rows, a twin from a real desk photo with a ruler check, and the narrated demo video.
- **In-window commits:** none yet (Oct 23). Once the event is under way,
  `git log --oneline pre-event-2026-10-25..main` lists them; they are listed here before submitting.

**Project name:** Hasami (鋏)

**Track:** Indie developer

**Theme:** Push the Frontier (to confirm against the official rules when they are published)

**One line:** Say what you want moved: Muse plans from the camera frame (wired; it runs once a Meta Model
API key is set, never called yet), SAM segments each object, depth turns the mask into a grasp, and a
simulated SO-101 arm picks and places it. Every number below is SAM 2.1 with a clicked or scripted plan.

**What it does:** Hasami closes the loop from language to a grasp, in simulation. Muse (`muse-spark-1.3` on
the Meta Model API) is to read the overhead camera frame and the instruction and return a validated plan of
pick/place steps with pixel anchors: the client is written and its validation tested, but it waits for
`MODEL_API_KEY` (Setup required) and has never been called, so today a labelled simulated-click oracle or
a scripted plan in Muse's format stands in. SAM (2.1 locally; 3.1 on the Meta Model API once keyed) turns
each anchor into a precise mask. Depth lifts the mask to a 3D centre and yaw, and closed-loop inverse
kinematics drives a simulated SO-101 (MuJoCo) to pick and place. Success is read from the physics state.

**Meta models used:**
- *Run (every number below comes from it):* **SAM 2.1** hiera-tiny, open weights, on device, point
  prompts: segmentation for picking and placing, and for checking each step from the camera.
- *Integrated, pending a key (never called yet):* **Muse Spark 1.3** (`muse-spark-1.3`) as the planner:
  instruction + overhead frame -> a validated plan of named objects with pixel hints, which Hasami then
  grounds and checks. **SAM 3.1** (`sam-3.1`, `POST /v1/responses`) for text-prompted segmentation.
  Both model ids and the endpoint are confirmed in the dev.meta.ai docs (read 2026-10-21); the SAM 3.1
  mask payload decoding and Muse's image + forced-tool call on Chat Completions are unverified until the
  key arrives. SAM 3.1's masks decode only through Meta's `meta-sam-parser`, which is not installed or a
  dependency yet, so a run would use SAM 3.1's box and let SAM 2.1 draw the mask from the box centre.
  Without the key the app says "Setup required"; it never fakes a plan.

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
- A less kind camera (2 mm depth noise, 3 mm calibration error, ±30 % lighting): 64/80 (63/80 before the
  target check). Injected grasp faults (6 mm offset on every first grasp, 20 % slips): 41/64 with 172
  retries.
- Before any move the located target is checked: it must fit the open jaws, match the scene's object
  sizes and heights, sit inside the workspace, and its mask must not run off the frame. A failing
  target gets a boxed SAM re-prompt, then a colour / raised-blob search, then the step is skipped as
  `implausible`. Under the noisy camera the arm used to be sent to the table's middle (once 594 mm wide);
  in that sweep no step ends with the arm aimed at something that is not an object.
- Every step is checked the way a real robot could (the gripper's own position, then SAM on the new
  frame) and retried up to twice; the check agreed with the physics in 1106 of 1111 checks.
- SAM 2.1 single-click IoU on recorded run 20261021T053112874Z: pick masks 0.71–0.95 (cubes), place masks
  0.85–1.00; mean of all six 0.86. Centre error ≤ 0.6 mm in a 9-pick run (tests assert ≤ 3 mm); yaw error
  max 1.76°, mean 0.18°.
- Agent trace: replay any run step by step (frame, SAM mask vs truth, 3D target, each arm phase's error,
  the check, why it retried).
- **Your desk, twinned:** one desk photo with an A4 sheet becomes a MuJoCo scene ("digital twin from one
  photo · simulated arm"): the sheet gives the plane and camera pose, SAM 2.1 automatic masks find the
  objects, each is fitted as an upright box or cylinder, and the simulated arm runs a task on the twin. On
  20 MuJoCo-rendered photos: 86/86 objects found, 0 false positives, centre error mean 1.2 mm, height mean
  1.6 mm; clear-desk met its goal on 16/20 twins. Not yet measured on a real photo.
- The Twin tab restores the twin the server still holds after a reload or a tab switch; at most 8 twins
  are kept (the least recently used is dropped).
- Real-camera tab: webcam or photo, A4 sheet for the table plane (portrait or landscape, read from the
  corners in the order they were clicked, or chosen), SAM 2.1 on a clicked real object, its position and
  yaw on the table (segment and locate only; the arm stays simulated). Corners and the object can be
  marked with the keyboard as well as the mouse.
- The server answers only on 127.0.0.1 / localhost / [::1]. Browsers may call it only from its own origin
  and the dashboard's dev server (an exact list, not every localhost port); other pages get no CORS header
  and their writes get 403. Photo uploads are capped (15 MB, 40 MP) and decoded off the event loop, so a
  twin build never stalls the run log. A twin's desk photo lives in a temporary folder removed when the
  server stops. Run records are written atomically; a damaged one is a readable 503.
- After a server restart mid-run, the dashboard ends the run (from its record if one was written, else
  "the server restarted; this run was lost") instead of showing "running" forever.
- 246 automated tests: 245 pass (real physics, real SAM weights, real HTTP and MCP, MuJoCo-rendered photos
  for the twin), 1 skipped (live Muse call); dashboard unit tests: 6 passed; browser E2E in Google Chrome:
  49 passed, 1 skipped (24 tests x desktop and mobile, plus a server-restart test on desktop only).
- MCP server so any agent can `look`, `find` and `pick_place`.

**Honest status:**
- The arm is a MuJoCo simulation of the official SO-101 model (force-limited servos) with perfect depth,
  no sensor noise and ideal lighting by default (one benchmark sheet adds noise, a calibration error and
  lighting jitter: 64/80 instead of 76/80 on the same seeds). A LeRobot follower can be attached in mirror mode (it repeats the
  simulated arm's joint targets); wired and unit-tested, never run on hardware.
- Muse and SAM 3.1 need a Meta Model API key (`MODEL_API_KEY`); the hackathon provides credits on
  acceptance. The real-camera geometry is tested on synthetic cameras; a ruler check on real objects is
  pending. The digital twin is measured only on MuJoCo-rendered photos so far.

**Links:**
- Code: https://github.com/nickthelegend/hasami (create before submitting)
- Narrated video: **not recorded yet.** To do (Nick): a 2–3 min narrated run of the dashboard, the agent
  trace replay with a retry, the benchmark sheet, the twin and the honest "Setup required" state, captioned
  "simulation", uploaded and linked here and at the top of the README; the opening is re-recorded with Muse
  live once the key arrives. Existing captioned clips ("MuJoCo simulation · official SO-101 model"), not
  public yet (videos are not kept in git): the clean 3-cube run (`deployments/runs/20261021T053112874Z`, 84 s)
  and the run with an injected 6 mm offset where every step recovers on its retry (`20261021T053203432Z`, 128 s).
- Landing: `packages/landing/index.html` (host before submitting: GitHub Pages from `packages/landing`, then
  put its URL here and at the top of the README)

**Team:** Nivesh Gajengi (solo)
