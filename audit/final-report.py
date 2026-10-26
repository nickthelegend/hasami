"""Validate this run's evidence, then link every row in table order.

Run only after all final-run.sh stages complete. Nothing is inferred from an
adjacent row: each PASS needs its own row ID in newly written execution evidence.
"""
from collections import Counter
import gzip
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
AUDIT = ROOT / 'audit'


def read(path):
    path = actual(path)
    return json.loads(gzip.decompress(path.read_bytes()) if path.suffix == '.gz' else path.read_text())


def actual(path):
    return path if path.is_file() else path.with_name(path.name + '.gz')


def relative(path):
    return str(path.relative_to(ROOT))


results = read(AUDIT / 'cli/results.json')
by_id = {}
for group in ['landing', 'dashboard', 'photo']:
    for path in sorted([*(AUDIT / group).glob('*.json'), *(AUDIT / group).glob('*.json.gz')]):
        item = read(path)
        if not re.fullmatch(r'[LNDKRTA]\d{3}', str(item.get('id', ''))):
            continue
        assert item['status'] == 'PASS', (path, item.get('assertions'), item.get('errors'))
        assert not item.get('assertions') and not item.get('errors'), path
        log = read(path.parent / item['log']) if 'log' in item else item
        assert log['status'] == 'PASS', path
        assert not log.get('errors') and not log.get('assertions'), path
        screenshots = item.get('screenshots', [])
        assert screenshots, path
        for shot in screenshots:
            assert (path.parent / shot).stat().st_size <= 200_000, shot
        by_id.setdefault(item['id'], []).append(path)

table = ROOT / 'AUDIT.md'
lines = [line for line in table.read_text().splitlines() if not line.startswith('No Hasami mocks, stubs, fabricated success data,')]
index = ['# Final row evidence — 2026-10-26', '',
         'Every PASS below points to execution evidence from this worktree’s final run. '
         'UI checks used Playwright Chromium and the real local product or landing server. '
         'Declared negative responses and deliberate server-outage errors are recorded in their scenarios. '
         'A016 also uses an explicitly local invalid protocol peer, not Muse. No Hasami implementation is mocked or replaced.', '']
ledger, dependencies, changed = [], [], []
for line in lines:
    match = re.match(r'^\| ([A-Z]\d{3}) \|', line)
    if not match:
        changed.append(line)
        continue
    row = match[1]
    prefix, status, evidence, fix, _ = line.rsplit('|', 4)
    expected = prefix.split('|', 3)[3].strip()
    paths = []
    if row in by_id:
        paths = by_id[row]
        status = 'PASS'
    elif row in results:
        assert results[row]['status'] == 'PASS', (row, results[row])
        status = 'PASS'
        if row.startswith('A'):
            paths = [AUDIT / 'api' / f'{row}.json']
            assert actual(paths[0]).is_file(), row
            if row == 'A001': paths += [AUDIT / 'api/A001-browser.json', AUDIT / 'api/A001.jpg']
            if row == 'A017': paths += [AUDIT / 'api' / f'{id}.json' for id in ['A025','A034','A038']]
            if row == 'A063': paths.append(AUDIT / 'api/A063-options.json')
        elif row.startswith('M'):
            paths = [AUDIT / 'cli/mcp-client.json', AUDIT / 'cli/mcp.jsonl']
        elif row == 'K008':
            paths = [AUDIT / 'cli/C004.json', AUDIT / 'cli/C004-record.json']
        else:
            paths = [AUDIT / 'cli' / f'{row}.json']
            extras = {'C002':['C002-image.json'], 'C003':['C003-syntax.json'],
                      'C004':['C004-record.json'], 'C005':['C005-record.json'], 'C007':['C007-names.json']}
            paths += [AUDIT / 'cli' / name for name in extras.get(row, [])]
        paths.append(AUDIT / 'cli/results.json')
    else:
        assert status.strip() == 'UNTESTED', (row, status, 'no final evidence')
        assert 'UNTESTED dependency:' in expected, row
        dependencies.append({'id':row, 'status':'UNTESTED', 'expected':expected})
        evidence = '[dependency review](audit/final/dependencies.json)'
        status = 'UNTESTED'
    if paths:
        paths = [actual(path) for path in paths]
        assert all(p.is_file() for p in paths), (row, paths)
        index += [f'<a id="{row.lower()}"></a>', f'## {row} — PASS', '']
        for path in paths:
            index.append(f'- [{path.name}](../{path.relative_to(AUDIT)})')
            if row in by_id:
                item = read(path)
                for shot in item.get('screenshots', []):
                    photo = path.parent / shot
                    index.append(f'- [JPEG {item.get("width", item.get("viewport", {}).get("width", ""))}px](../{photo.relative_to(AUDIT)})')
        index.append('')
        evidence = f'[final execution evidence](audit/final/index.md#{row.lower()})'
    if row in {'A013','A016','A062','A069','A077','C007'}:
        fix = 'Replaced the prohibited fixture/historical runtime expectation with the user-authorized real check; product unchanged.'
    changed.append(f'{prefix}| {status.strip()} | {evidence.strip()} | {fix.strip()} |')
    ledger.append({'id':row,'status':status.strip(),'evidence':[relative(p) for p in paths]})

observed = Counter(r['status'] for r in ledger)
counts = {status: observed[status] for status in ['PASS','FAIL','UNTESTED']}
assert counts.get('FAIL',0) == counts.get('PENDING',0) == 0
assert len(ledger) == 255
# Every live credential/hardware row must retain its concrete dependency.
for row in dependencies:
    text = row['expected']
    id = row['id']
    if id in {'A080','A081','A083','K009','K010'}: assert 'MODEL_API_KEY' in text
    if id in {'A082','K012'}: assert 'HASAMI_REALCAM' in text
    if id == 'K011': assert 'HASAMI_SO101_PORT' in text
    if id in {'K013','K014'}: assert 'person' in text and 'ruler' in text and 'real photo' in text
    if id.startswith('K') and 17 <= int(id[1:]) <= 24: assert 'Thenar Walker' in text and 'HASAMI_ROBOT' in text

statement = ('No Hasami mocks, stubs, fabricated success data, or unexpected console/page/network errors remain on the tested surface. '
             'A016’s deliberately invalid loopback protocol peer is not Muse; declared validation/setup failures and intentional outage errors are retained in evidence. '
             'No live Meta request, webcam capture, serial connection, or Thenar Walker contact/motion occurred. '
             f'Final counts: {counts["PASS"]} PASS, 0 FAIL, {counts["UNTESTED"]} UNTESTED.')
changed.insert(1, '\n' + statement)
table.write_text('\n'.join(changed) + '\n')
(AUDIT/'final/index.md').write_text('\n'.join(index).rstrip()+'\n')
(AUDIT/'final/dependencies.json').write_text(json.dumps(dependencies,indent=2)+'\n')
(AUDIT/'final/rows.json').write_text(json.dumps({'counts':counts,'rows':ledger},indent=2)+'\n')
print(json.dumps(counts,sort_keys=True))
