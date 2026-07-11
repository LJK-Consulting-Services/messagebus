#!/usr/bin/env bash
# Drive ANY turn-based agent CLI from the bus, forever.
#
# This is the robust alternative to the Claude Code Stop hook, and the ONLY
# option for CLIs without hooks (Codex, etc.). It blocks on `bus wait`, and
# whenever a message addressed to this agent arrives, it feeds that message to
# the agent command on stdin. The agent is expected to reply via `bus send`
# itself (see prompts/agent-system.md).
#
# Unlike the Stop hook, an idle agent here never "goes dead": the loop keeps
# waiting and wakes the moment a message lands, however much later.
#
# Usage:
#   scripts/agent-loop.sh <agent-id> <agent-cmd> [args...]
#
# The agent command receives the delivered message(s) as text on stdin.
# Examples:
#   # headless Claude Code, one prompt per turn:
#   scripts/agent-loop.sh claude-2 claude -p --append-system-prompt "$(scripts/agent-bootstrap.sh claude-2)"
#   # Codex CLI (adjust to your codex invocation that reads stdin):
#   scripts/agent-loop.sh codex-1 codex exec -
#
# Stop it: touch .bus-state/stop-<agent-id> (or stop-all), or send it __SHUTDOWN__.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/bus-lib.sh"
WAIT_SECS="${BUS_WAIT_SECS:-60}"

AGENT="${1:?usage: agent-loop.sh <agent-id> <agent-cmd> [args...]}"; shift
bus_require_valid_id "$AGENT"
[[ $# -ge 1 ]] || { echo "agent-loop.sh: missing agent command" >&2; exit 2; }

bus_ensure_online "$AGENT"
echo "agent-loop: $AGENT online in room=$ROOM, driving: $*" >&2

while true; do
  if bus_should_stop "$AGENT"; then
    echo "agent-loop: stop file present, $AGENT exiting" >&2
    exit 0
  fi

  # block until a message for us arrives; branch on the exit code, not on text
  set +e
  msgs="$("$BUS" --room "$ROOM" wait --as "$AGENT" --timeout "$WAIT_SECS" 2>/dev/null)"
  rc=$?
  set -e
  case "$rc" in
    "$RC_SHUTDOWN") echo "agent-loop: received __SHUTDOWN__, $AGENT exiting" >&2; exit 0 ;;
    "$RC_DELIVERED") : ;;                 # have a message -> fall through
    "$RC_NONE") continue ;;               # clean timeout: bus already blocked, re-loop
    *)                                    # error (e.g. Redis down): bus returned fast, so
      echo "agent-loop: bus error (rc=$rc); backing off" >&2  # back off to avoid a fork-spin
      sleep 2; continue ;;
  esac

  echo "agent-loop: $AGENT got a message, invoking agent..." >&2
  # SELF-CONTINUE (MB-SPEED R1): one model invocation does one bounded turn and
  # returns, so a single feed makes the agent stop after step 1 (claim, then
  # idle). Instead, RE-INVOKE the agent until it signals a terminal handoff by
  # touching $BUS_DONE_MARKER (pushed + review-requested, or a posted blocker),
  # capped at BUS_MAX_CONTINUE. Each re-invocation inspects durable state (git
  # worktree, branch, the bus) to see where it is and finish — so a fresh
  # invocation still continues the task, it isn't starting over.
  export BUS_DONE_MARKER="$BUS_DIR/turn-done-$AGENT"
  rm -f "$BUS_DONE_MARKER"
  feed="$msgs"
  attempt=0
  while true; do
    if ! printf '%s\n' "$feed" | "$@"; then
      echo "agent-loop: agent command exited non-zero (continuing)" >&2
    fi
    [[ -f "$BUS_DONE_MARKER" ]] && { echo "agent-loop: $AGENT signaled terminal" >&2; break; }
    bus_should_stop "$AGENT" && break
    attempt=$((attempt + 1))
    if (( attempt >= ${BUS_MAX_CONTINUE:-6} )); then
      echo "agent-loop: $AGENT hit BUS_MAX_CONTINUE without a terminal signal; yielding" >&2
      break
    fi
    feed="CONTINUE your current task — you have NOT reached a terminal handoff (MB-SPEED R1). Keep going until your work is committed AND pushed AND you've requested review on the bus, OR you post a hard blocker as a question. Inspect your worktree, branch, and the bus to see where you are, then finish this turn. When you reach that terminal state, run:  touch \"$BUS_DONE_MARKER\"  — that tells the loop you are done."
  done
  rm -f "$BUS_DONE_MARKER"
done
