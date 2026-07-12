#!/usr/bin/env bash
# Shared shell primitives for the bus drivers. Source this; don't execute it.
#   source "<path>/scripts/bus-lib.sh"
#
# Resolves paths relative to THIS file, so callers in scripts/ or hooks/ all
# agree on the repo root, the bus binary, the state dir, and the room.

# repo root = parent of the dir holding this lib
BUS_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUS="${BUS_BIN:-$BUS_HOME/bus}"
BUS_DIR="${BUS_DIR:-$BUS_HOME/.bus-state}"
ROOM="${BUS_ROOM:-main}"

# exit codes emitted by `bus poll` / `bus wait` (mirror of the Python constants)
RC_DELIVERED=0
RC_NONE=10
RC_SHUTDOWN=11

# reject agent ids that would escape filenames (turns-/stop-) or Redis keys.
# Mirrors the `ident` charset the bus CLI enforces on --as/--from.
bus_require_valid_id() {  # $1 = agent id
  if [[ ! "$1" =~ ^[A-Za-z0-9._-]+$ ]]; then
    echo "bus: invalid agent id '$1' (allowed: A-Za-z0-9._-)" >&2
    exit 2
  fi
}

# bring redis up and register the agent's cursor at "now"
bus_ensure_online() {  # $1 = agent id
  mkdir -p "$BUS_DIR"
  "$BUS_HOME/scripts/start-redis.sh" >&2
  "$BUS" --room "$ROOM" join --as "$1" >&2
}

# operator asked this agent (or everyone) to stop, via a sentinel file
bus_should_stop() {  # $1 = agent id
  [[ -f "$BUS_DIR/stop-all" || -f "$BUS_DIR/stop-$1" ]]
}

# Read unsigned integer config safely before Bash arithmetic sees it.
bus_uint_env() {  # $1 = env name, $2 = default
  local name="$1"
  local default="$2"
  local raw="${!name:-}"
  [[ -n "$raw" ]] || raw="$default"
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$((10#$raw))"
  else
    echo "bus: invalid $name='$raw' (expected unsigned integer); using $default" >&2
    printf '%s\n' "$default"
  fi
}

bus_uint_file() {  # $1 = path
  local raw
  raw="$(cat "$1" 2>/dev/null || true)"
  if [[ "$raw" =~ ^[0-9]+$ ]]; then
    printf '%s\n' "$((10#$raw))"
  else
    printf '0\n'
  fi
}

bus_shell_quote() {  # $1 = value
  printf '%q' "$1"
}

bus_continue_prompt() {  # $1 = concrete done-marker path
  local done_marker="$1"
  local quoted_marker
  quoted_marker="$(bus_shell_quote "$done_marker")"
  printf '%s' "CONTINUE your current task — you have NOT reached a terminal handoff (MB-SPEED R1). Keep going until your work is committed AND pushed AND you've requested review on the bus, OR you post a hard blocker as a question. Inspect your worktree, branch, and the bus to see where you are, then finish this turn. When you reach that terminal state, run:  touch $quoted_marker  — that tells the loop you are done."
}
