---
description: Start a co-authoring huddle — two+ agents write one branch together, with peer review and a done-gate.
argument-hint: <issue # or task to co-author>
---

Set up a co-authoring **huddle** for: **$ARGUMENTS**

Follow the huddle section of the `coordinate-message-bus` skill, driving `./bus`:

1. Confirm the workers are present (`./bus agents`). Ensure the gh issue exists (create it if the operator gave a task, not a number).
2. Tell one agent to open + seed the huddle: `./bus send --from coordinator --to claude-2 --topic issue-<N> "Open a huddle on #<N>: huddle open, join codex-1, write your part, checkpoint, signoff, pass the pen to codex-1."`
3. Tell the second agent to co-author on handoff: `./bus send --from coordinator --to codex-1 --topic issue-<N> "When you get the pen: review claude-2's work (block if weak), add your part, checkpoint, signoff."`
4. Watch the pen and the done-gate: `./bus pen status --issue <N>` and `./bus huddle status --issue <N>`. Report progress.
5. You are the tiebreak for a stuck pen challenge; you own the final review + merge. Close is gated — every present participant signs off at the current tip.

Pass free-form huddle prompts as one argv value or pass `-` as the positional body and pipe stdin; never build shell strings from user text. Report which agents are co-authoring and how to watch the pen hand off.
