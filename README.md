# Hasami (鋏)

**Muse plans, SAM sees, an SO-101 picks.** Say what you want moved. Muse (Meta Model API) looks at the
camera frame and plans the steps, SAM segments each object it names, depth turns each mask into a 3D
grasp, and an SO-101 arm picks and places it.

Built for the [Meta Global AI Developer Hackathon](https://cerebralvalley.ai/e/meta-ai-developer-hackathon)
(Indie track). Hasami is Japanese for scissors, the shape of the SO-101's gripper.

```
"put the red cube in the tray"
   │  Muse (muse-spark-1.3, image + instruction) ── plan: pick "red cube" @ (u,v), place "tray" @ (u,v)
   ▼
SAM 2.1 (local, point prompt)  or  SAM 3.1 (Meta Model API, text prompt) ── mask
   ▼
mask + metric depth ── 3D centre, top height, yaw (minimum-area rectangle)
   ▼
SO-101: closed-loop IK above → descend → close → lift → carry → lower → open   (MuJoCo, or LeRobot on a real arm)
   ▼
result read from the physics state: in the tray or not
```

## What is real, what is waiting

| Part | State |
|---|---|
| Arm | The official SO-101 MuJoCo model (TheRobotStudio, Apache-2.0) with force-limited STS3215 position servos. A pick only counts if the jaws physically hold the cube. |
| Camera | The simulated overhead camera: RGB and metric depth. Ground-truth segmentation is used only to score perception, never as input. |
| SAM 2.1 | `facebook/sam2.1-hiera-tiny`, open weights, runs on this Mac. Works with no key. |
| Clicked runs | Click an object, then where it goes. Fully working without any key. |
| Muse planner | `muse-spark-1.3` at `https://api.meta.ai/v1`. **Setup required: `MODEL_API_KEY`.** Without it `/api/plan` answers 503 `setup_required`; it never returns a canned plan. |
| SAM 3.1 | `sam-3.1` on the Meta Model API (text prompts). **Setup required: `MODEL_API_KEY`.** Response parsing is unverified until a key exists. |
| Real SO-101 | LeRobot adapter with the joint mapping and unit conversion tested. **BLOCKED: no arm attached** (`HASAMI_SO101_PORT` + `pip install lerobot[feetech]`). |

## Measured

| Check | Result |
|---|---|
| Randomised scenes (30 scenes × 3 cubes, yaw ±40°), simulated clicks → SAM 2.1 → depth → pick/place | **90/90 in the tray**, 0 neighbouring cubes disturbed |
| SAM 2.1 single-click IoU vs MuJoCo segmentation | 0.71–0.92 per cube; mean 0.86 on the recorded demo run |
| Localisation from mask + depth | ≤ 0.6 mm centre error in a 9-pick randomised validation run; the tests assert ≤ 3 mm |
| Yaw (minimum-area rectangle on the top face) | max 1.76°, mean 0.18° over 60 cubes |
| Test suite | `53 passed, 1 skipped` (the skip is the live Muse call; see [TESTING.md](TESTING.md)) |
| Recorded demo run | [`deployments/runs/20261020T221658918Z.json`](deployments/runs/20261020T221658918Z.json): 3/3 in the tray; video `hasami-demo.mp4` (shared separately until hosted) |

Two engineering notes that made the numbers real rather than lucky:
- **Sag correction.** The STS3215 servos are force-limited, so the arm lands a few millimetres low and
  off. Each move measures the gripper position and re-aims (up to three corrections).
- **Collision shape of the fixed jaw.** MuJoCo collides meshes as convex hulls, and the fixed finger's
  hull bridged to the palm, so it hit the top of every cube. Two boxes that follow the real finger
  replace it (documented in the vendored MJCF).

## Packages

| Package | Import | What |
|---|---|---|
| [`packages/sim`](packages/sim) | `hasami_sim` | SO-101 scene, DLS IK with sag correction, jaw-aligned grasp geometry, pick/place skill, overhead + demo cameras, ground truth |
| [`packages/perception`](packages/perception) | `hasami_perception` | `LocalSam2`, `MetaSam3`, `locate()` (mask + depth → centre, top height, yaw) |
| [`packages/planner`](packages/planner) | `hasami_planner` | Muse client (`openai` SDK, forced `submit_plan` tool call, strict validation, `PlanError` with the raw output); `plan_from_clicks()` |
| [`packages/core`](packages/core) | `hasami_core` | `Session`: observe, segment, run a plan with events, randomised reset, run records |
| [`packages/robot`](packages/robot) | `hasami_robot` | LeRobot SO-101 follower adapter (BLOCKED without hardware) |
| [`packages/server`](packages/server) | `hasami_server` | FastAPI on :8420, REST + SSE, serves the dashboard |
| [`packages/mcp`](packages/mcp) | `hasami_mcp` | MCP stdio server: `status`, `look`, `find`, `pick_place` |
| [`packages/dashboard`](packages/dashboard) | — | Vite + React: live camera, click-to-pick, SAM mask overlay, Muse instruction, run timeline, results |
| [`packages/landing`](packages/landing) | — | One-page landing |
| [`packages/docs`](packages/docs) | — | Method, sim-to-real notes, limits |

## Run it

```bash
uv venv --python 3.12 && uv pip install -e .
export HF_HOME=/path/to/huggingface-cache        # SAM 2.1 weights (~150 MB, downloaded on first use)
export MODEL_API_KEY=...                          # optional: Muse planner + SAM 3.1 (dev.meta.ai)

(cd packages/dashboard && pnpm install && pnpm build)
python -m hasami_server                           # http://127.0.0.1:8420 — click a cube, click the tray, Run
python scripts/run_demo.py                        # 3-cube run -> deployments/runs/<id>.json + <id>-demo.mp4
python -m pytest -q                               # see TESTING.md
python -m hasami_mcp                              # MCP over stdio (HASAMI_MCP_LOG=calls.jsonl logs every call)
```

MCP client config:

```json
{"mcpServers": {"hasami": {"command": "/abs/path/.venv/bin/python", "args": ["-m", "hasami_mcp"],
  "env": {"HF_HOME": "/abs/path/huggingface", "MODEL_API_KEY": "optional"}}}}
```

## HTTP API (port 8420)

`GET /api/status` · `GET /api/frame.png?camera=overhead|demo` · `POST /api/segment` · `POST /api/plan` ·
`POST /api/run` (409 while busy) · `GET /api/events` (SSE: `plan`, `segment`, `target`, `phase`,
`result`, `done`, `error`) · `POST /api/reset` · `GET /api/runs` · `GET /api/runs/{id}`.

## Limits

- Simulation first: the real arm path is written and its unit conversion tested, but has not moved a
  real SO-101. Real cameras add lighting, blur and calibration error the simulator does not have.
- Objects are boxes resting on a table; the grasp assumes a top-down approach.
- SAM 2.1 needs a point; text prompts ("the red cube") need Muse or SAM 3.1, i.e. a Meta Model API key.
- Not audited for safety around people. Do not run a real arm unattended.

## License

MIT, Copyright (c) 2026 Nivesh Gajengi. The SO-101 model under `packages/sim/hasami_sim/assets/so101` is
TheRobotStudio's, Apache-2.0 (license file included).
