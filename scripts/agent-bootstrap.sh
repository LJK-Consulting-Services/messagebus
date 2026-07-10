#!/usr/bin/env bash
# Render the agent system prompt for a given agent id and register presence.
# Usage: scripts/agent-bootstrap.sh <agent-id> [room]
# Prints the ready-to-paste system prompt to stdout.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/bus-lib.sh"
AGENT="${1:?usage: agent-bootstrap.sh <agent-id> [room]}"
bus_require_valid_id "$AGENT"
[[ -n "${2:-}" ]] && ROOM="$2"

bus_ensure_online "$AGENT"

sed -e "s|{{AGENT_ID}}|$AGENT|g" \
    -e "s|{{BUS_PATH}}|$BUS|g" \
    -e "s|{{ROOM}}|$ROOM|g" \
    "$BUS_HOME/prompts/agent-system.md"
