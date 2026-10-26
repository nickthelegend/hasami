"""Summarise the final real-browser photo run; change only R/T audit rows."""
from pathlib import Path
import json
import re

root = Path(__file__).resolve().parents[1]
out = root / 'audit' / 'photo'
rows = {}
for file in sorted(out.glob('[RT][0-9][0-9][0-9]-*.json')):
    item = json.loads(file.read_text())
    log = json.loads((out / item['log']).read_text())
    assert item['status'] == log['status'] == 'PASS', file
    assert not log['errors'] and not log['assertions'], file
    assert item['screenshots'], file
    rows.setdefault(item['id'], []).append((file.name, item))
expected = {f'R{i:03d}' for i in range(1, 21)} | {f'T{i:03d}' for i in range(1, 18)}
assert rows.keys() == expected, expected - rows.keys()
fixes = {
    'R003': 'Show uploaded-photo provenance in the caption and accessible image description.',
    'R011': 'Re-click sheet now clears the object-click marker and keyboard crosshair.',
    'R018': 'Uploaded-photo provenance is explicit at 375px; sheet reset clears all markers.',
    'R019': 'Contain edge markers within the photo so last-pixel/keyboard clicks cannot widen the page.',
    'T010': 'Write the completed run record before emitting done; eliminates the goal-record 404 race.',
    'T011': 'Restore the simulated arm side and FOV controls from the server twin.',
    'T012': 'Added an empty=true MuJoCo sample variant for a genuine full-resolution empty-sheet check.',
    'T017': 'Run records are visible before done; restored arm side and FOV match the twin.',
}
index = ['# Camera and twin browser evidence', '',
         '37 audit rows PASS. All 16 scenarios ran against the real local server and Playwright Chromium, at 1280×800 and 375×812. Two additional fresh-load replacement checks verify the second context has no horizontal scroll at either width. No unexpected console/page/network errors, mocks, fulfilled routes, Meta calls, or camera access.', '',
         'Live webcam capture (K012/A082), real-desk measurements (K013), and a twin from a real desk photograph (K014) remain UNTESTED. R002 independently verifies the missing-camera setup state. Those non-R/T rows are unchanged.', '',
         'Image provenance: every upload starts with GET /api/twin/sample.png, a MuJoCo rendered photo. Seed 3 calibrates the camera, seed 5 replaces it in a second browser context, seed 0 builds the twin, and the synthetic button advances 0→1. The tiny-mask input is a 3×3 crop; no-sheet input is a table-only crop; empty-sheet input is the full-resolution `?seed=0&empty=true` MuJoCo render of the desk and sheet without objects. None is a live webcam or real desk photograph.', '',
         'Declared failures are logged with exact path/status/body: missing webcam 503, invalid image/degenerate corners 400, stale frame/busy 409, no sheet 422, and forgotten photo/expired twin 404. Server-outage checks stop the owned server by PID; only their connection errors are declared. Navigations intentionally abort old frame/event-stream requests.', '',
         'Reproduce: `bash audit/run-photo.sh`, then send `run` and `finish`. The runner acquires `hasami-audit-photo`, unsets the four forbidden runtime settings, builds the dashboard, and releases the slot in a trap. Tests create throwaway runs/twins directories, start/stop their own server on 127.0.0.1:8420, and close Chromium.', '']
for id, records in sorted(rows.items()):
    index += [f'<a id="{id.lower()}"></a>', f'## {id} — PASS', '']
    for name, item in records:
        photos = ', '.join(f'[JPEG {i+1}]({s})' for i, s in enumerate(item['screenshots']))
        index.append(f"- {item['width']}px: [row]({name}), [console/network and assertions]({item['log']}), {photos}.")
    if id in fixes:
        index += ['', fixes[id]]
    index += ['']
(out / 'index.md').write_text('\n'.join(index))
summary = {'rows': {'PASS': len(rows), 'FAIL': 0}, 'scenarios': 16, 'additionalFreshLoadChecks': 2, 'widths': [1280, 375], 'unexpectedErrors': 0,
           'untestedUnchanged': ['A082', 'K012', 'K013', 'K014'], 'fixes': fixes}
(out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
file = root / 'AUDIT.md'
old = file.read_text()
lines = []
for line in old.splitlines():
    match = re.match(r'^\| ([RT]\d{3}) \|', line)
    if match:
        id = match[1]
        # The expected text can contain pipes inside code; replace only the final status/evidence/fix cells.
        line = re.sub(r'\| (?:PENDING|PASS|FAIL|UNTESTED) \|[^|]*\|[^|]*\|$',
                      f'| PASS | [browser evidence](audit/photo/index.md#{id.lower()}) | {fixes.get(id, "")} |', line)
    lines.append(line)
new = '\n'.join(lines) + ('\n' if old.endswith('\n') else '')
old_other = [line for line in old.splitlines() if not re.match(r'^\| [RT]\d{3} \|', line)]
new_other = [line for line in new.splitlines() if not re.match(r'^\| [RT]\d{3} \|', line)]
assert old_other == new_other
file.write_text(new)
print(json.dumps(summary, indent=2))
