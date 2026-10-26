# Final landing and notebook audit

51 rows passed in 102 independent Chromium checks: every L001–L021 and N001–N030 row at 1280×800 and 375×812. No row is inferred from another.

Each row has its own JSON console/network log and JPEG. The JSON records the expected cell, actual navigation, browser version, errors, HTTP responses, screenshots and additional comparisons where relevant. Long mobile flows have further numbered JPEGs listed in their JSON. All screenshots are at most 200 KB. `summary.json` gives counts and source record hashes; `playwright.txt` is the final complete run. Earlier attempts were discarded.

The two real local servers were the static landing on 8421 and the built dashboard served by `python -m hasami_server` on 8420. Both dashboard entry links were clicked and opened Viewfinder / overhead. The hosted target was not contacted.

No mocks, routes, stubs or fabricated live data were used. There were no console errors, page errors, failed requests or unexpected HTTP errors in the tested surface. Only localhost and the pages’ Google Fonts resources were requested. No Meta API, webcam, serial device, benchmark sweep or twin generation was called. Missing-key and device setup labels remained visible.

The provenance helper re-encodes the committed hero and six cropped replay PNGs and compares SHA-256 bytes against their WebP assets. It also checks the hero’s exact recorded mask paths and compares each fridge montage panel against its cited stored run frame (including the original mask tint). Benchmark, retry, arm log and twin numbers are read-only comparisons to committed JSON; none is re-measured.

Reproduce after installing the audit dependencies with `pnpm --dir audit install`: start the real local servers under the heavy slot, with MODEL_API_KEY, HASAMI_REALCAM, HASAMI_SO101_PORT and HASAMI_DISTURB unset, then run `pnpm --dir audit exec playwright test tests/landing.spec.ts --project=chromium --workers=1`. The offline image checks use `.venv/bin/python`.
