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
export HF_HOME="/Volumes/Extreme SSD/.cache/huggingface"   # where SAM 2.1 weights are cached
.venv/bin/python -m pytest -q
```

## Results (2026-10-21 ~05:40 UTC; Mac, CPU, no MODEL_API_KEY, no SO-101, no webcam)

`138 passed, 1 skipped in 35.68s`

| File | What it checks | Passed | Skipped |
|---|---|---:|---:|
| `tests/test_sim.py` | IK reach < 4 mm on 5 targets; pick-place into the tray (physics); phase callback order; each of six shapes at two yaws into the tray; a tower of three from the measured surface heights | 21 | 0 |
| `tests/test_scene.py` | MjSpec scenes: geometry, colours, containers; dict round trip; reproducible, reachable, non-overlapping layouts; eight objects fit; the classic scene | 10 | 0 |
| `tests/test_perception.py` | SAM 2.1 IoU >= 0.6, localisation <= 3 mm, yaw <= 3 deg (2 seeds x 3 cubes); bare-table click; every shape's IoU, localisation, top height, roundness and grasp width | 5 | 0 |
| `tests/test_colour.py` | HSV colour names for the six scene colours lit and shaded; blobs and nearest-blob search; footprint size / roundness -> grasp axes on synthetic footprints | 12 | 0 |
| `tests/test_grounding.py` | grounding a label + hint: accept, fall back to colour, reject the wrong colour, raised-blob fallback, give up with every try, containers; SAM 3.1 token parser on the documented example and request body; a Muse-style step with a hint 22 px off the cube grounded and placed in the sim | 10 | 0 |
| `tests/test_verify.py` | `HASAMI_DISTURB` parsing; gripper reading empty vs holding; a forced 6 mm offset fails the first attempt and the retry succeeds; a grip that always slips fails as `grasp` after two retries | 4 | 0 |
| `tests/test_core.py` | reset bounds and reproducibility; full 3-step clicked run (physics), exact event order including `verify`, run record; meta segmenter without key | 7 | 0 |
| `tests/test_tasks.py` | five task families: goal predicate true on a hand-placed solution, false when a move is undone or a kept object knocked (3 seeds each); reproducible tasks; sort-by-colour colours; tower order; single-pose predicates | 23 | 0 |
| `tests/test_planner.py` | plan schema, `PlanError.raw`, `submit_plan` tool schema, image only in the user message, exact Setup required text; live Muse call | 12 | 1 (live, needs `MODEL_API_KEY`) |
| `tests/test_robot.py` | sim -> LeRobot conversion; Setup required without / with a missing port; step limit; the sim arm streams joint targets to the hook; RealMirror units against a recording stand-in; a session stays sim-only when the port is missing | 13 | 0 |
| `tests/test_realcam.py` | homography and camera pose from four A4 corners on a synthetic tilted camera; a 50 x 30 x 40 mm box located within 1 mm / 1.5 deg with parallax correction (> 10 mm off without); capture Setup required | 5 | 0 |
| `tests/test_server.py` | status; frames; segment; 503 setup_required; 422s; clicked run over SSE, 409 while busy, recorded; per-step images and composites with IoU matching the record; reset to a task family; CORS; real-camera endpoints (Setup required, order, a synthetic photo through upload -> calibrate -> SAM 2.1 -> plane target within 4 mm) | 15 | 0 |
| `tests/test_mcp.py` | `python -m hasami_mcp` over stdio: tools, status, look, find, Setup required by label, call log | 1 | 0 |
| **Total** | | **138** | **1** |

## Dashboard (not part of pytest)

```bash
cd packages/dashboard && pnpm test     # vite bundles src/runState.test.ts, node --test runs it
```

`2 passed` (2026-10-21, Node 26): the run reducer ignores the previous run's events that
`GET /api/events` replays before `POST /api/run` names the new run, and keeps the new run's own early
events in order. `pnpm build` (tsc + vite) passes.

## Benchmark (not part of pytest)

See the README's tables; JSON in `deployments/bench/`. Commands:

```bash
python scripts/bench.py --family all-to-tray --seeds 30          # 30/30 tasks, 90/90 cubes
python scripts/bench.py --family sort-by-colour,stack-two,clear-except-X,tower-of-3 --seeds 10   # 39/40
HASAMI_DISTURB=offset_mm:6,slip:0.2 python scripts/bench.py --family all --seeds 6               # 19/30, 78 retries
```

The planner in these runs is the simulated-click oracle (labelled in every JSON), not Muse.

## Pending (needs a person, a key or hardware)

- **Real-camera ruler check: PENDING for Nick.** Put an A4 sheet and five real objects on the desk, set
  `HASAMI_REALCAM=0`, Grab, click the sheet's corners, then each object with its height typed; compare the
  reported x / y / size with a ruler, and record the errors here. The agent did not open the webcam.
- **Live Muse plan and SAM 3.1:** need `MODEL_API_KEY`; then `pytest -m live` and
  `scripts/bench.py --planner muse`.
- **Real SO-101 (mirror mode):** needs an arm on `HASAMI_SO101_PORT` and `pip install lerobot[feetech]`;
  check each joint's direction at low torque first.
