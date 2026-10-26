# API, MCP and CLI final audit — 2026-10-26

This directory and `../api/` contain only the final worktree execution evidence. See `../final/index.md` for row-by-row links and `../final/rows.json` for audited counts.

Run `bash audit/final-run.sh` and send `api`, `mcp`, `cli`, `setup`, and `twin-adjust`. The runner holds `hasami-audit-final`, unsets the excluded runtime settings, and releases the slot in its EXIT trap. `HASAMI_AUDIT_PART` selects each stage of `execute.py`. Each stage uses temporary run/twin stores; real server processes are terminated and waited for. HTTP checks use Playwright against real uvicorn, with Chromium for the built dashboard and SSE keepalive. MCP uses the official stdio client.

A013 records a real refused loopback connection from Hasami's Meta segmenter: 502 `URLError`, no mask. A016 records the real Planner's refused-connection 502 `APITimeoutError`, then a real 502 PlanError/raw after a tiny local HTTP peer returns a deliberately invalid chat-completions body. That peer is explicitly not Muse. Only the isolated server process receives a local-only placeholder credential; its value and HTTP Authorization headers are never recorded. No Hasami implementation is patched, monkeypatched, stubbed or replaced.

A062 checks real dark-image and degenerate-corner errors and an unchanged latest twin. A069 checks a real twin's successful load/status/frame/latest. A077 observes real reset/load requests rejected with 409 while a clicked run holds the slot. C007 records `git show --stat --oneline bf4dcce` and its exact one-file list, without making claims about later commits.

The single carton fetch and one-family/one-seed benchmark run in the CLI stage. Their command output, exit codes, records and verification totals are evidence here. The historical 20-seed benchmark and 10-seed fetch sweep are excluded by explicit user instruction. Live Muse/SAM 3.1, webcam capture, physical arm and Thenar Walker operations remain UNTESTED for their named dependencies in AUDIT.md.

Images and masks in HTTP JSON are represented by real dimensions, byte counts and hashes. Large JSON and server logs may use gzip (`gzip -dc <file>`). Declared validation/setup failures and deliberate browser-outage errors are logged. Torch/transformers/MuJoCo library warnings may remain in command stderr; no unexpected browser console/page/network errors are accepted. No `.env`, secrets, Meta service, webcam, serial port or physical robot is read or contacted.
