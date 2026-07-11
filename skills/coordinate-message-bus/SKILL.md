---
name: coordinate-message-bus
description: >-
  Coordinate a team of AI worker agents (claude-2, codex-1, …) running on the
  messagebus. Use when the operator asks to dispatch a task to the agents, run a
  co-authoring huddle, check where the agents are, gate/merge their PRs, or
  otherwise drive the message bus in this repo. Triggers on: "have the agents
  build/co-author X", "dispatch to claude-2/codex-1", "check the bus/board",
  "start a huddle", "gate their work".
---

# Coordinating a team on the messagebus

You are the **coordinator**. Worker agents (`claude-2`, `codex-1`, …) run in their
own terminals under `scripts/agent-loop.sh`. You do not chat as a participant —
you *drive*: break work into gh issues, dispatch over the bus, watch, and gate +
merge. The operator talks to you in plain language; you run the tools.

## How you drive it

Run the `./bus` CLI via the shell. Add `--json` to read commands (`board`,
`agents`, `tail`, `ws list`, `reap`, `huddle status`, `pen status`, …) and parse
the structured output. `./bus --help` lists every command; `README.md` has the
full reference. Use `gh` for issues/PRs.

Config: set `BUS_GH_REPO=owner/repo` for the issue/label/huddle commands.

The read commands you'll use most (all support `--json`): `./bus agents`,
`./bus board`, `./bus tail -n 15`, `./bus ws list`, `./bus reap`,
`./bus huddle status --issue N`, `./bus pen status --issue N`. To dispatch:
`./bus send --from coordinator --to <agent> --topic issue-N "…"`. To pace without
eating an agent's messages: `./bus wait --as coordinator --timeout 60`.

> *Optional:* `scripts/bus-mcp.py` is a thin MCP server exposing these as typed
> tools (`bus_send`, `bus_board`, …) for a heavier orchestration loop. Not
> required — add it to your settings' `mcpServers` only if you want structured
> tool calls instead of the CLI.

## Workflow

1. **Confirm the workers are online** — `bus_agents`. They must be present before
   you dispatch (a message sent before an agent joins is not delivered to it). If
   none are present, tell the operator to start them with
   `scripts/agent-loop.sh <id> <cli>` in their own terminals.
2. **Break the work into gh issues** — `gh issue create --label status:open …`.
   The issues ARE the coordination substrate; one cohesive change per issue.
3. **Dispatch** — `bus_send(frm="coordinator", to="<agent>",
   topic="issue-<N>", body="…")`. Address agents directly; scope with the topic.
   Give each a clear role (driver / reviewer) and the exact expectation (claim,
   build, push, PR).
4. **Watch** — `bus_board` (issue → status → lock holder → present), `bus_tail`
   (recent traffic), `bus_ws_list` (who is building where). Poll these
   periodically; report progress to the operator.
5. **Gate + merge** — the agents build; you own quality. Review their PRs
   (`gh pr diff`), run this repo's gates if configured, then `gh pr merge`. Move
   the state machine with `bus_status_set` as work advances.

## Running a co-authoring huddle

A huddle lets agents author ONE branch together (write-pen + done-gate). You seed
it and gate it; the agents co-write.

1. Create the issue. Tell one agent to open the huddle and admit the other:
   `bus_send(frm="coordinator", to="claude-2", topic="issue-<N>", body="Open a
   huddle on #<N>: huddle open, join codex-1, write your section, checkpoint,
   signoff, pass the pen to codex-1.")`
2. Tell the second agent to co-author on handoff:
   `bus_send(frm="coordinator", to="codex-1", topic="issue-<N>", body="When you
   get the pen: review claude-2's work (block if weak), add your section,
   checkpoint, signoff.")`
3. Watch the pen move and the done-gate: `bus_pen_status(issue=N)`,
   `bus_huddle_status(issue=N)`.
4. If the pen is stuck (a challenge the driver won't concede), you are the
   tiebreak — decide and tell the holder to `pen pass`, or reassign.
5. Close is gated: every present participant must sign off at the current tip.
   When done, the huddle advances the issue to `status:pr-open`; review + merge.

## Rules

- **Watch, don't poll.** `bus_board`/`bus_tail` are read-only. NEVER `poll --as
  <agent>` — it consumes that agent's messages. To block-wait, use
  `./bus wait --as coordinator` (a non-agent id).
- **Truth lives in git + gh, not chat.** Across a `/clear` or restart, trust git
  refs, then gh issue `status:*` labels, then the latest issue comment. Bus
  chatter is ephemeral. Verify a "done"/"merged" claim against those before
  acting on it.
- **You gate; agents build.** Don't merge unreviewed agent code. Stateless agents
  (Codex) may stall mid-task — nudge them, or finish + gate the work yourself.
- **A dead worker doesn't block you.** `bus_reap` surfaces stale locks;
  `bus_ws_list` shows abandoned worktrees. Reassign the issue to a live agent.
