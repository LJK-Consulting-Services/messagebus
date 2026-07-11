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
