# Changelog

All notable changes to messagebus. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions are git tags on `main`.

## [Unreleased]

### Added — production-readiness hardening (#77 roadmap)
- **Automated test suite + CI gate (#79/B1).** GitHub Actions on every PR into
  `dev`: pytest (3.11/3.12 matrix), a pinned Redis service for the atomic-Lua
  paths, `ruff`, a coverage floor, and `--require-hashes` dependency pinning.
- **git-ref collision safety (#82/B2).** Shared-branch writes lease against the
  driver's integrated tip (`--force-with-lease`) with a post-push `ls-remote`
  verify and a `bus huddle recover` helper — concurrent pushes can no longer
  silently reset a branch tip.
- **Redis resilience + durability (#83/B3).** Connection retry with backoff and
  a health check (keeping the load-bearing `socket_timeout`), a reconnect
  wrapper so `wait`/`watch` survive a Redis restart, AOF persistence in
  `start-redis.sh`, and cursor-aware retention that never drops an unread
  message.
- **Stop-hook self-continue parity (#84/B4).** The Claude Code Stop hook now
  re-invokes an agent until it signals a terminal done-marker, matching
  `agent-loop.sh` — idle agents no longer stall mid-task.
- **Structured observability (#81/B6).** A machine-readable `kind='event'` layer
  (`announce_event` + `bus events`) with allowlisted, redacted payloads.
- **Message-envelope `schema_version` stamp (#80).** `make_fields` stamps
  `MESSAGE_SCHEMA_VERSION`; readers tolerate legacy messages (missing field →
  version 0). Distinct from the event envelope's `EVENT_SCHEMA_VERSION`.

### Fixed
- **Huddle done-gate bypass (#83 follow-ups #91/#94).** Concurrent `join` could
  be erased by blind meta writes, letting a huddle close with a present but
  unsigned participant. All `k_huddle` writers now use WATCH/MULTI and honour a
  failed CAS instead of proceeding.

## [0.1.0]
- Initial release: Redis-Streams message bus, gh-issue state machine, per-agent
  worktree isolation, and the huddle (write-pen + done-gate) for co-authored code.
