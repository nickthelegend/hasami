# Camera and twin browser evidence

37 audit rows PASS. All 16 scenarios ran against the real local server and Playwright Chromium, at 1280×800 and 375×812. Two additional fresh-load replacement checks verify the second context has no horizontal scroll at either width. No unexpected console/page/network errors, mocks, fulfilled routes, Meta calls, or camera access.

Live webcam capture (K012/A082), real-desk measurements (K013), and a twin from a real desk photograph (K014) remain UNTESTED. R002 independently verifies the missing-camera setup state. Those non-R/T rows are unchanged.

Image provenance: every upload starts with GET /api/twin/sample.png, a MuJoCo rendered photo. Seed 3 calibrates the camera, seed 5 replaces it in a second browser context, seed 0 builds the twin, and the synthetic button advances 0→1. The tiny-mask input is a 3×3 crop; no-sheet input is a table-only crop; empty-sheet input is the full-resolution `?seed=0&empty=true` MuJoCo render of the desk and sheet without objects. None is a live webcam or real desk photograph.

Declared failures are logged with exact path/status/body: missing webcam 503, invalid image/degenerate corners 400, stale frame/busy 409, no sheet 422, and forgotten photo/expired twin 404. Server-outage checks stop the owned server by PID; only their connection errors are declared. Navigations intentionally abort old frame/event-stream requests.

Reproduce: `bash audit/run-photo.sh`, then send `run` and `finish`. The runner acquires `hasami-audit-photo`, unsets the four forbidden runtime settings, builds the dashboard, and releases the slot in a trap. Tests create throwaway runs/twins directories, start/stop their own server on 127.0.0.1:8420, and close Chromium.

<a id="r001"></a>
## R001 — PASS

- 1280px: [row](R001-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R001-1280-camera-main-1280px.jpg).
- 375px: [row](R001-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R001-375-camera-main-375px.jpg).

<a id="r002"></a>
## R002 — PASS

- 1280px: [row](R002-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R002-1280-camera-main-1280px.jpg).
- 375px: [row](R002-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R002-375-camera-main-375px.jpg).

<a id="r003"></a>
## R003 — PASS

- 1280px: [row](R003-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R003-1280-camera-main-1280px.jpg).
- 375px: [row](R003-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R003-375-camera-main-375px.jpg).

Show uploaded-photo provenance in the caption and accessible image description.

<a id="r004"></a>
## R004 — PASS

- 1280px: [row](R004-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R004-1280-camera-main-1280px.jpg).
- 375px: [row](R004-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R004-375-camera-main-375px.jpg).

<a id="r005"></a>
## R005 — PASS

- 1280px: [row](R005-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R005-1280-camera-main-1280px.jpg).
- 375px: [row](R005-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R005-375-camera-main-375px.jpg).

<a id="r006"></a>
## R006 — PASS

- 1280px: [row](R006-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R006-1280-camera-main-1280px.jpg).
- 375px: [row](R006-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R006-375-camera-main-375px.jpg).

<a id="r007"></a>
## R007 — PASS

- 1280px: [row](R007-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R007-1280-camera-main-1280px.jpg).
- 375px: [row](R007-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R007-375-camera-main-375px.jpg).

<a id="r008"></a>
## R008 — PASS

- 1280px: [row](R008-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R008-1280-camera-main-1280px.jpg).
- 375px: [row](R008-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R008-375-camera-main-375px.jpg).

<a id="r009"></a>
## R009 — PASS

- 1280px: [row](R009-1280-camera-keyboard-1280px.json), [console/network and assertions](1280-camera-keyboard-1280px.json), [JPEG 1](R009-1280-camera-keyboard-1280px.jpg).
- 375px: [row](R009-375-camera-keyboard-375px.json), [console/network and assertions](375-camera-keyboard-375px.json), [JPEG 1](R009-375-camera-keyboard-375px.jpg).

<a id="r010"></a>
## R010 — PASS

- 1280px: [row](R010-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R010-1280-camera-main-1280px.jpg).
- 375px: [row](R010-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R010-375-camera-main-375px.jpg).

<a id="r011"></a>
## R011 — PASS

- 1280px: [row](R011-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R011-1280-camera-main-1280px.jpg).
- 375px: [row](R011-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R011-375-camera-main-375px.jpg).

Re-click sheet now clears the object-click marker and keyboard crosshair.

<a id="r012"></a>
## R012 — PASS

- 1280px: [row](R012-1280-camera-replacement-1280px.json), [console/network and assertions](1280-camera-replacement-1280px.json), [JPEG 1](R012-1280-camera-replacement-1280px.jpg).
- 375px: [row](R012-375-camera-replacement-375px.json), [console/network and assertions](375-camera-replacement-375px.json), [JPEG 1](R012-375-camera-replacement-375px.jpg).

<a id="r013"></a>
## R013 — PASS

- 1280px: [row](R013-1280-camera-replacement-1280px.json), [console/network and assertions](1280-camera-replacement-1280px.json), [JPEG 1](R013-1280-camera-replacement-1280px.jpg).
- 375px: [row](R013-375-camera-replacement-375px.json), [console/network and assertions](375-camera-replacement-375px.json), [JPEG 1](R013-375-camera-replacement-375px.jpg).

<a id="r014"></a>
## R014 — PASS

- 1280px: [row](R014-1280-camera-replacement-1280px.json), [console/network and assertions](1280-camera-replacement-1280px.json), [JPEG 1](R014-1280-camera-replacement-1280px.jpg).
- 375px: [row](R014-375-camera-replacement-375px.json), [console/network and assertions](375-camera-replacement-375px.json), [JPEG 1](R014-375-camera-replacement-375px.jpg).

<a id="r015"></a>
## R015 — PASS

- 1280px: [row](R015-1280-camera-main-1280px.json), [console/network and assertions](1280-camera-main-1280px.json), [JPEG 1](R015-1280-camera-main-1280px.jpg).
- 375px: [row](R015-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R015-375-camera-main-375px.jpg).

<a id="r016"></a>
## R016 — PASS

- 1280px: [row](R016-1280-camera-failures-1280px.json), [console/network and assertions](1280-camera-failures-1280px.json), [JPEG 1](R016-1280-camera-failures-1280px-degenerate.jpg), [JPEG 2](R016-1280-camera-failures-1280px-outage.jpg).
- 375px: [row](R016-375-camera-failures-375px.json), [console/network and assertions](375-camera-failures-375px.json), [JPEG 1](R016-375-camera-failures-375px-degenerate.jpg), [JPEG 2](R016-375-camera-failures-375px-outage.jpg).

<a id="r017"></a>
## R017 — PASS

- 1280px: [row](R017-1280-camera-small-mask-1280px.json), [console/network and assertions](1280-camera-small-mask-1280px.json), [JPEG 1](R017-1280-camera-small-mask-1280px.jpg).
- 375px: [row](R017-375-camera-small-mask-375px.json), [console/network and assertions](375-camera-small-mask-375px.json), [JPEG 1](R017-375-camera-small-mask-375px.jpg).

<a id="r018"></a>
## R018 — PASS

- 375px: [row](R018-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R018-375-camera-main-375px.jpg).

Uploaded-photo provenance is explicit at 375px; sheet reset clears all markers.

<a id="r019"></a>
## R019 — PASS

- 375px: [row](R019-375-camera-keyboard-375px.json), [console/network and assertions](375-camera-keyboard-375px.json), [JPEG 1](R019-375-camera-keyboard-375px.jpg).
- 375px: [row](R019-375-camera-main-375px.json), [console/network and assertions](375-camera-main-375px.json), [JPEG 1](R019-375-camera-main-375px.jpg).

Contain edge markers within the photo so last-pixel/keyboard clicks cannot widen the page.

<a id="r020"></a>
## R020 — PASS

- 375px: [row](R020-375-camera-failures-375px.json), [console/network and assertions](375-camera-failures-375px.json), [JPEG 1](R020-375-camera-failures-375px.jpg).
- 375px: [row](R020-375-camera-replacement-375px.json), [console/network and assertions](375-camera-replacement-375px.json), [JPEG 1](R020-375-camera-replacement-375px.jpg).
- 375px: [row](R020-375-camera-small-mask-375px.json), [console/network and assertions](375-camera-small-mask-375px.json), [JPEG 1](R020-375-camera-small-mask-375px.jpg).

<a id="t001"></a>
## T001 — PASS

- 1280px: [row](T001-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T001-1280-twin-main-1280px.jpg).
- 375px: [row](T001-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T001-375-twin-main-375px.jpg).

<a id="t002"></a>
## T002 — PASS

- 1280px: [row](T002-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T002-1280-twin-main-1280px.jpg).
- 375px: [row](T002-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T002-375-twin-main-375px.jpg).

<a id="t003"></a>
## T003 — PASS

- 1280px: [row](T003-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T003-1280-twin-main-1280px.jpg).
- 375px: [row](T003-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T003-375-twin-main-375px.jpg).

<a id="t004"></a>
## T004 — PASS

- 1280px: [row](T004-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T004-1280-twin-main-1280px.jpg).
- 375px: [row](T004-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T004-375-twin-main-375px.jpg).

<a id="t005"></a>
## T005 — PASS

- 1280px: [row](T005-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T005-1280-twin-main-1280px.jpg).
- 375px: [row](T005-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T005-375-twin-main-375px.jpg).

<a id="t006"></a>
## T006 — PASS

- 1280px: [row](T006-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T006-1280-twin-main-1280px.jpg).
- 375px: [row](T006-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T006-375-twin-main-375px.jpg).

<a id="t007"></a>
## T007 — PASS

- 1280px: [row](T007-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T007-1280-twin-main-1280px.jpg).
- 375px: [row](T007-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T007-375-twin-main-375px.jpg).

<a id="t008"></a>
## T008 — PASS

- 1280px: [row](T008-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T008-1280-twin-main-1280px-far.jpg), [JPEG 2](T008-1280-twin-main-1280px-near.jpg), [JPEG 3](T008-1280-twin-main-1280px-left.jpg), [JPEG 4](T008-1280-twin-main-1280px-right.jpg).
- 375px: [row](T008-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T008-375-twin-main-375px-far.jpg), [JPEG 2](T008-375-twin-main-375px-near.jpg), [JPEG 3](T008-375-twin-main-375px-left.jpg), [JPEG 4](T008-375-twin-main-375px-right.jpg).

<a id="t009"></a>
## T009 — PASS

- 1280px: [row](T009-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T009-1280-twin-main-1280px.jpg).
- 375px: [row](T009-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T009-375-twin-main-375px.jpg).

<a id="t010"></a>
## T010 — PASS

- 1280px: [row](T010-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T010-1280-twin-main-1280px.jpg).
- 375px: [row](T010-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T010-375-twin-main-375px.jpg).

Write the completed run record before emitting done; eliminates the goal-record 404 race.

<a id="t011"></a>
## T011 — PASS

- 1280px: [row](T011-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T011-1280-twin-main-1280px.jpg).
- 375px: [row](T011-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T011-375-twin-main-375px.jpg).

Restore the simulated arm side and FOV controls from the server twin.

<a id="t012"></a>
## T012 — PASS

- 1280px: [row](T012-1280-twin-empty-input-1280px.json), [console/network and assertions](1280-twin-empty-input-1280px.json), [JPEG 1](T012-1280-twin-empty-input-1280px-no-sheet.jpg), [JPEG 2](T012-1280-twin-empty-input-1280px-no-objects.jpg).
- 375px: [row](T012-375-twin-empty-input-375px.json), [console/network and assertions](375-twin-empty-input-375px.json), [JPEG 1](T012-375-twin-empty-input-375px-no-sheet.jpg), [JPEG 2](T012-375-twin-empty-input-375px-no-objects.jpg).

Added an empty=true MuJoCo sample variant for a genuine full-resolution empty-sheet check.

<a id="t013"></a>
## T013 — PASS

- 1280px: [row](T013-1280-twin-main-1280px.json), [console/network and assertions](1280-twin-main-1280px.json), [JPEG 1](T013-1280-twin-main-1280px-busy.jpg), [JPEG 2](T013-1280-twin-main-1280px-failed-build.jpg).
- 1280px: [row](T013-1280-twin-outage-and-lifetime-1280px.json), [console/network and assertions](1280-twin-outage-and-lifetime-1280px.json), [JPEG 1](T013-1280-twin-outage-and-lifetime-1280px-outage.jpg).
- 375px: [row](T013-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T013-375-twin-main-375px-busy.jpg), [JPEG 2](T013-375-twin-main-375px-failed-build.jpg).
- 375px: [row](T013-375-twin-outage-and-lifetime-375px.json), [console/network and assertions](375-twin-outage-and-lifetime-375px.json), [JPEG 1](T013-375-twin-outage-and-lifetime-375px-outage.jpg).

<a id="t014"></a>
## T014 — PASS

- 1280px: [row](T014-1280-twin-outage-and-lifetime-1280px.json), [console/network and assertions](1280-twin-outage-and-lifetime-1280px.json), [JPEG 1](T014-1280-twin-outage-and-lifetime-1280px.jpg).
- 375px: [row](T014-375-twin-outage-and-lifetime-375px.json), [console/network and assertions](375-twin-outage-and-lifetime-375px.json), [JPEG 1](T014-375-twin-outage-and-lifetime-375px.jpg).

<a id="t015"></a>
## T015 — PASS

- 375px: [row](T015-375-twin-empty-input-375px.json), [console/network and assertions](375-twin-empty-input-375px.json), [JPEG 1](T015-375-twin-empty-input-375px.jpg).
- 375px: [row](T015-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T015-375-twin-main-375px.jpg).

<a id="t016"></a>
## T016 — PASS

- 375px: [row](T016-375-twin-empty-input-375px.json), [console/network and assertions](375-twin-empty-input-375px.json), [JPEG 1](T016-375-twin-empty-input-375px.jpg).
- 375px: [row](T016-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T016-375-twin-main-375px.jpg).

<a id="t017"></a>
## T017 — PASS

- 375px: [row](T017-375-twin-main-375px.json), [console/network and assertions](375-twin-main-375px.json), [JPEG 1](T017-375-twin-main-375px.jpg).
- 375px: [row](T017-375-twin-outage-and-lifetime-375px.json), [console/network and assertions](375-twin-outage-and-lifetime-375px.json), [JPEG 1](T017-375-twin-outage-and-lifetime-375px.jpg).

Run records are visible before done; restored arm side and FOV match the twin.
