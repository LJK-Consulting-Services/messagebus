---
description: Show the message-bus board — issues, status, lock holders, presence, and recent traffic.
---

Use the `messagebus` MCP tools to report the current state of the team:

1. `bus_agents` — who is present.
2. `bus_board` — every tracked issue with its status label, lock holder, and whether that holder is present.
3. `bus_ws_list` — active worktrees (dirty / unpushed / present).
4. `bus_reap` — stale locks (holder gone).
5. `bus_tail` (n=15) — recent traffic.

If MCP is unavailable, run the equivalent `./bus --json ...` commands. Summarize for the operator: who is working on what, what's blocked or stale, and anything that needs their attention. Don't dump raw JSON — synthesize.
