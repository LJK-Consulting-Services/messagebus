# messagebus

A tiny, durable message bus that lets multiple AI coding agents — different
Claude Code sessions, Codex, different models — talk to each other, ask
questions, coordinate a shared task list, and **co-author the same code**. You
inject the first prompt; the agents discuss, claim work, build in isolation, and
(via the *huddle*) write one file together with peer review and a done-gate.

Everything is one Python file (`bus`) over Redis, plus a few shell scripts. No
server, no framework, no auth — a single-operator local tool.

## Why Redis Streams (not pub/sub)

Agent sessions are **turn-based**: they aren't sitting on an open socket. Redis
pub/sub is fire-and-forget — a message published while an agent is mid-turn is
gone. Streams are a **durable log**: every message persists and each agent keeps
its own read cursor, so an agent that was busy (or offline) catches up on wake.
The `wait` command uses a blocking `XREAD` for a push-like feel without the loss.

## Coordination layers

| Layer | Mechanism | Role |
|-------|-----------|------|
| Chatter | Redis Stream per room | discussion, questions, answers |
| Task ownership | Redis `SET NX` lock | atomically decides who owns an issue |
| Task state | GitHub issue `status:*` labels | durable, human-visible state machine |
| Isolation | per-agent `git worktree` | agents build without colliding on files |
| Co-authorship | the *huddle* (write-pen + done-gate) | many agents, one branch, one writer at a time |

---

## Prerequisites

- **Python 3.9+** with **redis-py** (`pip install -r requirements.txt`; the file
  pins `redis>=5.0`).
- **Redis server** on localhost. `scripts/start-redis.sh` starts one via Homebrew
  or `redis-server` if none is running.
- **git** — required only for the worktree (`ws`) and huddle features.
- **GitHub CLI (`gh`), authenticated** (`gh auth login`) — required only for the
  issue commands (`claim`/`status`/`board`/`init`/`huddle`). Plain messaging
  (`send`/`poll`/`wait`/…) needs Redis only.

`bus` is a self-contained executable (`#!/usr/bin/env python3`). Run it as
`./bus` from the repo directory, or symlink it onto your `PATH`.

## Install & setup

```bash
git clone https://github.com/LJK-Consulting-Services/messagebus.git
cd messagebus
pip install -r requirements.txt        # redis-py

./scripts/start-redis.sh               # start local Redis if not already up
./bus doctor                           # verify Redis (+ gh, if you use issues)
```

If you want the GitHub-issue task features, also:

```bash
export BUS_GH_REPO=owner/repo           # which repo the issue commands target
./bus init                              # create the six status:* labels there (idempotent)
```

`bus` usually runs from *this* directory, which is **not** your project's git
repo — so issue/label commands need `BUS_GH_REPO=owner/repo` to know where to act.

---

## Usage

### 1. Messaging

```bash
./bus join   --as alice                         # register presence (cursor = "now")
./bus send   --from alice --to bob "ping"        # direct message
./bus send   --from alice --to all "hello team"  # broadcast
./bus send   --from alice --to bob --kind question --topic issue-42 "who takes the API?"
./bus poll   --as bob                            # read new messages for bob, advance cursor
./bus wait   --as bob --timeout 30               # block until a message for bob arrives
./bus watch                                      # live-follow the room (read-only, Ctrl-C to stop)
```

`--from`/`--as` are self-asserted agent ids (`[A-Za-z0-9._-]`). `send` takes the
body as the last argument, or `-` to read stdin.

> **Ordering: start agents BEFORE injecting the opening prompt.** `join` sets an
> agent's cursor to "now", so a message sent before an agent has joined is not
> delivered to it. Bring agents online (confirm with `./bus agents`), *then* send.

### 2. Task coordination (GitHub issues)

Requires `BUS_GH_REPO` set and `gh` authenticated.

```bash
./bus claim  --as alice --issue 42               # atomic claim: Redis lock + status:claimed label
./bus status --as alice --issue 42 --set status:pr-open
./bus board                                      # table: issue | status | lock holder | present?
./bus renew  --as alice --issue 42               # extend the claim TTL (default 8h) on a long task
./bus release --as alice --issue 42              # give up the claim
./bus reap                                       # list stale locks (holder gone); --release <N> frees one
```

A claim is a Redis `SET NX` lock (the atomic race arbiter) plus a durable gh
label. A second agent claiming the same issue is rejected. `claim` also
cross-checks the `status:claimed` label so an expired lock can't silently allow a
double-claim.

### 3. Isolated parallel building (worktrees)

Agents building in the *same* working directory collide on files and the git
index. `ws` gives each claimed issue its own git worktree:

```bash
./bus claim --as alice --issue 42 --worktree     # claim + create an isolated worktree
cd "$(./bus ws path --issue 42)"                 # build here — the main tree is untouched
#   ... edit, commit, git push, gh pr create ...
./bus ws list                                    # all worktrees: dirty / unpushed / present
./bus ws remove --as alice --issue 42            # after merge (refuses if dirty/unpushed)
```

Each worktree is a sibling dir `../<repo>-worktrees/issue-N-<agent>` on branch
`feat/issue-N-<agent>`, cut from a **freshly fetched** `origin/<base>` (default
`dev`; `--base main` for hotfixes) — never a stale local ref. `ws remove` refuses
to destroy uncommitted **or unpushed** work without `--force`; `claim --worktree`
is **fail-closed** (rolls the claim back if the worktree can't be created). Config:
`BUS_WORKTREE_ROOT` overrides the location, `BUS_REPO_DIR` the target repo.

### 4. Co-authoring one file (the huddle)

A **huddle** lets several agents author *one* shared branch together. A single
**write-pen** (a Redis mutex) means only its holder commits at any moment; the
pen changes hands, so the code has many authors but never two writers at once.
A **done-gate** requires everyone to sign off at the final commit before close.

```bash
# alice opens a huddle on issue 42 and holds the pen
./bus huddle open  --as alice --issue 42
./bus huddle join  --as bob   --issue 42          # admit bob as a participant

# alice writes in her per-driver worktree, then checkpoints (commit + push, one command)
cd "$(ls -d ../*-worktrees/huddle-42-alice)"      # (created by the first checkpoint)
./bus pen checkpoint --as alice --issue 42        # creates the worktree on first run
#   ... edit files ...
./bus pen checkpoint --as alice --issue 42        # commit + push your work to the shared branch
./bus signoff        --as alice --issue 42        # sign off at the current tip
./bus pen pass       --as alice --issue 42 --to bob   # hand off (commits first)

# bob's worktree fast-forwards to alice's work; he builds on it, then signs off
./bus pen checkpoint --as bob --issue 42          # ff to alice's tip; edit; checkpoint
./bus signoff        --as bob --issue 42          # (this push makes alice's signoff stale — she re-signs)
./bus signoff        --as bob --issue 42 --block "the error path is untested"   # or hard-block instead
./bus unblock        --as bob --issue 42          # lift your own block

# close is GATED: every present participant must have signed off at the CURRENT tip, no open block
./bus huddle close   --as alice --issue 42        # advances the issue to status:pr-open
```

Dynamic lead: `./bus pen take --as bob --issue 42 --reason "<evidence>"` challenges
for the pen. It force-takes only from an **absent** driver (and only after a grace
window); a *present* driver keeps the pen until it `pen pass`es (concede) or
`pen deny`s. Pen/huddle ops refresh presence, so an actively-driving agent is
never force-taken. Any new push dismisses stale sign-offs, so a huddle can't be
closed over code nobody re-approved.

---

## Full command reference

```
# messaging
bus send     --from A [--to all] [--topic T] [--reply-to ID] [--kind msg] "text" | -
bus poll     --as A [--topic T]              # new messages for you; advances your cursor
bus wait     --as A [--timeout 30] [--topic T] [--reply-to ID]   # block until one arrives
bus tail     [-n 20]                         # recent traffic; cursor untouched
bus history  [-n 1000]                       # full room log
bus watch    [-n 10] [--topic T]             # live-follow (read-only observer)
bus thread   ID                              # print the full reply_to thread of a message
bus inbox    --as A                          # peek unread directed msgs / questions (no cursor move)
bus prune    [--keep 5000] [--force]         # trim the room to newest N (refuses if it drops unread)
bus join     --as A                          # register presence; cursor to "now"
bus agents                                   # who is present

# task coordination (needs BUS_GH_REPO + gh)
bus claim    --as A --issue N [--ttl 28800] [--worktree] [--base dev]
bus renew    --as A --issue N [--ttl 28800]  # extend your claim TTL
bus release  --as A --issue N                # release your claim
bus reap     [--release N]                   # list stale locks; free one you've verified
bus status   --as A --issue N --set <status:open|claimed|pr-open|merged|deployed|verified>
bus board                                    # gh issue + status + lock holder + presence
bus init                                     # create the status:* labels in BUS_GH_REPO

# worktree isolation (needs git)
bus ws create --as A --issue N [--base dev] [--type feat] [--allow-stale] [--allow-nested]
bus ws path   --issue N                      # print the worktree path
bus ws list                                  # all worktrees: dirty / unpushed / present
bus ws remove --as A --issue N [--force]

# huddle — co-authored code (needs git + gh)
bus huddle open   --as A --issue N [--base dev] [--ttl 28800] [--allow-stale]
bus huddle join   --as B --issue N
bus huddle status --issue N
bus huddle close  --as A --issue N [--force]
bus pen status     --issue N
bus pen checkpoint --as A --issue N          # holder: commit + push WIP to the shared branch
bus pen pass       --as A --issue N --to B   # hand off (commits first; aborts if the push fails)
bus pen take       --as B --issue N --reason "<evidence>"   # challenge / force-take from absent driver
bus pen deny       --as A --issue N --reason "<why>"        # driver rejects a challenge
bus signoff  --as A --issue N [--block "<reason>"]   # sign off at the current tip, or hard-block
bus unblock  --as A --issue N                        # lift your own block

# diagnostics
bus doctor                                   # check Redis + gh
```

Global flags (before the subcommand): `--room <name>` (default `main`),
`--url <redis url>` (default `redis://127.0.0.1:6379/0`), `--json` (machine output).

`--topic <t>` (poll/wait/watch) scopes reading to one thread by exact match — cut
the noise while working one issue (`--topic issue-42`). The cursor still advances
past non-matching messages. A `__SHUTDOWN__` always passes the poll/wait topic
filter, so a topic-scoped agent can't miss the kill-switch.

---

## Autonomy (agents run without you between messages)

Two ways to keep an agent reacting to the bus:

**Stop hook (Claude Code).** Wire it into the agent's `settings.json` so a session
re-invokes itself when a message for it lands:

```json
{
  "hooks": {
    "Stop": [
      { "hooks": [ { "type": "command",
        "command": "BUS_AGENT=claude-1 /ABS/PATH/messagebus/hooks/stop-hook.sh" } ] }
    ]
  }
}
```

**`agent-loop.sh` (Codex / any hookless CLI).** It blocks on `bus wait`, pipes
each delivered message to your agent command on stdin, and — crucially —
**re-invokes the agent until it signals a terminal handoff** (`touch
"$BUS_DONE_MARKER"`, i.e. work pushed + review requested, or a blocker posted),
so an agent completes a multi-step task in one wake instead of stalling after
step 1:

```bash
# Codex:
./scripts/agent-loop.sh codex-1 codex exec --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check "$(./scripts/agent-bootstrap.sh codex-1)"
# headless Claude:
./scripts/agent-loop.sh claude-2 claude -p --append-system-prompt "$(./scripts/agent-bootstrap.sh claude-2)"
```

`scripts/agent-bootstrap.sh <agent-id>` renders the agent's system prompt
(operating rules + the MB-SPEED speed protocol) and registers its presence.

- Stop an agent for good: `touch .bus-state/stop-<agent-id>` (or `stop-all`), or
  send it a message whose body is exactly `__SHUTDOWN__`.
- `examples/echo-agent.sh` is a trivial stdin→bus-reply agent to prove the loop
  drives a real external process before wiring a real CLI.

### Prove the driver first

```bash
# terminal 1: drive the echo agent + watch the bus
./scripts/agent-loop.sh echo-1 ./examples/echo-agent.sh echo-1 &
./bus watch
# terminal 2 (after echo-1 shows in `./bus agents`):
./bus send --from operator --to echo-1 "ping"     # its reply appears live in the watch
touch .bus-state/stop-echo-1                       # stop it
```

---

## Security / trust model

This is a **single-operator local tool**. It assumes every agent on the bus and
every local user of the machine is trusted. There is deliberately **no
authentication** — the "homegrown simple" tradeoff. Sharp edges:

- **Identity is self-asserted.** `--from`/`--as` are whatever the caller types.
  Any agent can impersonate `operator` or a peer, `bus join --as <victim>` to
  reset another cursor, or `bus poll --as <victim>` to advance it. Don't put an
  untrusted agent on the bus.
- **`--to all __SHUTDOWN__` is a fleet kill-switch.** One message stops every
  agent. Intended for the operator; available to any agent on the bus.
- **Maintenance commands are not an auth boundary.** `bus prune` and
  `bus reap --release` are explicit manual commands (never automatic), but any
  local caller can invoke them.
- **Message content is untrusted input to your agents.** A peer's body is fed
  into each agent's context. The Stop hook fences it in an "UNTRUSTED" boundary
  and `prompts/agent-system.md` tells agents not to obey embedded instructions,
  but a capable model can still be prompt-injected — run wired agents with
  **restricted tool permissions** if any message source isn't fully trusted.
- **Redis is unauthenticated on loopback.** `start-redis.sh` binds `127.0.0.1`.
  Any local user can read/forge bus traffic. Don't expose the Redis port off-host.

Injection is *not* a gap: `bus` shells out to `gh`/`git` via argv lists (no
shell), the Stop hook escapes message bodies through `json.dumps`, untrusted
content printed to a terminal is control-char escaped, and agent ids / issue
numbers are charset/type validated.

---

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `BUS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection (also read by `start-redis.sh`) |
| `BUS_ROOM` | `main` | default room/stream |
| `BUS_GH_REPO` | — | `owner/repo` the issue/label/huddle commands target |
| `BUS_REPO_DIR` | cwd's repo | the git repo worktrees/huddles operate on |
| `BUS_WORKTREE_ROOT` | `../<repo>-worktrees` | where per-agent worktrees live |
| `BUS_AGENT` | — | agent id, used by the Stop hook |
| `BUS_WAIT_SECS` | `20` (hook) / `60` (loop) | how long each `bus wait` blocks |
| `BUS_MAX_CONTINUE` | `6` | `agent-loop.sh` self-continue cap (re-invocations per message) |
| `BUS_MAX_TURNS` | `200` | Stop-hook consecutive auto-continue cap (runaway guard) |
| `BUS_DONE_MARKER` | set by the loop | file an agent touches to signal its turn is complete |

Redis keys (for the curious): `bus:stream:<room>`, `bus:cursor:<room>:<agent>`,
`bus:presence:<room>:<agent>` (90s TTL), `bus:lock:issue:<N>`,
`bus:worktree:issue:<N>`, `bus:huddle:issue:<N>`, `bus:pen:issue:<N>`,
`bus:signoff:issue:<N>`, `bus:block:issue:<N>`.
