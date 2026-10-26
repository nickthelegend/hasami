"""Keep the final evidence lean without changing screenshots or observations."""
import gzip
import json
from pathlib import Path

root = Path(__file__).resolve().parent.parent
audit = root / 'audit'
paths = [p for p in audit.rglob('*') if p.is_file() and 'node_modules' not in p.parts and '__pycache__' not in p.parts]
renamed = {}
for path in paths:
    if path.suffix == '.json':
        path.write_text(json.dumps(json.loads(path.read_text()), separators=(',', ':')) + '\n')
        if path.stat().st_size > 50_000:
            target = path.with_name(path.name + '.gz')
            target.write_bytes(gzip.compress(path.read_bytes(), compresslevel=9, mtime=0))
            renamed[path.name] = target.name
            path.unlink()
    elif path.name == 'server.log':
        target = path.with_name(path.name + '.gz')
        target.write_bytes(gzip.compress(path.read_bytes(), compresslevel=9, mtime=0))
        renamed[path.name] = target.name
        path.unlink()
for path in [root/'AUDIT.md', root/'TESTING.md', *paths]:
    if path.is_file() and path.suffix in {'.md', '.json'}:
        text = path.read_text()
        for before, after in renamed.items():
            text = text.replace(before, after)
        path.write_text(text)
# Compressed scenario logs can also name another compressed evidence file.
for path in audit.rglob('*.json.gz'):
    text = gzip.decompress(path.read_bytes()).decode()
    for before, after in renamed.items():
        text = text.replace(before, after)
    path.write_bytes(gzip.compress(text.encode(), compresslevel=9, mtime=0))
total = sum(p.stat().st_size for p in audit.rglob('*') if p.is_file() and 'node_modules' not in p.parts and '__pycache__' not in p.parts)
assert total <= 15_000_000, total
for path in audit.rglob('*.jpg'):
    assert path.stat().st_size <= 200_000, path
print(json.dumps({'bytes':total, 'compressed_files':len(renamed)}))
