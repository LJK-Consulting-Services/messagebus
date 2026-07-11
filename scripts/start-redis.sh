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

# The bus keeps live coordination state in Redis — claim locks, the huddle write
# pen, huddle metadata, per-agent cursors — not just a replayable message log. A
# volatile server loses ALL of it on restart, so persistence is turned on for
# whichever server we end up talking to. AOF (not RDB): appendfsync everysec
# bounds loss to ~1s, where an RDB snapshot loses everything back to the last
# save point.
ensure_aof() {
  local mode
  # ONLY ever reconfigure a loopback Redis. BUS_REDIS_URL can point at a shared or
  # remote server this script did not start; turning on AOF there would force a
  # background rewrite on someone else's live instance, and CONFIG REWRITE would
  # persist that into their redis.conf. Not ours to change.
  case "$HOST" in
    127.0.0.1|::1|localhost) ;;
    *)
      echo "redis persistence: ${HOST} is not loopback — not touching its config." >&2
      echo "  (enable appendonly on that server yourself; the bus keeps live" >&2
      echo "   coordination state — locks, pen, huddle — that a restart would drop)" >&2
      return 0
      ;;
  esac
  mode="$(redis-cli -h "$HOST" -p "$PORT" config get appendonly 2>/dev/null | tail -1)"
  if [ "$mode" = "yes" ]; then
    echo "redis persistence: appendonly already on"
    return 0
  fi
  if ! redis-cli -h "$HOST" -p "$PORT" config set appendfsync everysec >/dev/null 2>&1 ||
     ! redis-cli -h "$HOST" -p "$PORT" config set appendonly yes >/dev/null 2>&1; then
    echo "WARNING: could not enable appendonly — a restart will drop claims/pen/huddle state" >&2
    return 0
  fi
  # Persist the change to the server's config file so it survives a restart too.
  # Fails when the server was started with no config file (our daemonized path,
  # which already got the flags on the command line) — not an error.
  redis-cli -h "$HOST" -p "$PORT" config rewrite >/dev/null 2>&1 || true
  echo "redis persistence: appendonly enabled (appendfsync everysec)"
}

if redis-cli -h "$HOST" -p "$PORT" ping >/dev/null 2>&1; then
  echo "redis already up on ${HOST}:${PORT}"
  ensure_aof
  exit 0
fi

# Prefer brew services (survives terminal close); fall back to a daemon bound to
# loopback only (no --bind defaults to all interfaces). brew starts redis from
# its own config, so it can't take flags — ensure_aof sets persistence over the
# wire afterwards, which covers both paths uniformly.
if command -v brew >/dev/null 2>&1 && brew list redis >/dev/null 2>&1; then
  echo "starting redis via brew services..."
  brew services start redis
else
  # --dir is not optional now that AOF is on: redis-server defaults `dir` to the
  # process's cwd, so without it the append log lands in whatever directory this
  # script was invoked from — usually the repo.
  DATA_DIR="${BUS_REDIS_DIR:-$HOME/.bus/redis}"
  # AOF means every message body the bus has ever carried is now written to disk,
  # and those bodies are untrusted agent chatter (see README security). Keep the
  # append log owner-only rather than the 0755/0644 the default umask would give.
  (umask 077 && mkdir -p "$DATA_DIR")
  chmod 700 "$DATA_DIR"
  echo "starting redis-server (daemonized, loopback only, data in ${DATA_DIR})..."
  redis-server --daemonize yes --bind 127.0.0.1 --port "$PORT" \
    --dir "$DATA_DIR" --appendonly yes --appendfsync everysec
fi

# wait for it
for _ in $(seq 1 20); do
  if redis-cli -h "$HOST" -p "$PORT" ping >/dev/null 2>&1; then
    echo "redis up on ${HOST}:${PORT}"
    ensure_aof
    exit 0
  fi
  sleep 0.25
done

echo "ERROR: redis did not come up on ${HOST}:${PORT}" >&2
echo "  (if BUS_REDIS_URL uses a non-default port, brew's redis may be on 6379 instead)" >&2
exit 1
