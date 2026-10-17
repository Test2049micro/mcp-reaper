#!/usr/bin/env bash
# Scheduled reaper that branches on exit codes, not on human-readable output.
# The failure mode this replaces: matching "Will SIGTERM" in the plan text, so a
# copy change in the message silently turns the job into a no-op.
set -uo pipefail

LOG_DIR="${LOG_DIR:-$HOME/.local/state/mcp-reaper}"
mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/$(date -u +%Y-%m-%d).log"

PLAN_JSON="$(mktemp)"
mcp-reaper plan --json >"$PLAN_JSON" 2>>"$LOG"
case $? in
  0) echo "$(date -u +%FT%TZ) nothing eligible" >>"$LOG"; rm -f "$PLAN_JSON"; exit 0 ;;
  1) : ;;
  *) echo "$(date -u +%FT%TZ) plan failed" >>"$LOG"; rm -f "$PLAN_JSON"; exit 1 ;;
esac

COUNT=$(sed -e 's/.*"eligible": *\([0-9]*\).*/\1/' <<<"$(tr -d '\n' <"$PLAN_JSON)")
echo "$(date -u +%FT%TZ) $COUNT eligible, reaping" >>"$LOG"
mcp-reaper reap --yes >>"$LOG" 2>&1
case $? in
  2) echo "reaped cleanly" >>"$LOG" ;;
  3) echo "reaped with skips, inspect the log" >>"$LOG" ;;
  4) echo "refused: over --max, inspect before raising it" >>"$LOG" ;;
esac
rm -f "$PLAN_JSON"
