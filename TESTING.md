# Testing

Everything runs for real: MuJoCo physics, SAM 2.1 hiera-tiny weights, FastAPI's TestClient against the
real session, and the MCP server spawned as a subprocess over stdio with the official MCP client.
No mocks and no stubbed external APIs. The one test that needs a key (a live Muse plan) is skipped
without `MODEL_API_KEY`, not faked.

## Command

```bash
uv pip install --python .venv/bin/python -e .
export HF_HOME="/Volumes/Extreme SSD/.cache/huggingface"   # where SAM 2.1 weights are cached
.venv/bin/python -m pytest -q
```

## Results (2026-10-20 22:10 UTC = Oct 21 03:40 IST; Mac, CPU, no MODEL_API_KEY, no SO-101 attached)

`53 passed, 1 skipped in 24.07s`

| File | What it checks | Passed | Skipped |
|---|---|---:|---:|
| `tests/test_sim.py` | IK reach error < 4 mm on 5 targets (closed loop, force-limited servos); pick-place puts a cube in the tray (physics); phase callback order | 8 | 0 |
| `tests/test_perception.py` | SAM 2.1 IoU >= 0.6 vs MuJoCo segmentation for every cube; localisation <= 3 mm; yaw <= 3 deg on randomised yaws (2 seeds x 3 cubes); bare-table click has no target | 4 | 0 |
| `tests/test_planner.py` | schema accepts a good plan, rejects out-of-frame points / >6 steps / extra fields / empty; `PlanError.raw`; `submit_plan` tool schema; image only in the user message; `Planner()` without key raises the exact `Setup required: MODEL_API_KEY ...` text; live Muse call | 12 | 1 (live, needs `MODEL_API_KEY`) |
| `tests/test_core.py` | reset bounds (x 0.18-0.27, y -0.1-0.1, separation, yaw +-40 deg, tray fixed) and reproducibility; full 3-step clicked run places all 3 cubes (physics), exact event order, run record contents; meta segmenter without key -> SetupRequired + `error` event | 7 | 0 |
| `tests/test_robot.py` | sim -> LeRobot conversion (keys, degrees, clipping, gripper 0-100, signs/offsets round trip, ranges match the MJCF); `SetupRequired` with the exact text without a port or with a missing port | 9 | 0 |
| `tests/test_server.py` | `/api/status` shape; frame PNG decodes (both cameras); `/api/segment` mask + target + IoU; `/api/plan` and instruction `/api/run` -> 503 `setup_required`; bad steps -> 422; clicked run + SSE stream to `done`, 409 while busy, run recorded and listed; reset; `/`; CORS for localhost only | 12 | 0 |
| `tests/test_mcp.py` | `python -m hasami_mcp` over stdio: tool list, `status`, `look` (PNG + labels), `find` by point (IoU >= 0.6), `find` by label without key is a tool error naming `MODEL_API_KEY`, `HASAMI_MCP_LOG` lines | 1 | 0 |
| **Total** | | **53** | **1** |

## Wider check (not part of `pytest`, run while building)

30 randomised scenes x 3 cubes, simulated clicks -> SAM 2.1 -> depth -> pick/place -> tray, judged from
physics: **90/90 in the tray, 0 neighbouring cubes disturbed**. Yaw estimate over 60 randomised cubes:
max error 1.76 deg, mean 0.18 deg (SAM masks).

Demo run (`scripts/run_demo.py`, record `deployments/runs/20261020T221658918Z.json`, after the scene was
relit): 3/3 in the tray, place error 4.9-8.4 mm, SAM 2.1 IoU mean 0.86 (min 0.71). The suite was re-run
after the relight: `53 passed, 1 skipped in 23.95s`.
