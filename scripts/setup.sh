#!/usr/bin/env bash
# One command for a fresh clone: a Python 3.12 venv with Hasami and its test tools ('.[dev]': pytest, httpx2),
# the dashboard build, and the SAM 2.1 hiera-tiny weights (~150 MB, into HF_HOME or ~/.cache/huggingface).
#
#   scripts/setup.sh                 everything
#   scripts/setup.sh --no-dashboard  skip the pnpm build (the API still runs; / then says how to build it)
#   scripts/setup.sh --no-weights    skip the SAM 2.1 download (the first segment downloads it instead)
#
# Then: source .venv/bin/activate && python -m hasami_server   (http://127.0.0.1:8420)
#       python -m pytest -q                                     (see TESTING.md)
# No key is read or needed here. MODEL_API_KEY (Muse, SAM 3.1) stays "Setup required" until you export it.
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root"

dashboard=1
weights=1
for arg in "$@"; do
  case "$arg" in
    --no-dashboard) dashboard=0 ;;
    --no-weights) weights=0 ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see scripts/setup.sh --help)" >&2; exit 2 ;;
  esac
done

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "Setup required: $1 is not installed ($2)" >&2; exit 1; }
}

need uv "https://docs.astral.sh/uv/"
[ -x .venv/bin/python ] || uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -e '.[dev]' -c constraints.txt  # the tested versions

if [ "$dashboard" = 1 ]; then
  need pnpm "https://pnpm.io/installation, or rerun with --no-dashboard"
  (cd packages/dashboard && pnpm install --frozen-lockfile && pnpm build)
fi

if [ "$weights" = 1 ]; then
  .venv/bin/python - <<'PY'
from transformers import Sam2Model, Sam2Processor

ckpt = "facebook/sam2.1-hiera-tiny"
Sam2Processor.from_pretrained(ckpt)
Sam2Model.from_pretrained(ckpt)
print(f"SAM 2.1 weights ready: {ckpt}")
PY
fi

echo
echo "Ready. source .venv/bin/activate && python -m hasami_server  ->  http://127.0.0.1:8420"
echo "Tests: python -m pytest -q  (the live Muse test is skipped without MODEL_API_KEY)"
