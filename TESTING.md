# Testing

Everything runs for real: MuJoCo physics, SAM 2.1 hiera-tiny weights, FastAPI's TestClient against the
real session, and the MCP server spawned as a subprocess over stdio with the official MCP client.
No external API is mocked. The one test that needs a key (a live Muse plan) is skipped without
`MODEL_API_KEY`, not faked. Two tests use a stand-in on purpose and say so: a recording object in place of
the SO-101 follower (there is no arm here; it checks units and the step limit), and a flood fill in place
of SAM for the grounding decision logic (it checks accept / fall back / give up exactly).

## Command

```bash
uv pip install --python .venv/bin/python -e .
export HF_HOME=/path/to/huggingface-cache   # where SAM 2.1 weights are cached (first run downloads ~150 MB)
.venv/bin/python -m pytest -q
```

## Results (2026-10-21 ~17:30 UTC; Mac, CPU, no MODEL_API_KEY, no SO-101, no webcam)

`180 passed, 1 skipped in 111.74s`

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
| `tests/test_realcam.py` | homography and camera pose from four A4 corners on a synthetic tilted camera; a 50 x 30 x 40 mm box located within 1 mm / 1.5 deg with parallax correction (> 10 mm off without); capture Setup required | 5 | 0 |
| `tests/test_server.py` | status; frames; segment; 503 setup_required; 422s; clicked run over SSE, 409 while busy, recorded; per-step images and composites with IoU matching the record; reset to a task family; CORS; real-camera endpoints; **twin: a rendered photo through POST /api/twin -> side-by-side PNG -> adjust a height -> run clear-desk over SSE, `goal` before `done`, recorded with the twin's label; 422 without a sheet or with bad corners, 400 for a non-image, 404 for an unknown twin** | 17 | 0 |
| `tests/test_mcp.py` | `python -m hasami_mcp` over stdio: tools, status, look, find, Setup required by label, call log | 1 | 0 |
| **Total** | | **180** | **1** |

## Dashboard (not part of pytest)

```bash
cd packages/dashboard && pnpm test     # vite bundles src/runState.test.ts, node --test runs it
```

`2 passed` (2026-10-21, Node 26): the run reducer ignores the previous run's events that
`GET /api/events` replays before `POST /api/run` names the new run, and keeps the new run's own early
events in order. `pnpm build` (tsc + vite) passes.

## Benchmark and twin accuracy (not part of pytest)

See the README's tables; JSON in `deployments/bench/` and `deployments/twins/`. Commands:

```bash
python scripts/bench.py --family all --seeds 20                                   # 146/160
python scripts/bench.py --family all --seeds 10 --planner scripted --hint-px 20   # 76/80
HASAMI_DISTURB=depth_mm:2,cam_mm:3,light:0.3 python scripts/bench.py --family all --seeds 10   # 63/80
HASAMI_DISTURB=offset_mm:6,slip:0.2 python scripts/bench.py --family all --seeds 8              # 41/64, 184 retries
python scripts/twin_accuracy.py --seeds 20 --tasks                                 # 86/86 found, 1.2 mm, 16/20 cleared
```

The planner in these runs is the simulated-click oracle or the scripted Muse-format plan (both labelled in
every JSON), not Muse. The twin's photos are MuJoCo renders.

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
