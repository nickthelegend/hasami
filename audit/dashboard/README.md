# Dashboard audit evidence

The real FastAPI server binds to `http://127.0.0.1:8420/`. Playwright 1.63.0 uses
headless Chrome for Testing. Desktop is 1280×800; mobile is 375×812. The harness
is [dashboard.spec.ts](../tests/dashboard.spec.ts).

Each row JSON records its assertions, all browser console messages, HTTP
responses, failed requests, explicitly declared failures, and JPEG screenshots.
Rows covering several mobile scenarios have one JSON per scenario. `before/`
contains diagnostic evidence from the initial failing checks; current row
results are the JSON files directly in this directory. `summary.json` and
`index.md` collect the final results.

No key file was read and no Meta endpoint was called. The server environment
unsets `MODEL_API_KEY`, `HASAMI_REALCAM`, `HASAMI_SO101_PORT`, and `HASAMI_DISTURB`.
Both missing-key API replies are real HTTP 503 responses with
`setup_required: MODEL_API_KEY`. Live model calls and real arm motion remain
untested. K003 and K006 cover the dashboard and local HTTP setup contract here;
landing/notebook checks belong to their separate audit.

The server writes only to throwaway run and twin directories. The run directory
is seeded with committed JSON and step-image folders. The video-present check
uses the existing `20261021T053112874Z-demo.mp4` in the main checkout; its copy is
removed after the check. No video is generated and no production run is written.

Failure exercises rewrite requests and forward them to the real server:

- register: `limit=0` produces a real 422;
- segment: `[640,480]` produces a real 400;
- reset: an unknown family produces a real 422;
- trace: an unknown run produces eleven bounded real 404 responses;
- frame: an invalid camera produces a real 422;
- SAM 3.1: a valid text request produces missing-key 503;
- offline/restart: the actual server PID is killed and a new process starts on
  the same port. Only the resulting disconnected/refused requests are expected.

No HTTP response is mocked. D025 is the explicitly requested offline component
exercise: the production InstructionCard is bundled by
`node audit/build-components.mjs`, then rendered with error and empty-plan props
in a separate audit-only page. It never calls a model or supplies dashboard data.
Intentional cancellation of SSE/frame requests and the native video page's
cancelled media read are recorded as declared `ERR_ABORTED` events. Any other
console error, page error, or undeclared HTTP error fails the corresponding row.

Reproduce while holding `hasami-audit-dash` with the shared heavy-slot script:

```sh
(cd packages/dashboard && pnpm install && pnpm build && pnpm test)
node audit/build-components.mjs
(cd audit && pnpm install && pnpm exec playwright install chromium)
(cd audit && pnpm exec playwright test tests/dashboard.spec.ts --workers=1 --output=/tmp/hasami-dashboard-playwright)
```

The spec owns and kills the server by PID. The foreground audit shell also traps
cleanup and releases the slot after the server and Chrome have exited.
