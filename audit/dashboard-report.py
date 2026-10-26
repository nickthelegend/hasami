"""Collect current dashboard evidence and update only the assigned AUDIT rows."""
from pathlib import Path
import json
import re

root = Path(__file__).resolve().parent.parent
out = root / 'audit' / 'dashboard'
assigned = {f'D{i:03d}' for i in range(1, 46)} | {'K002', 'K003', 'K006'}
rows = {key: [] for key in sorted(assigned)}
for path in sorted(out.glob('*.json')):
    item = json.loads(path.read_text())
    if item.get('id') in rows:
        rows[item['id']].append((path, item))
for key, evidence in rows.items():
    assert evidence, f'{key}: missing evidence'
    for path, item in evidence:
        assert item['status'] == 'PASS', f'{path}: {item["status"]}'
        assert not item['errors'] and not item['assertions'], f'{path}: unexpected errors'
        assert all('api.meta.ai' not in request['url'] for request in item['network'])
        for shot in item['screenshots']:
            assert (out / shot).stat().st_size <= 200_000

fixes = {
    'D002': 'Keep the camera skip target below the sticky header with scroll margin.',
    'D023': 'Discard the pending pick when segmentation is rejected, so a later click cannot complete it.',
    'D041': 'Discard rejected pending picks; name the planner setup key once.',
    'D045': 'Clear the pending pick on segmentation errors and setup replies.',
    'K002': 'Name MODEL_API_KEY once in the setup notice while retaining the key-creation link.',
    'K003': 'Discard the pending pick after the setup response.',
}
index = ['# Dashboard row evidence', '',
         'Current checks all passed. Every JSON includes the complete console and network log for its scenario.', '',
         'Setup checks make no Meta call. Live model calls and real arm motion remain untested.', '']
summary = {}
for key, evidence in rows.items():
    index += [f'## {key}', '']
    summary[key] = {'status': 'PASS', 'evidence': [], 'fix': fixes.get(key, '')}
    for path, item in evidence:
        index += [f'- [{item["test"]} — console/network]({path.name})']
        summary[key]['evidence'].append(path.name)
        for shot in item['screenshots']:
            index += [f'- [Screenshot]({shot})']
    index.append('')
(out / 'index.md').write_text('\n'.join(index))
(out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')

audit = root / 'AUDIT.md'
original = audit.read_text()
updated = []
changed = set()
for line in original.splitlines():
    match = re.match(r'^\| ([DK]\d{3}) \|', line)
    if match and match[1] in assigned:
        key = match[1]
        # Backticks in expected cells can contain a literal pipe; split from
        # the right, where status/evidence/fix have no embedded pipes.
        prefix, _status, _evidence, _fix, _tail = line.rsplit('|', 4)
        line = f'{prefix}| PASS | [console/network and screenshots](audit/dashboard/index.md#{key.lower()}) | {fixes.get(key, "")} |'
        changed.add(key)
    updated.append(line)
assert changed == assigned
audit.write_text('\n'.join(updated) + '\n')
print(f'{len(assigned)} assigned rows PASS; all other rows preserved.')
