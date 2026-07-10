#!/usr/bin/env bash
# Trivial demo "agent": reads a bus message batch on stdin and replies on the bus.
# Its only purpose is to PROVE that scripts/agent-loop.sh can drive a real,
# external, turn-based process end-to-end (the same contract Codex / headless
# Claude use): loop blocks on `bus wait` -> pipes the message to this process's
# stdin -> this process replies via `bus send`.
#
# Run it under the loop:
#   scripts/agent-loop.sh echo-1 examples/echo-agent.sh echo-1
# Then, from anywhere:
#   ./bus send --from operator --to echo-1 "ping"
#   ./bus watch          # you'll see echo-1's reply arrive live
set -euo pipefail

SELF="${1:?usage: echo-agent.sh <agent-id>}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOM="${BUS_ROOM:-main}"

body="$(cat)"                       # the delivered message(s), piped in by the loop
# here-string, not `printf | head` — a pipe would SIGPIPE on a large batch and,
# under `set -o pipefail`, abort the script before we ever reply.
IFS= read -r first_line <<<"$body" || true

# Reply on the bus. A real agent would reason here; the echo agent just acks.
"$HERE/bus" --room "$ROOM" send --from "$SELF" --to all \
  "echo-agent $SELF received a message and is replying (first line: ${first_line})" >/dev/null
