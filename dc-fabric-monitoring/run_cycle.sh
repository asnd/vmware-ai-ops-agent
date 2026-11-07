#!/usr/bin/env bash
# One monitoring cycle (variant A). Schedule every 5 minutes, e.g.:
#   */5 * * * *  flock -n /run/dcmon.lock /opt/dcmon/run_cycle.sh >>/var/log/dcmon.log 2>&1
#
# Expects in the repo working copy:
#   snapshots/   – written by your existing SNMP poller (one JSON per device)
#   state/       – alarm_delta.py state (NOT committed: it holds last_seen
#                  timestamps and would create a commit every cycle)
#   inventory.yaml
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/dcmon/data}"
cd "$REPO_DIR"

# 1. Poll. Replace with your existing script; it must write deterministic,
#    sorted snapshots without poll timestamps so unchanged data = no diff.
"${POLLER:-./poll_alarms.py}" --out snapshots/

# 2. Delta + notifications (Teams/e-mail credentials come from the environment).
python3 "${DELTA:-/opt/dcmon/alarm_delta.py}" \
  --snapshot-dir snapshots/ \
  --state state/alarm_state.json \
  --inventory inventory.yaml \
  --summary-file /tmp/dcmon-summary

# 3. Commit only when the alarm picture actually changed.
git add -A snapshots/
if ! git diff --cached --quiet; then
  git commit -q -m "alarms: $(cat /tmp/dcmon-summary)"
  git push -q || echo "push failed, will retry next cycle" >&2
fi
