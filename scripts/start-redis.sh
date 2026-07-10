#!/usr/bin/env bash
# Start a local Redis for the bus, if one isn't already running.
# Idempotent: safe to run repeatedly. Reads the SAME config the bus uses
# (BUS_REDIS_URL) so the port it starts/polls always matches what `bus` talks to.
set -euo pipefail

URL="${BUS_REDIS_URL:-redis://127.0.0.1:6379/0}"

# derive host/port from the URL so there's one source of truth (not a second
# set of BUS_REDIS_HOST/PORT vars that could drift from BUS_REDIS_URL).
read -r HOST PORT < <(python3 - "$URL" <<'PY'
import sys, urllib.parse
u = urllib.parse.urlparse(sys.argv[1])
print(u.hostname or "127.0.0.1", u.port or 6379)
PY
)

if redis-cli -h "$HOST" -p "$PORT" ping >/dev/null 2>&1; then
  echo "redis already up on ${HOST}:${PORT}"
  exit 0
fi

# Prefer brew services (survives terminal close); fall back to a daemon bound to
# loopback only (no --bind defaults to all interfaces).
if command -v brew >/dev/null 2>&1 && brew list redis >/dev/null 2>&1; then
  echo "starting redis via brew services..."
  brew services start redis
else
  echo "starting redis-server (daemonized, loopback only)..."
  redis-server --daemonize yes --bind 127.0.0.1 --port "$PORT" --save "" --appendonly no
fi

# wait for it
for _ in $(seq 1 20); do
  if redis-cli -h "$HOST" -p "$PORT" ping >/dev/null 2>&1; then
    echo "redis up on ${HOST}:${PORT}"
    exit 0
  fi
  sleep 0.25
done

echo "ERROR: redis did not come up on ${HOST}:${PORT}" >&2
echo "  (if BUS_REDIS_URL uses a non-default port, brew's redis may be on 6379 instead)" >&2
exit 1
