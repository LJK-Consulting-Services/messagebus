---
description: Start a co-authoring huddle — two+ agents write one branch together, with peer review and a done-gate.
argument-hint: <issue # or task to co-author>
---

Set up a co-authoring **huddle** for: **$ARGUMENTS**

Follow the huddle section of the `coordinate-message-bus` skill:

1. Confirm the workers are present (`bus_agents`). Ensure the gh issue exists (create it if the operator gave a task, not a number).
2. Tell one agent to open + seed the huddle: `bus_send(frm="coordinator", to="claude-2", topic="issue-<N>", body="Open a huddle on #<N>: huddle open, join codex-1, write your part, checkpoint, signoff, pass the pen to codex-1.")`
3. Tell the second agent to co-author on handoff: `bus_send(frm="coordinator", to="codex-1", topic="issue-<N>", body="When you get the pen: review claude-2's work (block if weak), add your part, checkpoint, signoff.")`
4. Watch the pen and the done-gate with `bus_pen_status(issue=N)` and `bus_huddle_status(issue=N)`. Report progress.
5. You are the tiebreak for a stuck pen challenge; you own the final review + merge. Close is gated — every present participant signs off at the current tip.

If MCP is unavailable, use the equivalent `./bus` CLI commands with `--json` for reads. Report which agents are co-authoring and how to watch the pen hand off.
