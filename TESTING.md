# Testing

Everything runs for real: MuJoCo physics, SAM 2.1 hiera-tiny weights, FastAPI's TestClient against the
real session, and the MCP server spawned as a subprocess over stdio with the official MCP client.
No external API is mocked. The one test that needs a key (a live Muse plan) is skipped without
`MODEL_API_KEY`, not faked. Two tests use a stand-in on purpose and say so: a recording object in place of
the SO-101 follower (there is no arm here; it checks units and the step limit), and a flood fill in place
of SAM for the grounding decision logic (it checks accept / fall back / give up exactly). Three tests in
`test_plausible.py` inject a fault into the real SAM 2.1 (labelled, like `HASAMI_DISTURB`): chosen prompts
return the whole-table mask the noisy-camera sweep produced, so the target check's retry path runs on demand.

## Command

```bash
scripts/setup.sh                                     # or by hand: uv venv --python 3.12, then the next line
uv pip install --python .venv/bin/python -e '.[dev]'   # pytest is in the [dev] extra, not in the base install
export HF_HOME=/path/to/huggingface-cache   # where SAM 2.1 weights are cached (first run downloads ~150 MB)
.venv/bin/python -m pytest -q
```

## Results (2026-10-22 ~15:15 UTC; Mac, CPU, no MODEL_API_KEY, no SO-101, no webcam)

`213 passed, 1 skipped in 157.02s` (2026-10-22, after the ultra round's server fixes: +9 server tests; 204
before them, 191 before the sheet-orientation and twin-restore fixes, 180 before the target check)

| File | What it checks | Passed | Skipped |
|---|---|---:|---:|
| `tests/test_sim.py` | IK reach < 4 mm on 5 targets; pick-place into the tray (physics); phase callback order; each of six shapes at two yaws into the tray; a tower of three from the measured surface heights | 21 | 0 |
| `tests/test_scene.py` | MjSpec scenes: geometry, colours, containers; dict round trip; reproducible, reachable, non-overlapping layouts; eight objects fit; the classic scene; a measured object (own form, size, colour), an extra camera and a visual-only sheet survive JSON and compile | 11 | 0 |
| `tests/test_perception.py` | SAM 2.1 IoU >= 0.6, localisation <= 3 mm, yaw <= 3 deg (2 seeds x 3 cubes); bare-table click; every shape's IoU, localisation, top height, roundness and grasp width | 5 | 0 |
| `tests/test_colour.py` | HSV colour names for the six scene colours lit and shaded; blobs and nearest-blob search; footprint size / roundness -> grasp axes on synthetic footprints | 12 | 0 |
| `tests/test_grounding.py` | grounding a label + hint: accept, fall back to colour, reject the wrong colour, raised-blob fallback, give up with every try, containers; SAM 3.1 token parser and request body; a Muse-style step with a hint 22 px off grounded and placed in the sim; 'the table' is not searched; **for all eight families, a scripted Muse-format plan with every pick hint 20 px off grounds the named object** (truth label, IoU > 0.6); a scripted line-up with 'the table' places runs in MuJoCo and meets its row goal | 20 | 0 |
| `tests/test_verify.py` | `HASAMI_DISTURB` parsing; gripper reading empty vs holding; a forced 6 mm offset fails the first attempt and the retry succeeds; a grip that always slips fails as `grasp` after two retries; perception disturbances: depth noise sigma ~2 mm, camera offset exactly 3 mm, lighting changed, truth untouched, reproducible, a located cube 1.5-6 mm off | 5 | 0 |
| `tests/test_core.py` | reset bounds and reproducibility; full 3-step clicked run (physics), exact event order including `verify`, run record; meta segmenter without key | 7 | 0 |
| `tests/test_tasks.py` | eight task families: goal predicate true on a hand-placed solution, false when a move is undone or a kept object knocked (3 seeds each); reproducible tasks; sort-by-colour and sort-by-shape moves; tower order; line-up slots free and sizes ordered; `in_row` (reversed, 17 mm off line, touching, tipped); pick-from-bin starts in the bin; single-pose predicates; one seed of line-up, pick-from-bin and sort-by-shape run in MuJoCo by the oracle meets its goal | 42 | 0 |
| `tests/test_twin.py` | digital twin, as round trips through MuJoCo renders: sheet corners within 2 px and the photo's camera within 12 mm (2 random desks); no sheet -> readable error; box and cylinder fits on truth masks within 2 mm height / 1.5 mm sides, typed height kept; SAM 2.1 automatic masks on 2 random desks find every object, nothing extra, centre < 5 mm, yaw < 3 deg, height < 10 mm, sides < 8 mm, shape and colour right; a 2 mm card dropped as flat; the twin renders from the photo's viewpoint and clear-desk on it meets its goal; objects under the base / tray / out of reach say why | 9 | 0 |
| `tests/test_planner.py` | plan schema, `PlanError.raw`, `submit_plan` tool schema, image only in the user message, exact Setup required text; live Muse call | 12 | 1 (live, needs `MODEL_API_KEY`) |
| `tests/test_robot.py` | sim -> LeRobot conversion; Setup required without / with a missing port; step limit; the sim arm streams joint targets to the hook; RealMirror units against a recording stand-in; a session stays sim-only when the port is missing | 13 | 0 |
| `tests/test_realcam.py` | homography and camera pose from four A4 corners on a synthetic tilted camera; a 50 x 30 x 40 mm box located within 1 mm / 1.5 deg with parallax correction (> 10 mm off without); capture Setup required; **the sheet's orientation read from four clicked corners for 6 camera poses x portrait/landscape (fields of view 60-80 deg, 70 assumed): the detected one measures two marks 150 mm apart exactly, the other does not** | 17 | 0 |
| `tests/test_server.py` | status; frames; segment; 503 setup_required; 422s; clicked run over SSE, 409 while busy, recorded; per-step images and composites with IoU matching the record; reset to a task family; CORS; real-camera endpoints, **calibrate `sheet: auto` detects a landscape sheet and an explicit choice is labelled chosen**; **twin: a rendered photo through POST /api/twin -> side-by-side PNG -> adjust a height -> run clear-desk over SSE, `goal` before `done`, recorded with the twin's label; 422 without a sheet or with bad corners, 400 for a non-image, 404 for an unknown twin, GET /api/twin returns the latest twin with its images**; a recorded run's MP4 served (Range too) only when the file is in the runs folder. Ultra round (each failed on the code before its fix): **`sheet: auto` follows the click order** (portrait / landscape x sheet yaw 0/30/50 deg x first click on each corner x both windings: edge 1 -> 2 measures its true length within 2 mm; 24 of 48 were wrong); **local only**: foreign `Host` -> 400, a text/plain, form or foreign-origin upload -> 403 with the frame unchanged, body-less writes need `X-Hasami`; **photo uploads**: 400 MP and 81 MP header-only PNGs -> 400 "photo too large", over 15 MB -> 413; **a real uvicorn keeps answering `/api/status` in under 200 ms during a twin build** (6 ms max; 11 s before); **twins capped** (`HASAMI_MAX_TWINS`, least recently used dropped, folder removed); **malformed input**: off-frame / NaN clicks, seeds outside 0..2^32-1, corners that are not four pairs -> 4xx; SAM 3.1 against a refused connection on 127.0.0.1 -> 502 | 28 | 0 |
| `tests/test_plausible.py` | the target check before any move: a synthetic **594 mm-wide** leak (wider than the jaws, longer than any object, 6 mm tall, off the frame edge) is rejected; a normal cube passes; a lying cylinder and a leaning cube pass, an out-of-reach centre and a noise speck fail, a target inside a bin counts as workspace; a bare-table mask on a real noisy frame locates hundreds of mm wide and is rejected, a cube's mask passes; **retry path** with SAM 2.1 fault-injected: a leaked first mask is re-prompted with a box and the step places the cube; every prompt leaking falls back to the red blob; nothing plausible near a bare-table click skips the step as `implausible` with no pick-and-place phase and no object moved; **the sweep's own case, no injection:** tower-of-3 seed 0 under the noisy camera no longer sends the arm to the 594 mm target (step 2 is `implausible`); a clean run passes the check first time. 6 of these 10 fail on b1d6bfa's code (the other 4 test the new module directly) | 10 | 0 |
| `tests/test_mcp.py` | `python -m hasami_mcp` over stdio: tools, status, look, find, Setup required by label, call log | 1 | 0 |
| **Total** | | **213** | **1** |

## Dashboard (not part of pytest)

```bash
cd packages/dashboard && pnpm test     # vite bundles src/runState.test.ts, node --test runs it
```

`2 passed` (2026-10-21, Node 26): the run reducer ignores the previous run's events that
`GET /api/events` replays before `POST /api/run` names the new run, and keeps the new run's own early
events in order. `pnpm build` (tsc + vite) passes.

## Browser E2E (Playwright in Google Chrome)

```bash
cd e2e && pnpm install && pnpm test:e2e      # builds the dashboard, then runs Playwright
```

`38 passed` (2026-10-22, Playwright 1.63.0, installed Google Chrome; 19 tests x two viewports, desktop
1280x800 and mobile 375x812; 4.1 minutes; 34 before this round). `e2e/serve.sh` starts the real server on 127.0.0.1:8420
(MuJoCo + SAM 2.1; run records go to a throwaway `e2e/.runs` seeded with the committed evidence runs, not
`deployments/runs`, and twins to a throwaway `e2e/.twins`) and the landing on :8421, with no
`MODEL_API_KEY`, `HASAMI_REALCAM` or `HASAMI_SO101_PORT`, so those stay Setup required. Nothing is routed
or stubbed. A test fails on any console error, page error or failed same-origin request it did not declare.

| File | What a user does | Tests per viewport |
|---|---|---:|
| `e2e/tests/dashboard.spec.ts` | status pills and empty states, camera toggle, skip link; Muse Setup required with no plan drawn; clicked steps (add, remove, clear, a bare-table click, keyboard); click a cube, click the tray, Run: run log, results, agent trace (perception / truth, frame after), register, Replay a new and a committed run, Watch a video, reset; the run log follows a run after a reload; task scenes | 6 |
| `e2e/tests/photo.spec.ts` | Real camera: webcam Setup required, a MuJoCo render with an A4 sheet uploaded, four corners clicked, an object located. Twin: a non-image refused, a render becomes a twin, refit with a typed height, shown in the arm's camera, a task run with its goal check, the picker after a reload, the synthetic-photo button. The sheet's orientation detected from the clicked corners (a landscape sheet), then chosen portrait and landscape, the object measuring differently. **The same sheet clicked from its top-right corner: portrait (edge 1 -> 2 is 210 mm), and the object's position matches the top-left frame's, rotated, within 2 mm.** **Keyboard only: Tab to the photo, arrows + Enter on the four corners, Space on the object.** A twin built earlier is shown again after a load, a reload and a tab switch | 6 |
| `e2e/tests/landing.spec.ts` | hero and every image; nav 01-06, hero and footer links, skip link, the quickstart's three commands (venv + install, dashboard build, server); every anchor; the scissors cut once, and stay still under reduced motion; the results sheet matches `deployments/bench/*.json` row for row (32 rows); replay strip; apparatus | 7 |

The photos are MuJoCo renders (`GET /api/twin/sample.png`); the webcam is never opened.

## Benchmark and twin accuracy (not part of pytest)

See the README's tables; JSON in `deployments/bench/` and `deployments/twins/`. Commands:

```bash
python scripts/bench.py --family all --seeds 20                                   # 146/160 (unchanged)
python scripts/bench.py --family all --seeds 10 --planner scripted --hint-px 20   # 76/80 (unchanged)
HASAMI_DISTURB=depth_mm:2,cam_mm:3,light:0.3 python scripts/bench.py --family all --seeds 10   # 64/80 (63/80 before)
HASAMI_DISTURB=offset_mm:6,slip:0.2 python scripts/bench.py --family all --seeds 8              # 41/64, 172 retries (184 before)
python scripts/twin_accuracy.py --seeds 20 --tasks                                 # 86/86 found, 1.2 mm, 16/20 cleared
```

The planner in these runs is the simulated-click oracle or the scripted Muse-format plan (both labelled in
every JSON), not Muse. Before → after the target check, same seeds: clean 146/160 → 146/160 (165535Z →
173703Z), scripted 76/80 → 76/80 (170448Z → 174610Z), noisy camera 63/80 → 64/80 with 51 → 35 retries
(170928Z → 175048Z), grasp faults 41/64 → 41/64 with 184 → 172 retries (171443Z → 175539Z). The old files
are kept; the README has the per-sweep failure breakdown. The twin's photos are MuJoCo renders.

## Pending (needs a person, a key or hardware)

- **Real-camera ruler check: PENDING for Nick.** Put an A4 sheet and five real objects on the desk, set
  `HASAMI_REALCAM=0`, Grab, click the sheet's corners, then each object with its height typed; compare the
  reported x / y / size with a ruler, and record the errors here. The agent did not open the webcam.
- **Digital twin from a real photo: PENDING.** Photograph a desk with an A4 sheet and a few boxes and
  cylinders, upload it in the Twin tab, and compare the reported sizes and positions with a ruler.
- **Live Muse plan and SAM 3.1:** need `MODEL_API_KEY`; then `pytest -m live` and
  `scripts/bench.py --planner muse`.
- **Real SO-101 (mirror mode):** needs an arm on `HASAMI_SO101_PORT` and `pip install lerobot[feetech]`;
  check each joint's direction at low torque first.
