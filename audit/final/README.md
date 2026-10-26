# Final zero-tolerance run — 2026-10-26

Start `bash audit/final-run.sh`. It holds `hasami-audit-final`, unsets the four excluded runtime settings, builds the dashboard, and releases the slot in an EXIT trap. Send stages `landing`, `dashboard`, `photo`, `api`, `mcp`, `cli`, `setup`, `twin-adjust`, then `finish`. The harnesses start real local servers in temporary stores and close their own Chromium processes. Landing entry-link checks stop the static server before starting the dashboard server, then switch back.

Browser commands use `pnpm --dir audit exec playwright test tests/<stage>.spec.ts --project=chromium --workers=1 --trace=off --reporter=list`. API, MCP, CLI and setup commands use `HASAMI_AUDIT_PART=<stage> .venv/bin/python audit/cli/execute.py`. API HTTP traffic uses Playwright's request API; A001 and SSE keepalive also use Chromium. MCP uses the official stdio client. CLI commands and exit codes are retained in `audit/cli/`.

A013 and A016 run the real Hasami server with its upstream pointed at a bound, non-listening loopback port. A016 additionally uses a tiny explicitly local chat-completions peer whose deliberately invalid reply the real Planner rejects. This peer is not Muse and provides no successful plan. The isolated server receives a local-only placeholder credential through its environment; its value is never recorded. No Hasami segmenter, planner, reset or twin loader is replaced.

A062 uses real image/corner inputs and checks that failed requests do not replace the latest twin. A069 loads a real generated twin and checks status/frame/latest. A077 makes reset and load requests during a real clicked run and checks 409, never 500. C007 checks only the file list/stat of bf4dcce.

Live Meta calls and hardware remain UNTESTED with their exact dependencies. The 20-seed benchmark and 10-seed fetch sweep remain excluded by explicit user instruction. The single fetch and one-family/one-seed benchmark are re-run; their outcomes are not historical sweep claims. No webcam, serial device or Thenar Walker is contacted. `.env` is never read.

`rows.json` is the table-order evidence manifest and count. `dependencies.json` preserves each UNTESTED expectation. `index.md` links every PASS row to newly executed evidence. Large JSON and logs may be gzip-compressed; `gzip -dc <file>` reads them. All screenshot files are JPEG and at most 200,000 bytes. Earlier attempt screenshots are removed from `audit/`.

The first API invocation stopped at its preflight socket bind before executing a row. Its log is retained as `api-preflight.txt`; the harness now enables SO_REUSEADDR before checking the recently closed port, matching uvicorn's reuse behavior. The complete API stage was then re-run. `A063-options.json` retains the additional successful changed-FOV/corners/default-height execution separately from the full API run.

The first loopback execution observed 502 `URLError` for segmentation and 502 `APITimeoutError: Request timed out.` for planning on this machine's bound/non-listening socket. The harness initially assumed the SDK would use APIConnectionError, so that attempt stopped after both A016 transports completed. Its log is retained as `api-transport-attempt.txt`. The expectation was corrected to the real exception, then the full API stage was re-run. Both error types are transport failures, and no mask or plan was returned.
