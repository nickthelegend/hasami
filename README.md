# Hasami (鋏)

**Muse plans, SAM sees, an SO-101 picks.** Say what you want moved. Muse (Meta Model API) looks at the
camera frame and plans the steps, SAM segments each object it names, depth turns each mask into a 3D
grasp, and an SO-101 arm picks and places it, checking every step and retrying when it fails.

**Sim-first, scored against ground truth.** The arm is the official SO-101 model in MuJoCo. The pipeline
sees only RGB + depth and the gripper's own position; the simulator's ground truth is used only to score
it. The simulation is kind (perfect depth, no sensor noise, ideal lighting), and every number below is from it.

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon)
(Indie track). Hasami is Japanese for scissors, the shape of the SO-101's gripper.

## Benchmark

`scripts/bench.py` runs seeded task families and checks each goal against the physics state. Planner:
**simulated-click oracle** (it knows which object each step moves and clicks the middle of its colour blob
in the rendered image; it is not a language model, so this measures perception, grasping, placing and
recovery, not planning). Segmenter: SAM 2.1 hiera-tiny, local. Up to 2 retries per step.

| Family | Seeds | Tasks solved | Steps placed | Failures (segment / grasp / place / goal) | Retries | Check agrees |
|---|---:|---:|---:|---|---:|---:|
| all-to-tray | 30 | **30/30** | 90/90 | 0 / 0 / 0 / 0 | 0 | 90/90 |
| sort-by-colour | 10 | **10/10** | 29/29 | 0 / 0 / 0 / 0 | 0 | 29/29 |
| stack-two | 10 | **10/10** | 10/10 | 0 / 0 / 0 / 0 | 0 | 10/10 |
| clear-except-X | 10 | **10/10** | 30/30 | 0 / 0 / 0 / 0 | 0 | 30/30 |
| tower-of-3 | 10 | **9/10** | 19/20 | 0 / 1 / 0 / 0 | 4 | 24/24 |
| **all** | 70 | **69/70** | 178/179 | | 4 | 183/183 |

Failure classes: **segment** (the mask, or the object it led to, was wrong), **grasp** (the jaws never
held it), **place** (held but did not end up at the target), **goal** (every step passed its check but the
final arrangement did not, e.g. a stack shifted). *Check agrees*: how often the robot's own check (gripper
position + camera) matched the physics outcome.

With labelled disturbances injected (`HASAMI_DISTURB=offset_mm:6,slip:0.2`: every step's first grasp is
aimed 6 mm off so the fixed finger lands on the object, and 20% of grips let go after the lift), the
verify-and-retry loop has to do the work:

| Family | Seeds | Tasks solved | Steps placed | Failures (segment / grasp / place / goal) | Retries | Check agrees |
|---|---:|---:|---:|---|---:|---:|
| all-to-tray | 6 | **5/6** | 17/18 | 0 / 1 / 0 / 0 | 19 | 37/37 |
| sort-by-colour | 6 | **4/6** | 17/19 | 1 / 1 / 0 / 0 | 20 | 39/39 |
| stack-two | 6 | **5/6** | 6/6 | 0 / 0 / 0 / 1 | 5 | 11/11 |
| clear-except-X | 6 | **2/6** | 14/18 | 2 / 2 / 0 / 0 | 21 | 38/38 |
| tower-of-3 | 6 | **3/6** | 10/12 | 0 / 2 / 0 / 1 | 13 | 25/25 |
| **all** | 30 | **19/30** | 64/73 | | 78 | 150/150 |

Without disturbances 69/70 tasks pass. All 4 retries were in tower-of-3: in seed 1 the top cube needed two
retries before its check passed; in seed 3 the top cube's first attempt failed its check and both retries
aimed at a re-found blob out of reach, so the step failed (classed grasp). With disturbances, 78 retries recover most steps (64/73
placed; without retries the offset alone would fail every first attempt). What is not recovered is
mostly the disturbance's own damage: a 40 mm cylinder knocked over by the misaimed finger (a lying
cylinder is not graspable top-down) or a cube pushed against a neighbour so the jaws cannot get around it.
In all 333 checks the robot's own verdict (gripper position + camera) matched the physics outcome.

Reproduce:

```bash
python scripts/bench.py --family all-to-tray --seeds 30                      # 30 scenes x 3 cubes
python scripts/bench.py --family sort-by-colour,stack-two,clear-except-X,tower-of-3 --seeds 10
HASAMI_DISTURB=offset_mm:6,slip:0.2 python scripts/bench.py --family all --seeds 6
python scripts/bench.py --family all --seeds 5 --planner muse                # needs MODEL_API_KEY
```

Results: [`20261021T052238Z.json`](deployments/bench/20261021T052238Z.json) (all-to-tray), [`20261021T052444Z.json`](deployments/bench/20261021T052444Z.json) (the other four), [`20261021T052717Z.json`](deployments/bench/20261021T052717Z.json) (disturbed). The Muse planner has not been benchmarked: no key yet
(`--planner muse` prints `Setup required: MODEL_API_KEY` and writes nothing).

## Your desk, twinned

**Digital twin from one photo · simulated arm.** Photograph a desk with an A4 sheet on it. The sheet gives
the table plane and the camera's pose; SAM 2.1, prompted at a grid of points on the plane, finds every
object; each mask is fitted as an upright box or cylinder whose projected silhouette matches it (height
estimated from the silhouette, or typed); MuJoCo builds the scene with the SO-101 beside the sheet and
renders it from the photo's own viewpoint. Then the simulated arm runs a task on the twin (clear the desk,
sort by colour, stack two) with the same grounding, checks and retries, scored by the same goal predicates.

![Synthetic desk photo with the fitted shapes drawn, beside its MuJoCo twin rendered from the same viewpoint](packages/landing/assets/twin-sbs.webp)

*Left: a synthetic photo (MuJoCo render), not a real one. Right: its twin.* Measured on 20 such photos
(`scripts/twin_accuracy.py --seeds 20 --tasks`, [`accuracy-20261021T164701Z.json`](deployments/twins/accuracy-20261021T164701Z.json)),
only the RGB image given to the pipeline:

| | True field of view | Assumed 70° (a real photo's default) |
|---|---:|---:|
| Objects found / false positives | **86/86** / 0 | 83/86 / 1 |
| Shape (box vs cylinder) / colour right | 83/86 / 86/86 | 83/83 / 83/83 |
| Centre error, mean / max | **1.2 / 4.9 mm** | 3.3 / 14.3 mm |
| Yaw error, median / max | 0.35° / 22° | 0.57° / 3.5° |
| Height error, mean / median / max | **1.6** / 0.9 / 13.7 mm | 2.8 / 1.7 / 17.7 mm |
| clear-desk on the twin (simulated-click planner) | **16/20** goals met | — |

`POST /api/twin` (photo body), the dashboard's **Twin** tab (upload, or "Try a synthetic photo"), and
`GET /api/twin/sample.png` for a rendered test photo. Limits: tested on rendered photos only (a real desk
photo is pending); white objects on the sheet and objects the colour of the table are missed; an object over
a sheet corner needs the corners clicked; the field of view is assumed unless known. Method:
[`packages/docs`](packages/docs/README.md#your-desk-twinned-digital-twin-from-one-photo--simulated-arm).

## Pipeline

```
"put the red things in the tray and the blue things in the grey bin"
   │  Muse (muse-spark-1.3, image + instruction) ── plan: pick "red cube" @ hint (u,v), place "tray" @ (u,v)
   ▼
grounding: SAM 2.1 at the hint ─ check size, distance, raised, colour ─ else colour blob ─ else raised blob
           (or SAM 3.1 on the Meta Model API by name, validated the same way)
   ▼
mask + metric depth ── centre, top height, yaw, footprint (square / elongated / round) ── grasp axis + width
   ▼
SO-101 in MuJoCo: closed-loop IK above → descend → close → lift → carry → lower to measured surface → open
   ▼
check: gripper position (held?) + camera (pick spot clear? object at the place spot?) ── fail → re-find, retry ≤ 2
   ▼
score (not used by the robot): physics state, goal predicate
   (optional mirror mode: a LeRobot follower repeats the joint targets; wired, never run on hardware)
```

## What is real, what is waiting

| Part | State |
|---|---|
| Arm | The official SO-101 MuJoCo model (TheRobotStudio, Apache-2.0) with force-limited STS3215 position servos. A pick only counts if the jaws physically hold the object. |
| Camera | Simulated overhead RGB + metric depth. Ground-truth segmentation scores perception; it is never an input. |
| SAM 2.1 | `facebook/sam2.1-hiera-tiny`, open weights, on this Mac. Every number here comes from it. |
| Grasp + image check, retry | Running: gripper position after close / lift / carry, SAM on the frame after each step, re-find and retry. SSE `verify` / `retry`. |
| Scenes | Procedural (`mujoco.MjSpec`): 20/25/30 mm cubes, blocks, flat boxes, 40 mm cylinders, six colours, 4–8 objects, tray + grey bin. Stacking works (place height from the measured surface). |
| Muse planner | `muse-spark-1.3` at `https://api.meta.ai/v1` (model id confirmed in the dev.meta.ai docs). **Setup required: `MODEL_API_KEY`.** Without it `/api/plan` answers 503 `setup_required`; never a canned plan. Grounding of its hints is tested in the sim with deliberately bad hints. |
| SAM 3.1 | `sam-3.1` via `POST /v1/responses` (both confirmed in the docs); the special-token output is parsed per the docs, the mask payload only through Meta's `meta-sam-parser`. **Setup required: `MODEL_API_KEY`; unverified until then.** |
| Real SO-101 | Wired into `Session` behind `HASAMI_SO101_PORT`: a connected LeRobot follower mirrors every joint target the simulated arm is commanded (≤ 6° per joint per command, ~30 Hz). `/api/status` says `"arm": "so101"` only while one is connected. Unit conversion tested; **never run on hardware. BLOCKED: no arm attached** (`HASAMI_SO101_PORT` + `pip install lerobot[feetech]`). |
| Digital twin | One desk photo with an A4 sheet → a MuJoCo scene with the simulated arm (`POST /api/twin`, Twin tab). 86/86 objects found, 1.2 mm mean centre error on 20 rendered photos; **a real photo is pending.** |
| Real camera | Dashboard tab: webcam frame (`HASAMI_REALCAM`) or a photo, A4 sheet / ArUco for the table plane, SAM 2.1 on a clicked object, x / y / yaw / size on the plane (typed height, parallax-corrected). Segment and locate only; the arm stays simulated. Geometry tested on synthetic cameras; **ruler check on real objects pending.** |

## Perception, measured

| Check | Result |
|---|---|
| SAM 2.1 single-click IoU vs MuJoCo segmentation (classic scene) | 0.71–0.92 per cube; mean 0.86, min 0.71 on the recorded demo run |
| Localisation from mask + depth | ≤ 0.6 mm centre error in a 9-pick run; tests assert ≤ 3 mm |
| Yaw (fourth-order moment / minimum-area rectangle) | max 1.76°, mean 0.18° over 60 cubes |
| Real-camera geometry (synthetic tilted camera, 50 × 30 × 40 mm box) | centre within 1 mm, yaw within 1.5° with parallax correction; > 10 mm off without it |
| Recorded demo runs (replay them in the dashboard's Agent trace) | [`20261021T053112874Z`](deployments/runs/20261021T053112874Z.json): 3/3 in the tray, no retries. [`20261021T053203432Z`](deployments/runs/20261021T053203432Z.json): 6 mm offset injected on every first attempt, each first attempt fails at the descent, each retry places the cube: 3/3. Videos (captioned "MuJoCo simulation · official SO-101 model") are shared separately until hosted. |
| Test suite | `138 passed, 1 skipped` (the skip is the live Muse call) (see [TESTING.md](TESTING.md)) |

## Watch the agent think

Every server run keeps, per step and attempt, the overhead frame before and after, the SAM masks and the
truth masks. The dashboard's **Agent trace** shows them as a vertical timeline (plan → mask → 3D target →
each arm phase's error → gripper + camera check → why it retried), with a perception / truth toggle, and
can replay any recorded run. `GET /api/runs/{id}/steps/{i}.png?kind=view|view_truth|frame|after|pick|place`.

## Packages

| Package | Import | What |
|---|---|---|
| [`packages/sim`](packages/sim) | `hasami_sim` | SO-101 scene, procedural scenes (MjSpec), DLS IK with sag correction, shape-aware grasps, stacking, gripper-position grasp check, cameras, ground truth, synthetic desk photos (`photo.py`) |
| [`packages/perception`](packages/perception) | `hasami_perception` | `LocalSam2` (single and batched point prompts), `MetaSam3`, `locate()`, grounding, colour, real-camera geometry, digital-twin geometry (sheet, automatic masks, box / cylinder fits) |
| [`packages/planner`](packages/planner) | `hasami_planner` | Muse client (`openai` SDK, forced `submit_plan` tool call, strict validation, `PlanError` with the raw output); `plan_from_clicks()`; plan sources `muse`, `clicks`, `scripted` |
| [`packages/core`](packages/core) | `hasami_core` | `Session` (observe, segment, ground, run with verify + retry, labelled disturbances, records), eight benchmark task families, the digital twin (scene, render, tasks) |
| [`packages/robot`](packages/robot) | `hasami_robot` | LeRobot SO-101 follower adapter and `RealMirror` (mirror mode; untested on hardware, BLOCKED without it) |
| [`packages/server`](packages/server) | `hasami_server` | FastAPI on :8420, REST + SSE (runs, real camera, `/api/twin`), serves the dashboard |
| [`packages/mcp`](packages/mcp) | `hasami_mcp` | MCP stdio server: `status`, `look`, `find`, `pick_place` |
| [`packages/dashboard`](packages/dashboard) | — | Vite + React, a camera viewfinder beside a lab notebook: live camera with SAM masks drawn as a cut line, click-to-pick, 3D target readout, Muse instruction, scene picker, run log, agent trace + replay, Twin tab, real camera |
| [`packages/landing`](packages/landing) | — | One page built from real run data: the overhead frame of a recorded run with its SAM mask outlines, the benchmark sheets, a contact sheet of a disturbed run, the twin figure (`scripts/landing_assets.py` rebuilds the images and outlines) |
| [`packages/docs`](packages/docs) | — | Method, checks, digital twin, Meta API confirmed vs unverified, sim-to-real, limits |

## Run it

```bash
uv venv --python 3.12 && uv pip install -e .
export HF_HOME=/path/to/huggingface-cache        # SAM 2.1 weights (~150 MB, downloaded on first use)
export MODEL_API_KEY=...                          # optional: Muse planner + SAM 3.1 (dev.meta.ai)

(cd packages/dashboard && pnpm install && pnpm build)
python -m hasami_server                           # http://127.0.0.1:8420 — click a cube, click the tray, Run
python scripts/run_demo.py                        # 3-cube run -> deployments/runs/<id>.json + images + captioned video
python scripts/bench.py --family all --seeds 5    # benchmark (see above)
python -m pytest -q                               # see TESTING.md
python -m hasami_mcp                              # MCP over stdio (HASAMI_MCP_LOG=calls.jsonl logs every call)
```

Optional: `HASAMI_DISTURB=offset_mm:6,slip:0.2` (labelled disturbances), `HASAMI_SO101_PORT` (real arm,
mirror mode), `HASAMI_REALCAM=0` (webcam for the Real camera tab; opened only when you press Grab).

MCP client config:

```json
{"mcpServers": {"hasami": {"command": "/abs/path/.venv/bin/python", "args": ["-m", "hasami_mcp"],
  "env": {"HF_HOME": "/abs/path/huggingface", "MODEL_API_KEY": "optional"}}}}
```

## HTTP API (port 8420)

`GET /api/status` · `GET /api/frame.png?camera=overhead|demo` · `POST /api/segment` · `POST /api/plan` ·
`POST /api/run` (409 while busy) · `GET /api/events` (SSE: `plan`, `segment`, `target`, `phase`, `verify`,
`retry`, `result`, `done`, `error`) · `POST /api/reset` (`{seed?, family?}`) · `GET /api/runs` ·
`GET /api/runs/{id}` · `GET /api/runs/{id}/steps/{i}.png` · `POST /api/realcam/grab|upload|calibrate|segment` ·
`GET /api/realcam/frame.png`.

## Limits

- Simulation first, and a kind one: perfect metric depth, no sensor noise, ideal lighting, rigid objects.
  The real-arm path (mirror mode) is wired and its unit conversion tested, but has not moved a real
  SO-101. The real-camera path is tested on synthetic geometry only.
- Top-down grasps; objects narrower than ~38 mm across one side; cylinders shorter than 40 mm slip out of
  the hinged jaw in this model.
- SAM 2.1 needs a point; text prompts need Muse or SAM 3.1, i.e. a Meta Model API key.
- Not audited for safety around people. Do not run a real arm unattended.

## License

MIT, Copyright (c) 2026 Nivesh Gajengi. The SO-101 model under `packages/sim/hasami_sim/assets/so101` is
TheRobotStudio's, Apache-2.0 (license file included).
