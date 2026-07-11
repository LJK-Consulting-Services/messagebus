---
description: Dispatch a task to the worker agents on the bus (break into gh issues, hand out, watch).
argument-hint: <the task to dispatch>
---

Coordinate the worker agents to do this task: **$ARGUMENTS**

Follow the `coordinate-message-bus` skill:

1. `bus_agents` — confirm the workers are present (they must be online first).
2. Break the task into one or more gh issues (`gh issue create --label status:open`), one cohesive change each. Set `BUS_GH_REPO` if not already.
3. Dispatch with `bus_send(frm="coordinator", to="<agent>", topic="issue-<N>", ...)` — give each agent a clear role (driver vs reviewer) and the exact expectation (claim --worktree, build, commit, push, open a PR, report).
4. Watch with `bus_board` / `bus_tail`; report progress.
5. When PRs land, gate them (`gh pr diff`, run gates), then merge; advance the state machine with `bus_status_set`.

If MCP is unavailable, use the equivalent `./bus` CLI commands with `--json` for reads. Report back: which issues you created, who you assigned, and how to watch progress.
