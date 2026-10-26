#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
slot='/Volumes/Extreme SSD/Projects/grokbot/tools/heavy-slot.sh'
export HASAMI_PHOTO_PIDS="$(mktemp -t hasami-photo-pids)"
cleanup() {
  while IFS='|' read -r pid started; do
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    # A PID from an earlier run may have been reused; compare its process start time.
    current="$(ps -p "$pid" -o lstart= 2>/dev/null | sed 's/^ *//;s/ *$//' || true)"
    if [ -n "$started" ] && [ "$current" = "$started" ]; then kill -KILL "$pid" 2>/dev/null || true; fi
  done < "$HASAMI_PHOTO_PIDS"
  "$slot" release hasami-audit-photo
  rm -f "$HASAMI_PHOTO_PIDS"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
"$slot" acquire hasami-audit-photo
unset MODEL_API_KEY HASAMI_REALCAM HASAMI_SO101_PORT HASAMI_DISTURB
if [ -d '/Volumes/Extreme SSD/.cache/huggingface' ]; then export HF_HOME='/Volumes/Extreme SSD/.cache/huggingface'; fi
(cd packages/dashboard && pnpm install --frozen-lockfile && pnpm build)
(cd audit && pnpm install --frozen-lockfile)
echo 'PHOTO SLOT READY: send run, run <grep>, build, or finish'
while IFS= read -r command; do
  case "$command" in
    run) (cd audit && pnpm exec playwright test tests/photo.spec.ts --workers=1 --trace=off --reporter=list 2>&1 | tee photo/playwright.txt) || true ;;
    run\ *) (cd audit && pnpm exec playwright test tests/photo.spec.ts --workers=1 --trace=off --reporter=list -g "${command#run }" 2>&1 | tee photo/playwright.txt) || true ;;
    build) (cd packages/dashboard && pnpm build) ;;
    finish) break ;;
  esac
  echo 'PHOTO COMMAND COMPLETE'
done
