#!/usr/bin/env bash
# The real Hasami stack for the browser E2E, bound to 127.0.0.1:
#   serve.sh api      python -m hasami_server on :8420 (real MuJoCo + SAM 2.1), run records in e2e/.runs,
#                     twins in e2e/.twins
#   serve.sh landing  packages/landing as static files on :8421
# No MODEL_API_KEY, HASAMI_REALCAM or HASAMI_SO101_PORT is passed on: those stay "Setup required".
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
root="$(cd "$here/.." && pwd)"
case "${1:-}" in
  api)
    runs="$here/.runs"
    rm -rf "$runs" && mkdir -p "$runs"
    twins="$here/.twins"  # twins built by the tests go here, never into deployments/twins
    rm -rf "$twins" && mkdir -p "$twins"
    # Seed the register with the committed evidence runs (records + per-step images), never deployments/runs itself.
    for f in "$root"/deployments/runs/*.json; do
      cp "$f" "$runs/"
      d="${f%.json}"
      [ -d "$d" ] && cp -R "$d" "$runs/"
    done
    # their demo videos, when this checkout has them (videos are not in git): linked, not copied
    for v in "$root"/deployments/runs/*.mp4; do
      [ -f "$v" ] && ln -s "$v" "$runs/$(basename "$v")"
    done
    if [ -z "${HF_HOME:-}" ]; then
      unset HF_HOME  # an empty HF_HOME would put the cache in a relative folder
      if [ -d "/Volumes/Extreme SSD/.cache/huggingface" ]; then
        export HF_HOME="/Volumes/Extreme SSD/.cache/huggingface"
      fi
    fi
    exec env -u MODEL_API_KEY -u HASAMI_REALCAM -u HASAMI_SO101_PORT -u HASAMI_DISTURB \
      HASAMI_RUNS_DIR="$runs" HASAMI_TWINS_DIR="$twins" HASAMI_HOST=127.0.0.1 HASAMI_PORT=8420 \
      "$root/.venv/bin/python" -m hasami_server
    ;;
  landing)
    exec "$root/.venv/bin/python" -m http.server 8421 --bind 127.0.0.1 --directory "$root/packages/landing"
    ;;
  *)
    echo "usage: serve.sh api|landing" >&2
    exit 2
    ;;
esac
