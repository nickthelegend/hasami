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
uv pip install --python .venv/bin/python -e '.[dev]' -c constraints.txt   # pytest is in [dev]; the tested versions
export HF_HOME=/path/to/huggingface-cache   # where SAM 2.1 weights are cached (first run downloads ~150 MB)
.venv/bin/python -m pytest -q
```

## Results (2026-10-25; Mac, CPU, no MODEL_API_KEY, no SO-101, no webcam)

`249 passed, 1 skipped in 183.93s` (2026-10-25: +1 server test for the dashboard's cache headers, +1 twin test
and +2 docs tests from the Oct 24 deploy fixes; 245 on 2026-10-23 after the third ultra round: +9 server, +4 planner,
+1 perception, +5 docs tests; 226 before it, 213 before the second ultra round, 204 before the first,
180 before the target check)

**Fresh clone (2026-10-22):** `git clone` of this repository into an empty folder, `uv venv --python 3.12`,
`uv pip install -e '.[dev]'` with an empty uv cache, then `python -m pytest -q --collect-only`: every test
file collected (219 tests at that commit), nothing missing. With a plain editable install (no `[dev]` extra)
pytest is not installed, which is why every install line now says `'.[dev]'` (`tests/test_docs.py` checks them).

| File | What it checks | Passed | Skipped |
|---|---|---:|---:|
| `tests/test_sim.py` | IK reach < 4 mm on 5 targets; pick-place into the tray (physics); phase callback order; each of six shapes at two yaws into the tray; a tower of three from the measured surface heights | 21 | 0 |
| `tests/test_scene.py` | MjSpec scenes: geometry, colours, containers; dict round trip; reproducible, reachable, non-overlapping layouts; eight objects fit; the classic scene; a measured object (own form, size, colour), an extra camera and a visual-only sheet survive JSON and compile | 11 | 0 |
| `tests/test_perception.py` | SAM 2.1 IoU >= 0.6, localisation <= 3 mm, yaw <= 3 deg (2 seeds x 3 cubes); bare-table click; every shape's IoU, localisation, top height, roundness and grasp width Third ultra round: **SAM 2.1 clamps the looked-up pixel to its mask** (a point that rounds to column 640 raised IndexError). | 6 | 0 |
| `tests/test_colour.py` | HSV colour names for the six scene colours lit and shaded; blobs and nearest-blob search; footprint size / roundness -> grasp axes on synthetic footprints | 12 | 0 |
| `tests/test_grounding.py` | grounding a label + hint: accept, fall back to colour, reject the wrong colour, raised-blob fallback, give up with every try, containers; SAM 3.1 token parser and request body; a Muse-style step with a hint 22 px off grounded and placed in the sim; 'the table' is not searched; **for all eight families, a scripted Muse-format plan with every pick hint 20 px off grounds the named object** (truth label, IoU > 0.6); a scripted line-up with 'the table' places runs in MuJoCo and meets its row goal | 20 | 0 |
| `tests/test_verify.py` | `HASAMI_DISTURB` parsing; gripper reading empty vs holding; a forced 6 mm offset fails the first attempt and the retry succeeds; a grip that always slips fails as `grasp` after two retries; perception disturbances: depth noise sigma ~2 mm, camera offset exactly 3 mm, lighting changed, truth untouched, reproducible, a located cube 1.5-6 mm off | 5 | 0 |
| `tests/test_core.py` | reset bounds and reproducibility; full 3-step clicked run (physics), exact event order including `verify`, run record; meta segmenter without key | 7 | 0 |
| `tests/test_tasks.py` | eight task families: goal predicate true on a hand-placed solution, false when a move is undone or a kept object knocked (3 seeds each); reproducible tasks; sort-by-colour and sort-by-shape moves; tower order; line-up slots free and sizes ordered; `in_row` (reversed, 17 mm off line, touching, tipped); pick-from-bin starts in the bin; single-pose predicates; one seed of line-up, pick-from-bin and sort-by-shape run in MuJoCo by the oracle meets its goal | 42 | 0 |
| `tests/test_twin.py` | digital twin, as round trips through MuJoCo renders: sheet corners within 2 px and the photo's camera within 12 mm (2 random desks); no sheet -> readable error; box and cylinder fits on truth masks within 2 mm height / 1.5 mm sides, typed height kept; SAM 2.1 automatic masks on 2 random desks find every object, nothing extra, centre < 5 mm, yaw < 3 deg, height < 10 mm, sides < 8 mm, shape and colour right; a 2 mm card dropped as flat; the twin renders from the photo's viewpoint and clear-desk on it meets its goal; objects under the base / tray / out of reach say why | 10 | 0 |
| `tests/test_planner.py` | plan schema, `PlanError.raw`, `submit_plan` tool schema, image only in the user message, exact Setup required text; live Muse call Third ultra round: **a plan point is validated as the rounded pixel** ([639.6, 479.6], [639.5, 0], [0, 479.5] are refused; [639.4, 479.4] passes). | 16 | 1 (live, needs `MODEL_API_KEY`) |
| `tests/test_robot.py` | sim -> LeRobot conversion; Setup required without / with a missing port; step limit; the sim arm streams joint targets to the hook; RealMirror units against a recording stand-in; a session stays sim-only when the port is missing. Second ultra round: **the very first mirrored command is capped at 6°** (it is measured from the arm's present position; 1.5 rad on shoulder_pan was sent whole, 85.9°), the gripper is capped too, and a follower whose position cannot be read is not mirrored | 15 | 0 |
| `tests/test_realcam.py` | homography and camera pose from four A4 corners on a synthetic tilted camera; a 50 x 30 x 40 mm box located within 1 mm / 1.5 deg with parallax correction (> 10 mm off without); capture Setup required; **the sheet's orientation read from four clicked corners for 6 camera poses x portrait/landscape (fields of view 60-80 deg, 70 assumed): the detected one measures two marks 150 mm apart exactly, the other does not** | 17 | 0 |
| `tests/test_server.py` | status; frames; segment; 503 setup_required; 422s; clicked run over SSE, 409 while busy, recorded; per-step images and composites with IoU matching the record; reset to a task family; CORS; real-camera endpoints, **calibrate `sheet: auto` detects a landscape sheet and an explicit choice is labelled chosen**; **twin: a rendered photo through POST /api/twin -> side-by-side PNG -> adjust a height -> run clear-desk over SSE, `goal` before `done`, recorded with the twin's label; 422 without a sheet or with bad corners, 400 for a non-image, 404 for an unknown twin, GET /api/twin returns the latest twin with its images**; a recorded run's MP4 served (Range too) only when the file is in the runs folder. Ultra round (each failed on the code before its fix): **`sheet: auto` follows the click order** (portrait / landscape x sheet yaw 0/30/50 deg x first click on each corner x both windings: edge 1 -> 2 measures its true length within 2 mm; 24 of 48 were wrong); **local only**: foreign `Host` -> 400, a text/plain, form or foreign-origin upload -> 403 with the frame unchanged, body-less writes need `X-Hasami`; **photo uploads**: 400 MP and 81 MP header-only PNGs -> 400 "photo too large", over 15 MB -> 413; **a real uvicorn keeps answering `/api/status` in under 200 ms during a twin build** (6 ms max; 11 s before); **twins capped** (`HASAMI_MAX_TWINS`, least recently used dropped, folder removed); **malformed input**: off-frame / NaN clicks, seeds outside 0..2^32-1, corners that are not four pairs -> 4xx; SAM 3.1 against a refused connection on 127.0.0.1 -> 502. Second ultra round (each failed on the code before its fix): **CORS and the write guard trust an exact origin list** (8420 and the 5190 dev server; `http://localhost:8888` got `access-control-allow-origin` and its writes passed, now no header and 403), the list follows `HASAMI_PORT` and `HASAMI_ALLOWED_ORIGINS`; **a stale `Last-Event-ID`** (99999, another process's id, garbage) still gets the current run from `plan` (it got nothing); **twin photos**: folders an earlier process left in `HASAMI_TWINS_DIR` are deleted at startup, and without it twins go to a temporary folder removed at shutdown, never to `deployments/twins`; **run records**: a truncated record is a 503 "damaged or still being written" (was 500) and is listed as damaged (was hidden), and a failed rename leaves the old record untouched; **`/api/realcam/segment` during a run** -> 409 at once (it waited 9 s for the run); **a run posted while a reset replaces the scene** -> 409 (it started on the new scene with the old scene's clicks) Third ultra round (each failed on the code before its fix): **a GET naming a foreign Origin -> 403**; **a run whose clicks round off the frame -> 422 up front** (it was a 200 and a failed run); **`/api/realcam/segment` at x = 319.7 on a 320-wide photo -> 400** (500 IndexError); **only a held run slot is a 409** (a failing reset or twin load was reported as 'a run is in progress'; now 500 with its message); **real-camera frames are named**: calibrate and segment need the `frame_id` (409 'replaced in another tab' after another tab's upload), `GET /api/realcam` restores the frame and its calibration, `/api/realcam/clear` forgets it; **`/api/runs` reads only the page asked for** (500 records, `limit=50`: 50 reads, 0 on the second call; it read all 500), with `total` and a repo-relative `dir`; **`HASAMI_MAX_RUNS` prunes** the oldest records, images and videos after a run and keeps the evidence runs (which match .gitignore); **step images outside `runs/<id>/` are a 404** (`/etc/hosts` was served) | 45 | 0 |
| `tests/test_docs.py` | a fresh clone gets pytest: every install line in the README, TESTING and the landing installs `'.[dev]'`; `scripts/setup.sh` exists, is executable, passes `bash -n` and answers `--help`; CI runs pytest files that exist and the dashboard build (the first two failed before the fix) Third ultra round: every dependency pinned in `constraints.txt` to the tested version and installed at it, setup / CI / docs install with it; **the SAM 2.1 IoU quoted in the README and form is computed from run 20261021T053112874Z's record**; **every docs path a source file cites exists** (`specs/hasami.md` did not). | 11 | 0 |
| `tests/test_plausible.py` | the target check before any move: a synthetic **594 mm-wide** leak (wider than the jaws, longer than any object, 6 mm tall, off the frame edge) is rejected; a normal cube passes; a lying cylinder and a leaning cube pass, an out-of-reach centre and a noise speck fail, a target inside a bin counts as workspace; a bare-table mask on a real noisy frame locates hundreds of mm wide and is rejected, a cube's mask passes; **retry path** with SAM 2.1 fault-injected: a leaked first mask is re-prompted with a box and the step places the cube; every prompt leaking falls back to the red blob; nothing plausible near a bare-table click skips the step as `implausible` with no pick-and-place phase and no object moved; **the sweep's own case, no injection:** tower-of-3 seed 0 under the noisy camera no longer sends the arm to the 594 mm target (step 2 is `implausible`); a clean run passes the check first time. 6 of these 10 fail on b1d6bfa's code (the other 4 test the new module directly) | 10 | 0 |
| `tests/test_mcp.py` | `python -m hasami_mcp` over stdio: tools, status, look, find, Setup required by label, call log | 1 | 0 |
| **Total** | | **249** | **1** |

## Dashboard (not part of pytest)

```bash
cd packages/dashboard && pnpm test     # vite bundles src/runState.test.ts, node --test runs it
```

`6 passed` (2026-10-23, Node 26; `src/runState.test.ts` and `devserver.test.mjs`). `devserver.test.mjs`
(third ultra round) checks Vite is 5.4.21, the dev and preview servers bind 127.0.0.1 with CORS off, and a
real dev server gives a foreign origin no `Access-Control-Allow-Origin` on source files or proxied `/api`
(it answered `*` on 5.4.11). The run reducer ignores the previous run's events that
`GET /api/events` replays before `POST /api/run` names the new run, and keeps the new run's own early
events in order; **a run the server no longer has** (its status names another run or none, or the slot is
free) ends from its record or as "The server restarted; this run was lost." instead of staying on running
(failed before the fix: no such check existed). `pnpm build` (tsc + vite) passes.

## CI

`.github/workflows/ci.yml` runs on push and pull request: the pytest files that need no SAM 2.1 weights
(`test_docs`, `test_colour`, `test_scene`, `test_sim`, `test_tasks`, `test_realcam`, `test_robot`,
`test_planner`) on headless MuJoCo (OSMesa) with CPU torch, and the dashboard's type check, build and unit
test. **It has not run yet:** the repository has no GitHub remote until Nick creates it. The full suite and
the browser E2E run locally.

## Browser E2E (Playwright in Google Chrome)

```bash
cd e2e && pnpm install && pnpm test:e2e      # builds the dashboard, then runs Playwright
```

`49 passed, 1 skipped` (2026-10-23, Playwright 1.63.0, installed Google Chrome; 24 tests x two viewports,
desktop 1280x800 and mobile 375x812, plus the server-restart test, which runs on desktop only and is the
skip on mobile; 4.5 minutes; 41 before the third ultra round, 38 before the second). `e2e/serve.sh` starts the real server on 127.0.0.1:8420
(MuJoCo + SAM 2.1; run records go to a throwaway `e2e/.runs` seeded with the committed evidence runs, not
`deployments/runs`, and twins to a throwaway `e2e/.twins`) and the landing on :8421, with no
`MODEL_API_KEY`, `HASAMI_REALCAM` or `HASAMI_SO101_PORT`, so those stay Setup required. Nothing is routed
or stubbed. A test fails on any console error, page error or failed same-origin request it did not declare.

| File | What a user does | Tests per viewport |
|---|---|---:|
| `e2e/tests/dashboard.spec.ts` | status pills and empty states, camera toggle, skip link; Muse Setup required with no plan drawn; clicked steps (add, remove, clear, a bare-table click, keyboard); click a cube, click the tray, Run: run log, results, agent trace (perception / truth, frame after), register, Replay a new and a committed run, Watch a video, reset; the run log follows a run after a reload; task scenes; **the register tabs as a WAI-ARIA tab widget: roving tabindex, ArrowRight / ArrowLeft wrap, Home / End, a tabpanel labelled by its tab** (failed before the fix: no tabindex, no tabpanel) Third ultra round: **a click on the last half-pixel of the overhead frame is sent as pixel 639 and segments** (it was rounded to 640 and failed). | 8 |
| `e2e/tests/photo.spec.ts` | Real camera: webcam Setup required, a MuJoCo render with an A4 sheet uploaded, four corners clicked, an object located. Twin: a non-image refused, a render becomes a twin, refit with a typed height, shown in the arm's camera, a task run with its goal check, the picker after a reload, the synthetic-photo button. The sheet's orientation detected from the clicked corners (a landscape sheet), then chosen portrait and landscape, the object measuring differently. **The same sheet clicked from its top-right corner: portrait (edge 1 -> 2 is 210 mm), and the object's position matches the top-left frame's, rotated, within 2 mm.** **Keyboard only: Tab to the photo, arrows + Enter on the four corners, Space on the object.** A twin built earlier is shown again after a load, a reload and a tab switch Third ultra round: **a click on the photo's last pixel column is sent as that whole pixel and segments**; **two tabs: tab B's upload replaces tab A's photo; A's next click is told so and A shows B's photo and corners; a reload restores the photo and its four corners; Forget photo removes it from the server** (failed before the fix: a reload showed 'No real frame yet'). Each Real camera test starts from no frame (`POST /api/realcam/clear`), since the server keeps one across page loads. | 8 |
| `e2e/tests/restart.spec.ts` | **A server restart mid-run** (second ultra round): the test starts its own server on 127.0.0.1:8432 (`HASAMI_E2E_RESTART_PORT`), starts a 3-step clicked run, kills that server by PID with SIGKILL, starts a new one; the run log ends with "The server restarted; this run was lost.", the live-frame banner goes, and a new step can be run. Before the fix the page stayed on "running" (failed on the old dashboard) Third ultra round: **the spawned server's environment sets `HF_HOME` to the author's cache only when that folder exists**, otherwise the default cache `scripts/setup.sh` fills (it forced `/Volumes/Extreme SSD/...` on every machine). | 2 (restart on desktop only) |
| `e2e/tests/landing.spec.ts` | the landing (`index.html`): headline, viewfinder readout, the three proof numbers, the Setup required keys, its links to the notebook and the hosted dashboard; the lab notebook (`evidence.html`): every image; nav 01-06, hero and footer links, skip link, the quickstart's three commands (venv + `'.[dev]'` install, dashboard build, server); every anchor; the scissors cut once, and stay still under reduced motion; the results sheet matches `deployments/bench/*.json` row for row (32 rows); replay strip; apparatus | 7 |

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
