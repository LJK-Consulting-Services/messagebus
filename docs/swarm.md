# Swarms on the bus

A swarm is one bus room: a coordinator, a few workers and a verifier, all posting to one durable log. Every member can
read the whole log, so a member that joins late catches up from the start. Members don't message each other privately;
the room is the record.

```
coordinator ──swarm-spawn──▶ picks a seat by role band and spare quota
                               │ bus join --replay, brief posted to @member
                               ▼
                            swarm-worker <member> <seat>
                               │ asleep in bus wait          state=waiting, 0 tokens
                               │ post for @member arrives ─▶ state=working, seat runs, tokens recorded
                               ▼
                 room log ◀── replies (bus send, or posted for seats that can't)
                    │
                    └──▶ swarm-board (TUI)        `--kind done` ─▶ members retire, board shows ✓ finished
```

## Addressing

`bus send --to` takes a comma list of agent ids, `@name`s and `all`. `@name` reaches the agent with that id, or every
member of group `name`:

```
bus send --from coordinator --to @api,verifier - <<'EOF'
...
EOF
```

`@name` is stored as written and resolved when each agent reads, so a member that joins a group later still gets the
group's earlier posts.

## Membership, state and usage

```
bus --room swarm-x title "Epic 62: Phase 0 hardening"
bus --room swarm-x join --as w1 --replay --group api --role worker \
    --harness claude --model claude-sonnet-5-5 --workspace /path/to/worktree
bus --room swarm-x state --as w1 working --doing "writing the migrations"
bus --room swarm-x usage --as w1 --tokens-in 1200 --tokens-cached 48000 --tokens-out 300 --cost 0.04
bus --room swarm-x roster [--json]
```

- `join --replay` starts the cursor at the beginning of the room, so the next `poll` or `wait` delivers every earlier
  post addressed to the member. A `__SHUTDOWN__` sent before the join is skipped; it stopped an earlier swarm.
- `state` is `working`, `waiting` or `retired`. `--doing` is the one-line status the board shows.
- `usage` adds one turn's tokens, and its cost when the harness reports it, to the member's totals. `--tokens-in`
  counts only input the model processed fresh; `--tokens-cached` counts prompt-cache reads, which re-send the same
  context on every API call and would otherwise swamp the total. The board header shows both (`1.4M tok · +9M cached`), each
  card shows `tok`, and member detail splits in, out and cached. Cache reads still count toward a plan's limits.
- `roster` lists every member, including ones whose presence has expired (`present: false`).

## Workers

`scripts/swarm-worker <member> <seat>` runs one member. It never re-joins, so a brief posted before it started is still
delivered. On each wake it:

1. sets `working` and refreshes presence every 30 seconds while the seat runs;
2. runs the seat with the new posts on stdin, resuming the member's session;
3. records tokens and cost from the seat's JSON output;
4. posts the seat's final answer for it when the seat ended without posting (codex's sandbox can't reach Redis), or
   always for agy, which can't run tools;
5. posts a `system` message to the coordinator when the seat exits non-zero;
6. sets `waiting`.

`__SHUTDOWN__`, or a `--kind done` post from a member whose roster role is `coordinator`, retires the member: the worker
sets `retired`, keeps the post's first line as `doing`, and exits. A `done` from anyone else is an ordinary post. A stop
addressed to one member retires only that member. The board marks the header `✓ finished` when the latest post passes
the same rule (`bus.ends_swarm`) and is addressed to `all`. A second worker with the same id exits with code 3.

Seats run `bus` by name, which matches the Claude seat allowlist (`Bash(bus:*)`). The worker puts this checkout first
on the seat's PATH, but a seat that runs commands in a login shell (omp) gets your shell's PATH instead, so make sure
the `bus` on your PATH is this checkout.

The worker commits its cursor after the wake, not before. A worker killed mid-wake and respawned with `swarm-spawn`
gets the same posts again.

| seat kind | first wake | later wakes |
|---|---|---|
| `claude` | `<launcher> -p --output-format json --session-id <uuid>` | `--resume <uuid>` |
| `codex` | `codex exec --json -s workspace-write` | `codex exec resume <thread>` |
| `omp` | `omp -p --mode json --session-dir <dir>` | `-c` in the same dir |
| `agy` | prompt plus a snapshot of the workspace (diff and new files), no tools | same, fresh each wake |
| `cmd` | any command; prompt on stdin, JSON usage lines on stdout | same |

## Choosing seats

`scripts/swarm-spawn --room R --as w1 --role worker --workspace DIR --brief FILE` reads the swarm config
(`$SWARM_CONFIG` or `~/.config/messagebus/swarm.yaml`; format in `examples/swarm.yaml`):

- `seats` names each harness you can run and its kind.
- `models` lists each model with an intelligence score and the seats that serve it.
- `roles` gives each role a band of scores. Keeping one band per role keeps a swarm at one level of intelligence.
  A role with `writes: false` may use read-only seats.
- `picker` is a command that prints each seat's spare quota as JSON. Inside the band, the seat with the most spare
  quota wins, and the smarter model breaks ties. A `throttled` seat is skipped.

- Each live member already in the room docks its seat's pool by `spread_penalty` (default 10). A quota reading
  doesn't change between spawns, so without this every member of a swarm would land on the same seat. Seats that
  spend one plan name the same `pool` (for example an omp seat on the same Claude login as a `claude` seat).

`--harness` and `--model` skip the picker.

## Finishing

The coordinator ends the swarm with one post:

```
bus send --from coordinator --to all --kind done "Epic 62 shipped: PR 63 and PR 64, both verified."
```

Every worker retires. The board collapses retired members to one line and marks the header `✓ finished`.

## The board

`scripts/swarm-board --room R` follows the room live: roster cards (state, harness and model, what each member is
doing, tokens and cost), the post list, and the selected post in full. Keys are listed in its footer.

## Why not hcom

[aannoo/hcom](https://github.com/aannoo/hcom) (MIT) has the same shape: spawning, `@tag` groups, a TUI and many
vendors. Its spawner and TUI are the two parts this design builds on the bus instead, so seat choice can follow a
quota picker and every session that already talks on the bus can join a swarm.
