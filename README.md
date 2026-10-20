# Hasami (鋏)

Muse plans, SAM sees, an SO-101 picks. Say what you want moved; Muse (Meta Model API) plans the steps
from the camera frame, SAM segments each object, depth turns the mask into a 3D grasp, and an SO-101
arm (MuJoCo physics, or the real follower through LeRobot) does it.

> README stub for the backend. The lead polishes this page (measured numbers, demo video, dashboard).

## Packages

| Package | Import | What |
|---|---|---|
| `packages/sim` | `hasami_sim` | SO-101 MuJoCo scene, DLS IK with closed-loop sag correction, grasp geometry, pick/place skill, overhead + demo cameras, ground truth |
| `packages/perception` | `hasami_perception` | `LocalSam2` (SAM 2.1 hiera-tiny, point prompt), `MetaSam3` (SAM 3.1 on the Meta Model API, text prompt), `locate()` (mask + depth -> centre, top height, yaw) |
| `packages/planner` | `hasami_planner` | Muse client (`openai` SDK, `submit_plan` tool call, strict pydantic validation, `PlanError` with the raw output), `plan_from_clicks()` for the no-key path |
| `packages/core` | `hasami_core` | `Session`: observe, segment, run a plan with events, randomised reset, run records; simulated clicks for the demo/tests |
| `packages/robot` | `hasami_robot` | LeRobot SO-101 follower adapter: sim radians -> LeRobot degrees / gripper 0-100 (BLOCKED without `HASAMI_SO101_PORT`) |
| `packages/server` | `hasami_server` | FastAPI on :8420, REST + SSE, serves `packages/dashboard/dist` at `/` |
| `packages/mcp` | `hasami_mcp` | MCP stdio server: `status`, `look`, `find`, `pick_place` |

## Run

```bash
export UV_CACHE_DIR=...            # optional
uv pip install --python .venv/bin/python -e .
export HF_HOME=/path/to/huggingface-cache   # SAM 2.1 weights (open, downloaded on first use)

.venv/bin/python -m hasami_server              # http://127.0.0.1:8420  (HASAMI_HOST / HASAMI_PORT)
.venv/bin/python -m hasami_mcp                 # MCP over stdio; HASAMI_MCP_LOG=calls.jsonl to log calls
.venv/bin/python scripts/run_demo.py --seed 2  # 3-cube clicked run -> deployments/runs/<id>.json + <id>-demo.mp4
.venv/bin/python -m pytest -q                  # see TESTING.md
```

MCP client config:

```json
{"mcpServers": {"hasami": {"command": "/abs/path/.venv/bin/python", "args": ["-m", "hasami_mcp"],
  "env": {"HF_HOME": "/abs/path/huggingface"}}}}
```

## Setup required (never faked)

| Needs | For | Without it |
|---|---|---|
| `MODEL_API_KEY` | Muse planner, SAM 3.1 text segmentation | `POST /api/plan` and instruction runs return 503 `{"setup_required": "MODEL_API_KEY"}`; clicked runs with SAM 2.1 work |
| `HASAMI_SO101_PORT` + `pip install lerobot[feetech]` | the real SO-101 follower | `status.so101.setup` says so; the simulator is used |

## HTTP API (port 8420)

`GET /api/status`, `GET /api/frame.png?camera=overhead|demo`, `POST /api/segment`, `POST /api/plan`,
`POST /api/run` (409 while a run is in progress), `GET /api/events` (SSE: `plan`, `segment`, `target`,
`phase`, `result`, `done`, `error`; `?once=true` ends after the run), `POST /api/reset`, `GET /api/runs`,
`GET /api/runs/{run_id}`. Contract: `specs/hasami.md`.

## License

MIT, Copyright (c) 2026 Nivesh Gajengi. The SO-101 model under `packages/sim/hasami_sim/assets/so101`
is TheRobotStudio's, Apache-2.0.
