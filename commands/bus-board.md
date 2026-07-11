---
description: Show the message-bus board — issues, status, lock holders, presence, and recent traffic.
---

Report the current state of the team using the `./bus` CLI (add `--json` and parse):

1. `./bus agents` — who is present.
2. `./bus board` — every tracked issue with its status label, lock holder, and whether that holder is present.
3. `./bus ws list` — active worktrees (dirty / unpushed / present).
4. `./bus reap` — stale locks (holder gone).
5. `./bus tail -n 15` — recent bus traffic.

Avoid shell-string interpolation for any untrusted text. Summarize for the operator: who is working on what, what's blocked or stale, and anything that needs their attention. Don't dump raw JSON — synthesize.
