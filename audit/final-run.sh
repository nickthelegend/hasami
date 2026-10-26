#!/usr/bin/env bash
# One foreground audit session; every subprocess is owned by this process tree.
set -euo pipefail
cd "$(dirname "$0")/.."
unset MODEL_API_KEY HASAMI_REALCAM HASAMI_SO101_PORT HASAMI_DISTURB MODEL_API_BASE_URL
export HF_HOME='/Volumes/Extreme SSD/.cache/huggingface'
slot='/Volumes/Extreme SSD/Projects/grokbot/tools/heavy-slot.sh'
export HASAMI_PHOTO_PIDS="$(mktemp -t hasami-final-pids)"
mkdir -p .scratch audit/final
cleanup() {
  .venv/bin/python - "$$" <<'PY'
import os, signal, subprocess, sys
root = int(sys.argv[1])
rows = [line.split(None, 2) for line in subprocess.check_output(['ps','-axo','pid=,ppid=,comm='],text=True).splitlines()]
owned = {root}
for _ in range(20):
    for p, parent, command in rows:
        if int(parent) in owned: owned.add(int(p))
for p, parent, command in reversed(rows):
    pid = int(p)
    if pid in owned and pid not in (root, os.getpid(), os.getppid()):
        try: os.kill(pid, signal.SIGTERM)
        except ProcessLookupError: pass
PY
  "$slot" release hasami-audit-final
  rm -f "$HASAMI_PHOTO_PIDS"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$slot" acquire hasami-audit-final
pnpm --dir packages/dashboard build > audit/final/build.txt 2>&1
node audit/build-components.mjs >> audit/final/build.txt 2>&1
echo 'FINAL SLOT READY: landing, dashboard, photo, api, mcp, cli, setup, twin-adjust, or finish'
while IFS= read -r stage; do
  case "$stage" in
    landing|dashboard|photo)
      (cd audit && pnpm exec playwright test "tests/$stage.spec.ts" --project=chromium --workers=1 --trace=off --reporter=list > "final/$stage-playwright.txt" 2>&1) && echo "$stage PASS" || echo "$stage FAILED (see log)"
      ;;
    api|mcp|cli|setup|twin-adjust)
      HASAMI_AUDIT_PART="$stage" .venv/bin/python audit/cli/execute.py > "audit/final/$stage.txt" 2>&1 && echo "$stage PASS" || echo "$stage FAILED (see log)"
      ;;
    finish) break ;;
  esac
done
