# messagebus

A tiny, durable message bus that lets multiple AI coding agents — different
Claude Code sessions, Codex, different models — talk to each other, ask
questions, and work a shared task list together. You inject the first prompt;
the agents discuss and coordinate; GitHub issues keep order.

## Why Redis Streams (not pub/sub)

Agent sessions are **turn-based**: they aren't sitting on an open socket. Redis
pub/sub is fire-and-forget — a message published while an agent is mid-turn is
gone. Streams are a **durable log**: every message persists and each agent keeps
its own read cursor, so an agent that was busy (or dead) catches up on wake. The
`wait` command uses blocking `XREAD` for a push-like feel without the loss.

## Two coordination layers

| Layer | Mechanism | Role |
|-------|-----------|------|
| Chatter | Redis Stream per room | discussion, questions, answers |
| Task state | GitHub issue labels | durable, human-visible state machine |
| Claim race | Redis `SET NX` lock | atomically decides who owns an issue |

The Redis lock resolves the race the instant two agents reach for the same
issue; the gh label is the durable record humans read.

## Setup

```bash
pip install -r requirements.txt      # redis-py
./scripts/start-redis.sh             # starts local redis if not running
export BUS_GH_REPO=owner/repo        # which repo the issue state machine targets
./bus init                           # create the status:* labels in that repo
./bus doctor                         # verify redis + gh
```

`bus` usually runs from this directory, which is **not** your project's git
repo — so issue/label commands need `BUS_GH_REPO=owner/repo` to know where to
act. `bus init` creates the six `status:*` labels there (idempotent).

## Quick start (2 agents, by hand)

```bash
# terminal A – register + read prompt for agent "claude-1"
./scripts/agent-bootstrap.sh claude-1

# terminal B – same for "codex-1"
./scripts/agent-bootstrap.sh codex-1

# inject the opening prompt to everyone
./bus send --from operator --to all "Let's build feature X. Discuss approach, claim gh issues, go."

# each agent, every turn:
./bus poll --as claude-1
./bus send --from claude-1 --to codex-1 --kind question "Who takes the API layer?"
./bus claim --as codex-1 --issue 42
./bus status --as codex-1 --issue 42 --set status:pr-open
```

## Autonomy (agents keep talking without you)

Wire the Stop hook into each agent's Claude Code `settings.json` so a session
re-invokes itself whenever a message for it lands:

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

- The hook polls (then briefly waits) for messages addressed to the agent. If
  any arrive it blocks the stop and feeds them back to the model; if the room is
  quiet the agent goes idle.
- **Codex (and any hookless CLI): use `scripts/agent-loop.sh`.** It blocks on
  `bus wait` and pipes each delivered message into your agent command on stdin;
  the agent replies via `bus send`. This also beats the Stop hook on liveness —
  an idle agent here still wakes when a message lands later, whereas a Stop hook
  that already allowed the session to stop won't restart itself.
  ```bash
  scripts/agent-loop.sh codex-1 codex exec -           # hookless CLI
  scripts/agent-loop.sh claude-2 claude -p --append-system-prompt "$(scripts/agent-bootstrap.sh claude-2)"
  ```
- Stop an agent for good: `touch .bus-state/stop-claude-1` (or `stop-all`), or
  send it a message whose body is exactly `__SHUTDOWN__`.
- `BUS_MAX_TURNS` (default 200) caps consecutive auto-continues as a runaway guard.

> **Ordering matters: start agents BEFORE you inject the opening prompt.** `join`
> sets an agent's cursor to "now", so any message sent before an agent has
> joined is not delivered to it. Bring every agent online (watch for its
> presence in `bus agents`), *then* `bus send` the kickoff.

## Watch the conversation (supervised autonomy)

The strongest way to run this today is **supervised**: pre-break the work into gh
issues (the issues are the real coordination), let agents claim + execute +
report, and watch the bus live so you can intervene if they drift instead of
converge:

```bash
bus watch          # live-follow room "main"
bus --room smoke watch     # follow a specific room
```

`watch` is a read-only observer — it never registers presence or moves any
agent's cursor, so watching never perturbs the agents.

## Proving the drivers (before you trust an autonomous run)

`examples/echo-agent.sh` is a trivial agent that reads a bus message on stdin and
replies on the bus. Use it to prove `agent-loop.sh` drives a real external
process end-to-end before wiring a real CLI:

```bash
# terminal 1: drive the echo agent, and watch
scripts/agent-loop.sh echo-1 examples/echo-agent.sh echo-1 &
bus watch
# terminal 2 (after echo-1 shows in `bus agents`):
bus send --from operator --to echo-1 "ping"
#   -> you see echo-1's reply arrive live in the watch
touch .bus-state/stop-echo-1     # stop it
```

Swap `examples/echo-agent.sh echo-1` for your real CLI once the mechanism is
proven — e.g. `codex exec -` or `claude -p --append-system-prompt "$(scripts/agent-bootstrap.sh codex-1)"`.
The exact Codex/Claude invocation (does it read the prompt from stdin? one turn
per call?) is the only unverified piece — start with the echo agent, then wire
the real one.

## Commands

```
bus send     --from A [--to all] [--topic T] [--reply-to ID] [--kind msg] "text"
bus poll     --as A [--topic T]     # new messages for you, advance cursor
bus wait     --as A [--timeout 30] [--topic T] [--reply-to ID]  # block until a message arrives
bus tail     [-n 20]                # recent traffic, cursor untouched
bus history  [-n N]                 # full room log
bus watch    [-n 10] [--topic T]    # live-follow the room (observer, Ctrl-C to stop)
bus thread   ID                     # print the full reply_to thread of a message
bus inbox    --as A                 # peek unread for you (directed / questions), cursor untouched
bus prune    [--keep N] [--force]   # trim room to newest N (refuses if it drops unread)
bus join     --as A                 # register, cursor to "now"
bus agents                          # who's present
bus claim    --as A --issue N       # atomic claim (lock + label)
bus renew    --as A --issue N       # extend your claim TTL (long tasks)
bus release  --as A --issue N       # release your claim
bus reap     [--release ISSUE]      # list stale locks (holder gone); free one you've verified
bus status   --as A --issue N --set status:pr-open
bus board                           # table: gh issue + status + lock holder + presence
bus ws create --as A --issue N      # isolated git worktree for an issue you claimed
bus ws path   --issue N             # print the worktree path
bus ws list                         # all agent worktrees (dirty / unpushed / present)
bus ws remove --as A --issue N      # remove a worktree (refuses if dirty/unpushed)
bus init                            # create status:* labels in BUS_GH_REPO
bus doctor                          # check redis + gh
```

## Isolated parallel building (worktrees)

Task claims coordinate *who owns* an issue, but agents building in the **same
working directory** collide on files and the git index. `bus ws` gives each
claimed issue its own git worktree so agents build in true isolation:

```bash
bus claim --as A --issue 42 --worktree     # claim + isolated worktree in one step
cd "$(bus ws path --issue 42)"             # A builds here; the coordinator's tree is untouched
#   ... build, commit, git push, gh pr create ...
bus ws remove --as A --issue 42            # after merge (refuses if dirty/unpushed)
```

Each worktree is a sibling dir `../<repo>-worktrees/issue-N-A` on branch
`feat/issue-N-A`, cut from a **freshly fetched** `origin/<base>` (default `dev`;
`--base main` for hotfixes) — never a stale local ref. Safety, mirroring
prune/reap: `ws remove` refuses to destroy uncommitted **or unpushed** work
without `--force`; `claim --worktree` is **fail-closed** (if the worktree can't
be created, the claim is rolled back so an agent never silently works the main
tree); a dead agent's worktree is surfaced by `bus reap` but **never
auto-removed**. Config: `BUS_WORKTREE_ROOT` overrides the location,
`BUS_REPO_DIR` the target repo.

Claim locks default to an 8h TTL and auto-renew on every `bus status`. For a
task that outlives that without status changes, call `bus renew`. A claim also
cross-checks the gh `status:claimed` label, so an expired lock can't silently
let a second agent double-claim.

Global flags: `--room <name>` (default `main`), `--url <redis url>`, `--json`.

`--topic <t>` (poll/wait/watch) scopes reading to one thread by exact topic
match — cut the noise while working a single issue (`--topic issue-42`). The
cursor still advances past non-matching messages (they're consumed, not
re-read). One safety carve-out: a `__SHUTDOWN__` always passes the poll/wait
topic filter, so an agent scoping itself to a topic can't miss the kill-switch.

## Security / trust model

This is a **single-operator local tool**. It assumes every agent on the bus and
every local user of the machine is trusted. There is deliberately **no
authentication** — that's the "homegrown simple" tradeoff. Know the sharp edges:

- **Identity is self-asserted.** `--from` / `--as` are whatever the caller types.
  Any agent can impersonate `operator` or a peer, `bus join --as <victim>` to
  reset another agent's cursor, or `bus poll --as <victim>` to advance it (making
  the victim miss messages). Don't put an untrusted agent on the bus.
- **`--to all __SHUTDOWN__` is a fleet kill-switch.** One message stops every
  agent (Stop hook + `agent-loop.sh` both honor it). That's intended for the
  operator; it's also available to any agent on the bus.
- **Maintenance commands are not an auth boundary.** `bus prune` and
  `bus reap --release` are explicit manual commands and never run automatically,
  but any trusted local caller can invoke them. This matches the no-RBAC local
  trust model.
- **Message content is untrusted input to your agents.** A peer's message body
  is fed into each agent's context. The Stop hook fences it in an explicit
  "UNTRUSTED" boundary and `prompts/agent-system.md` tells agents not to obey
  embedded instructions, but a capable model can still be prompt-injected — run
  wired agents with **restricted tool permissions**, not full autonomy, if any
  message source isn't fully trusted.
- **Redis is unauthenticated on loopback.** `start-redis.sh` binds 127.0.0.1
  (fallback) or uses brew's loopback default. Any local user can read/forge bus
  traffic. Don't expose the Redis port off-host.

Injection is *not* a gap: `bus` shells out to `gh` via argv lists (no shell), the
Stop hook escapes message bodies through `json.dumps`, and agent ids / issue
numbers are charset/type validated. The exposure is the trust model above, not
code injection.

## Config

| Env | Default | Meaning |
|-----|---------|---------|
| `BUS_REDIS_URL` | `redis://127.0.0.1:6379/0` | Redis connection |
| `BUS_ROOM` | `main` | default room/stream |
| `BUS_GH_REPO` | — | `owner/repo` the issue commands target |
| `BUS_AGENT` | — | agent id (used by the Stop hook) |
| `BUS_WAIT_SECS` | `20`/`60` | hook idle-wait / agent-loop block interval |
| `BUS_MAX_TURNS` | `200` | hook runaway-loop cap |
