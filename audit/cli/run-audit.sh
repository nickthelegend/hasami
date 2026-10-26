#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../.."
unset MODEL_API_KEY HASAMI_REALCAM HASAMI_SO101_PORT HASAMI_DISTURB
export HF_HOME='/Volumes/Extreme SSD/.cache/huggingface'
slot='/Volumes/Extreme SSD/Projects/grokbot/tools/heavy-slot.sh'
audit_pid=''
cleanup() {
  if [ -n "$audit_pid" ]; then kill -TERM "$audit_pid" 2>/dev/null || true; wait "$audit_pid" 2>/dev/null || true; fi
  "$slot" release hasami-audit-cli
}
trap cleanup EXIT INT TERM
"$slot" acquire hasami-audit-cli
.venv/bin/python audit/cli/execute.py &
audit_pid=$!
wait "$audit_pid"
audit_pid=''
