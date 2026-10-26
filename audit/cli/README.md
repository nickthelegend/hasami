# API, MCP and CLI audit — 2026-10-26

96 newly resolved rows: **90 PASS, 0 FAIL, 6 UNTESTED**. API A001–A079: 74 PASS / 5 UNTESTED;
MCP M001–M008: 8 PASS; CLI C001–C007: 6 PASS / 1 UNTESTED; K001/K008: 2 PASS.
The existing live-Meta, webcam, real-arm and walker-motion UNTESTED rows were retained.

`run-audit.sh` unsets the four excluded environment settings, holds `hasami-audit-cli` and releases it
in an EXIT trap. `execute.py` runs real uvicorn on loopback with temporary runs/twins directories,
Playwright's request API and Chrome, and the official MCP stdio client. `HASAMI_AUDIT_PART` selects
`api`, `mcp`, `cli`, `setup`, `tests`, or `twin-adjust`; the default is `all`.
The single fetch and single benchmark were each invoked once. Both exited 0; the benchmark printed
`all-to-tray: 1/1 tasks, 3/3 steps`, with 3/3 verification agreements. Fetch verified the upright carton
in the basket, with 1/1 agreement and IoU 0.9961. Run records are included here; temporary image/video
files and stores were removed at the end. The local walker JPEG/annotated PNG were real files; their
dimensions and hashes are recorded in C002-image.json.

Five API rows require injected implementations/transport failures and remain UNTESTED under the
no-mocks instruction: A013, A016, A062 (IndexError branch only), A069 (ValueError branch only), A077.
Actual dark-image, degenerate-geometry and successful twin-load behavior was exercised. C007's
historical Phase 1 process claim cannot be retrospectively established from runtime tests.

The product fix excludes `/api` paths from the dashboard's root static mount. Previously that mount
turned a known API route's method mismatch into a static 404; GET /api/reset now returns 405 with
Allow: POST. Four focused regression tests passed (42 deselected), and the corrected route was
reverified against real uvicorn. Dashboard TypeScript/Vite build passed.

JSON logs include status, headers and response data; binary images/masks use hashes and dimensions.
SSE logs retain IDs, event order, run IDs and terminal/result/goal data. Large JSON/log files use gzip
to keep the complete audit below 15,000,000 bytes (`gzip -dc <file>` reads them). No secrets or `.env`
were read; no Meta API, robot, serial port or webcam was contacted. No runtime stand-ins or fake masks
were used. The browser console/network was clean. Declared validation/setup/tool failures are recorded;
local Torch/transformers/MuJoCo warnings are preserved in stderr logs. All started children were closed
or terminated and waited for, and the heavy slot was released.
