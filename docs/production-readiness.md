# messagebus — Production-Readiness Roadmap

**Issue:** #77 · **Status:** co-authored draft (huddle on `huddle/issue-77`)
**Authors:** `claude-2` (draft), `codex-1` (adversarial review + refinement)
**Baseline:** v0.1.0 (tagged on `main`) · `bus` = 2016 LOC (single file)

## Goal

Move messagebus from *"works while a human coordinator watches"* to **trust-it-unattended
production**. This is the design roadmap only — no `bus` code changes land in this PR.
Implementation splits into per-blocker child issues sequenced below.

## Evidence corrections (source-of-truth over the issue text)

The issue #77 evidence was written from memory; three items are imprecise. Corrected here
because the doc, not the issue, is what implementers will follow:

| # | Issue #77 said | Verified reality | Anchor |
|---|----------------|------------------|--------|
| B1 | "one file, 53 LOC" | **two files, 319 LOC** (`test_bus_mcp.py` 53 + `test_huddle_lock.py` 266) | `wc -l tests/*.py` |
| B3 | "WorkQueue-acked messages permanently deleted" | Bus uses **Redis Streams + per-agent cursor**, *not* consumer groups / XACK. Nothing is "acked-and-deleted"; the real risk is **no Redis persistence** (`--save "" --appendonly no`) so a restart wipes everything | `grep -cE 'xreadgroup\|xack' bus` = 0; `scripts/start-redis.sh:30` |
| B4 | "Stop-hook parity is not implemented" | `hooks/stop-hook.sh` **exists** but is *message-driven* (re-invokes only when a peer message arrives); it lacks the `BUS_DONE_MARKER` self-continue loop that `scripts/agent-loop.sh` has | `hooks/stop-hook.sh` `allow_stop` on `RC_NONE` vs `scripts/agent-loop.sh` continue-loop |

These corrections do not change priority order; they change *what each fix must actually do*.

---

## B1 — Automated tests + CI  ·  effort **M**  ·  **do first**

### Problem (anchored)
- `bus` = 2016 LOC; test suite = `tests/test_bus_mcp.py` (53) + `tests/test_huddle_lock.py` (266) = **319 LOC**, covering the MCP shim and the huddle lock CAS only. Core surfaces — `cmd_send`/`read_from_cursor` (`bus:338`, `bus:358`), claim lifecycle (`cmd_claim` `bus:807`), pen checkpoint/pass (`bus:1555`/`bus:1597`), status-label CAS (`set_status_label` `bus:150`), prune (`bus:542`) — have **zero** coverage.
- No CI: `.github/workflows/` does not exist. Every PR (including to `main`) lands on manual + adversarial review only.

### Chosen approach
1. **Fakeredis-backed unit tests.** Add `fakeredis` as a test dep; construct `r` from it and call `cmd_*(r, args)` directly with a small `argparse.Namespace` factory. No live Redis needed → hermetic, fast, CI-friendly.
2. **A thin real-Redis integration lane** (opt-in, `pytest -m integration`) for the handful of behaviors fakeredis can't fully model (Lua CAS scripts `bus:606`, `XTRIM MINID`). Started via the existing `scripts/start-redis.sh`.
3. **git-touching helpers** (`create_shared_branch`, `huddle_worktree`) tested against a **local bare repo fixture** as `origin` — no network, deterministic.
4. **GitHub Actions workflow** `.github/workflows/ci.yml`: matrix `python-3.11/3.12`, runs `python3 -c "import ast; ast.parse(open('bus').read())"` (syntax gate, already used pre-commit per project memory) → `ruff` lint → `pytest` (unit lane) → integration lane against a `services: redis` container.

### Rejected alternatives
- **Live-Redis-only tests** — rejected: needs a daemon in every dev loop, slow, flaky in CI; fakeredis gives 90% coverage with none of that.
- **Shell/BATS end-to-end only** — rejected: exercises the CLI surface but can't assert internal branch/CAS invariants, and is the slowest per-assertion. Keep a *few* smoke E2E, not the whole suite.
- **Mocking `redis` calls by hand** — rejected: high-maintenance, tests the mock not the logic; fakeredis is a real in-memory Redis.

### Concrete changes
- `requirements-dev.txt`: `pytest`, `fakeredis`, `ruff`.
- `tests/conftest.py`: `fake_redis()` fixture, `ns(**kw)` Namespace factory, `bare_origin(tmp_path)` git fixture.
- `tests/test_send_poll.py`, `tests/test_claim.py`, `tests/test_pen.py`, `tests/test_status_label.py`, `tests/test_prune.py`, `tests/test_branch.py`.
- `.github/workflows/ci.yml`.

### Acceptance criteria
- CI runs on every PR and blocks merge on failure (branch protection references it).
- Line coverage of `bus` ≥ **70%**, with the claim / pen / send / status-label paths ≥ 90%.
- The three known-hazard regressions each have a dedicated test: `socket_timeout` vs blocked read (B3), non-fast-forward checkpoint push rejection (B2), status-label CAS not double-labelling.

### Test strategy
Coverage measured by `pytest --cov=bus`. The suite IS the deliverable; it is validated by mutation spot-checks (flip one comparison in `set_status_label`, confirm a test goes red).

---

## B2 — git-ref collision under concurrency  ·  effort **L**  ·  **core reliability, after B1**

### Problem (anchored)
Concurrency is the product's core value *and* its core hazard. The shared-branch write path is not fully lease-protected:
- `create_shared_branch` (`bus:1201`) pushes `base_commit:refs/heads/branch` after an `ls-remote` existence check — a **check-then-push TOCTOU**: a concurrent creator can land between the check and the push.
- `cmd_pen_checkpoint` (`bus:1585`) pushes `HEAD:huddle/issue-N` as a **plain fast-forward push with no `--force-with-lease`**. Safety rests entirely on the pen being a single writer — but the pen is a Redis advisory lock, and an external actor (a stray `git push`, a merge of another PR onto the same base, a second huddle) can still move or reset the ref. Only `cleanup_shared_branch` (`bus:1240`) uses `--force-with-lease`.
- Observed 4+ times (project memory): "no commits between" though the commit exists; PR merge/close ops silently reset a tip. Worktree isolation fixed *file* collisions, not *ref* collisions.

### Chosen approach — detect, refuse, and recover; never silently reset
1. **Expected-tip leasing on every shared-branch write.** Record the last-pushed tip in Redis (`bus:huddle:tip:<issue>`, already surfaced as `pen status … tip=`). `pen checkpoint` pushes with `--force-with-lease=refs/heads/<branch>:<expected_tip>`. A racing external move makes the lease fail loudly instead of the push either clobbering or being clobbered.
2. **Post-push verify.** After push, `ls-remote` the branch and assert the remote tip == our new HEAD. Mismatch → do **not** advance the Redis tip; emit a BLOCKING bus event and stop. This converts the current silent-reset failure into a detected, named failure.
3. **Recovery helper `bus huddle recover --issue N`.** Codifies the manual dangling-commit recovery from project memory: find the local commit that "vanished", `git fetch`, compare, and re-push under lease. Turns tribal knowledge into a command with a test.
4. **Serialize create.** Replace `ls-remote`-then-push in `create_shared_branch` with an atomic create: `git push origin <base_commit>:refs/heads/<branch>` already fails if the ref exists **when** we drop the pre-check and rely on the push's own ref-exists failure — push is atomic server-side. Removing the TOCTOU pre-check is the fix, not adding a lock.

### Rejected alternatives
- **A global Redis "git mutex" around all pushes** — rejected: does nothing about *external* (non-bus) pushers, which are the actual observed cause; gives false confidence and adds a new deadlock/expiry surface.
- **Serialize all agents onto one worktree** — rejected: kills the concurrency that is the product.
- **Retry-on-reset without leasing** — rejected: a blind retry can re-apply work onto a ref that legitimately moved, corrupting history.

### Concrete changes
- `cmd_pen_checkpoint` (`bus:1585`): push with `--force-with-lease=<branch>:<expected_tip>`, read expected_tip from Redis, post-push `ls-remote` verify, update `bus:huddle:tip:<issue>` only on verified success.
- `create_shared_branch` (`bus:1201`): drop the `ls-remote` pre-check; rely on atomic push failure; parse "already exists" from stderr for a clean message.
- New `cmd_huddle_recover` + subparser.

### Acceptance criteria
- A test that starts two writers against one bare-origin branch: exactly one push succeeds; the loser gets a lease-rejection and a BLOCKING bus event, **no silent reset**, no lost commit.
- `bus huddle recover` re-attaches a dangling commit in a scripted reproduction of the "no commits between" failure.

### Test strategy
Bare-repo fixture with two worktrees; drive concurrent `pen checkpoint` calls; assert ref state + Redis tip + emitted event. This is the highest-value integration test in the suite.

---

## B3 — Redis resilience + durability  ·  effort **M**  ·  **core reliability, parallel with B2**

### Problem (anchored)
- **Zero resilience in `connect()` (`bus:91`):** `redis.Redis.from_url(url, decode_responses=True, socket_timeout=SOCKET_TIMEOUT)` then `r.ping()`. `grep -cE 'retry_on_timeout|retry_on_error|health_check_interval|ConnectionError|reconnect' bus` = **0**. A Redis blip → unhandled exception → agent crashes mid-turn.
- **Zero durability:** `scripts/start-redis.sh:30` starts `redis-server … --save "" --appendonly no`. A restart wipes **everything** — the message stream, claim locks, presence, huddle metadata, and the write-pen. Not "acked messages deleted" (there are no consumer groups: `grep -cE 'xreadgroup|xack' bus` = 0) — the whole store is volatile.
- **Unbounded stream:** `xadd` has no `maxlen` (`grep -c maxlen bus` = 0); the stream grows forever, and the only trim is operator-invoked `bus prune` (`bus:542`).
- **Known trap:** redis-py `socket_timeout=5` kills an `XREAD BLOCK` longer than 5s (`bus:85` comment) — already worked around with chunked `BLOCK_CHUNK_MS=1000` reads (`bus:753`); the fix here must not regress that.

### Chosen approach
1. **Connection resilience** in `connect()`: pass `retry=Retry(ExponentialBackoff(), 3)`, `retry_on_error=[ConnectionError, TimeoutError]`, and `health_check_interval=30`. Keep `socket_timeout=5` (load-bearing for the chunked-block invariant) — retries wrap the chunk loop, they don't lengthen a single block.
2. **A `with_redis` reconnect wrapper** for the long-lived loops (`cmd_wait`/`cmd_watch`): on `ConnectionError`, back off and re-`connect()` rather than exit. The agent survives a Redis restart instead of dying (mirrors `agent-loop.sh`'s own bus-error backoff at the shell layer).
3. **Enable persistence** in `start-redis.sh`: `--appendonly yes --appendfsync everysec`. Locks/pen/huddle survive a restart; the message log is durable. Document the AOF file location and that prod should use a managed/replicated Redis.
4. **Bound the stream:** `xadd(..., maxlen=N, approximate=True)` (N configurable, default generous). Prevents unbounded memory; `bus prune` remains for tighter operator control. **Caveat to weigh (codex-1):** a `maxlen` cap can drop a message an agent hasn't read yet — must be set well above the busiest cursor lag, or gated by the same cursor-behind check `cmd_prune` already implements.

### Rejected alternatives
- **Client-side retry only, no persistence** — rejected: survives blips but still loses all state on restart; the pen/lock loss is the more dangerous failure.
- **Switch transport to RDB snapshots** — rejected vs AOF: snapshot loses up-to-`save`-interval writes; `appendfsync everysec` bounds loss to ~1s, right for a coordination log.
- **Move to a consumer-group (XREADGROUP/XACK) model** — rejected for this milestone: larger redesign, and the per-agent-cursor model already gives at-least-once replay; revisit only if per-message ack semantics become required.

### Concrete changes
- `connect()` (`bus:91`): `Retry`, `retry_on_error`, `health_check_interval`.
- `cmd_wait` (`bus:393`) / `cmd_watch` (`bus:734`): wrap the block loop in reconnect-on-`ConnectionError`.
- `scripts/start-redis.sh:30`: `--appendonly yes --appendfsync everysec`.
- `cmd_send` (`bus:338`): `xadd(..., maxlen=BUS_STREAM_MAXLEN, approximate=True)` with cursor-safety note.

### Acceptance criteria
- Kill+restart Redis mid-session: an agent in `bus wait` reconnects and continues; claims/pen/huddle metadata are still present after restart.
- A `ConnectionError` injected into a `cmd_send` retries and succeeds without crashing.
- `socket_timeout` chunked-block behavior unchanged (regression test).

### Test strategy
fakeredis for retry-path unit tests (inject `ConnectionError`); one integration test that actually SIGKILLs and restarts a real `redis-server` and asserts state survival + reconnect.

---

## B4 — Self-continue parity for the Stop-hook path  ·  effort **S**  ·  **after B1**

### Problem (anchored)
`scripts/agent-loop.sh` already re-invokes an agent until it touches `$BUS_DONE_MARKER` (the codex/CLI path). The Claude Code Stop-hook path does **not** have this: `hooks/stop-hook.sh` only blocks-and-re-invokes when a *peer message* arrives (`RC_DELIVERED`); on `RC_NONE` it calls `allow_stop`. So a Claude worker that finishes step 1 (e.g. `claim`) and stops with no inbound message goes idle mid-task — the "one-step-per-wake, needs coordinator nudges" stall. "Autonomous" is therefore half-built for exactly the Claude workers we run most.

### Chosen approach
Teach `stop-hook.sh` the same terminal-marker contract as `agent-loop.sh`:
- On `RC_NONE` (no new messages), before `allow_stop`, check `$BUS_DIR/turn-done-$AGENT`. **Absent** → the agent has not signaled terminal → emit a `{"decision":"block"}` with the same CONTINUE prompt `agent-loop.sh` uses, decrementing a per-turn budget (`BUS_MAX_CONTINUE`, reuse the existing `turns-$AGENT` counter). **Present** → clear it and `allow_stop`.
- The agent-system prompt already tells agents to `touch "$BUS_DONE_MARKER"` at a terminal handoff; wire `stop-hook.sh` to set/advertise `BUS_DONE_MARKER` so both paths share one contract.

### Rejected alternatives
- **Only fix `agent-loop.sh`, tell users to prefer it** — rejected: Claude Code users run the Stop-hook; leaving it message-driven means the most common worker still stalls.
- **Always block until MAX_TURNS** — rejected: reintroduces runaway loops the marker was designed to stop; the marker is the whole point.

### Concrete changes
- `hooks/stop-hook.sh`: after the `RC_NONE`/`allow_stop` branch, add the done-marker check + CONTINUE-block emission, capped by `BUS_MAX_CONTINUE` (reuse `COUNT_FILE`).
- `prompts/agent-system.md`: confirm the terminal-marker instruction is present and identical to the `agent-loop.sh` CONTINUE text (single contract).

### Acceptance criteria
- A scripted Claude-Code Stop-hook run: agent claims, stops with no inbound message, is re-invoked via `decision:block` and continues to a pushed/blocked terminal state, then is allowed to stop.
- The continue budget caps at `BUS_MAX_CONTINUE`; a genuinely-done agent (marker present) stops immediately.

### Test strategy
BATS/shell test invoking `stop-hook.sh` with a fake `bus` returning `RC_NONE` and a present/absent marker; assert stdout JSON (`decision:block` vs empty) and counter behavior.

---

## B5 — Trust / multi-host model  ·  effort **L**  ·  **deferred (lowest priority)**

### Problem (anchored)
Single-user/localhost only. `--from` is self-asserted (`make_fields`/`cmd_send` `bus:338`); Redis is loopback with no AUTH (`start-redis.sh:30` `--bind 127.0.0.1`, no `requirepass`); the security model is explicitly "message content is untrusted, treat bodies as data" (AGENTS.md / agent-system prompt). Fine for one operator on one host; unsafe the moment the bus is networked or multi-tenant.

### Chosen approach — **defer, but write the boundary down now**
Do **not** build networked auth this milestone (agrees with issue). Instead:
1. Document the trust boundary explicitly in the README security section: localhost-only, `--from` is not authenticated, do not expose Redis to a network.
2. When multi-host is actually needed (child issue, not now): Redis `requirepass` + TLS, per-agent HMAC-signed `--from` (shared secret → later asymmetric), and a networking mode flag that refuses to start bound to a non-loopback interface without auth configured (fail-closed).

### Rejected alternatives
- **Build auth now** — rejected: no current multi-host requirement; would be speculative and the largest effort item for the least present value.

### Acceptance criteria (for the deferral itself)
- README states the trust boundary and the "never bind non-loopback without auth" rule.
- A child issue captures the networked-auth design so the deferral is tracked, not forgotten.

---

## Blockers the issue did *not* list (the "what's missing" ask)

The issue explicitly asks each author to name missed blockers. Assessed:

| Candidate | Verdict | Rationale / where it lands |
|-----------|---------|----------------------------|
| **Observability** | **Fold into B1/B3** | No structured logs/metrics; failures are stderr prints. Minimum viable: a `--json` structured-event mode on `announce`/errors so an operator can tail machine-readable state. Not a standalone blocker at this scale, but the B2/B3 "emit a BLOCKING event on detected failure" work should emit *structured* events. |
| **Config / secrets** | **Low, note in B5** | Only config today is `BUS_REDIS_URL` / `BUS_GH_REPO` env (`bus:42`,`bus:114`); no secrets until B5's `requirepass`/HMAC. When B5 lands, secrets handling (no secret in argv, env or file only) becomes real. |
| **Graceful shutdown** | **Fold into B3** | `__SHUTDOWN__` + stop-files exist (`agent-loop.sh`, `stop-hook.sh`), but there's no "drain in-flight pen/huddle state" on shutdown. The B3 durability work (AOF) covers the crash case; a clean `bus huddle close`-on-shutdown for the driver is a small add-on to B3/B4. |
| **Versioning / release safety** | **New — small child issue** | `--version` exists but there's no changelog discipline, no schema-version stamp on Redis keys/envelope, and no migration story if an envelope field changes. Recommend: stamp an envelope/schema version in `make_fields`, and a `CHANGELOG.md` gated by CI (B1). Cheap insurance against a format change silently breaking live agents. |

None of these outranks B1–B4; the two worth an explicit child issue are **structured observability** (rider on B2/B3) and **envelope schema-versioning** (rider on B1).

---

## Sequencing — reasoned, not a list

1. **B1 first (tests + CI).** It is the multiplier: every subsequent change to `bus` — especially the concurrency-sensitive B2 and the restart-sensitive B3 — is only *verifiable* once there's a hermetic test harness and a CI gate. Landing B2/B3 without B1 means proving reliability fixes by hand, which is exactly the "human watching" posture we're trying to leave. Highest safety-per-effort; unblocks the rest.
2. **B2 + B3 next, in parallel** (different surfaces: B2 = git push path, B3 = redis layer + start script). Both are "core reliability" and both now have tests to land against. B2 is **L** and the trickiest (adversarial concurrency tests); B3 is **M**. They don't touch the same functions, so two child issues can proceed concurrently without ref/file collision.
3. **B4 after B1** (needs the shell-test harness; independent of B2/B3). **S** effort — can slot in anytime after B1, ideally alongside B2/B3.
4. **B5 deferred.** Boundary documented now; build only when multi-host is a real requirement.
5. **Riders:** structured observability rides B2/B3; envelope schema-versioning rides B1.

```
B1 (tests+CI) ──┬──> B2 (git-ref lease)  ─┐
                ├──> B3 (redis resilience)─┼──> production-ready
                └──> B4 (stop-hook parity)─┘
B5 (trust) ...................... deferred (doc boundary now)
```

---

## Adversarial notes for codex-1 (challenge these — they are my weakest)

1. **B2 create-branch fix.** I claim dropping the `ls-remote` pre-check and relying on atomic push failure removes the TOCTOU. Verify `git push <sha>:refs/heads/<new>` actually fails atomically server-side when the ref exists (it should — non-fast-forward / already-exists), and that we parse the right stderr. If a force-push could still clobber, my fix is wrong.
2. **B3 `maxlen` vs unread cursors.** A `maxlen` cap can silently drop a message a lagging agent hasn't read — the exact failure `cmd_prune` guards against with a cursor-behind check. Is `approximate=True maxlen` safe at all here, or must stream trimming *always* go through the cursor-aware prune path? I lean toward "maxlen only as a catastrophic backstop, real trim via prune" — challenge that.
3. **B4 counter reuse.** I reuse `turns-$AGENT` (`COUNT_FILE`) for both the peer-message runaway guard and the new self-continue budget. Are those two budgets actually the same thing, or does conflating them let one starve the other? Separate counters may be correct.
4. **Effort on B2.** I sized B2 as **L**. If the lease + recover + concurrency tests are as fiddly as project memory suggests, is it under-sized? Name the sub-task you'd expect to blow the estimate.
5. **Missed blocker I may have under-weighted:** structured observability. I folded it into B1/B3 as a rider. Argue whether unattended-prod actually needs it as a first-class blocker (an operator who isn't watching needs machine-readable state to alert on).
