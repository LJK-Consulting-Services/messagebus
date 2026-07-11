---
description: Dispatch a task to the worker agents on the bus (break into gh issues, hand out, watch).
argument-hint: <the task to dispatch>
---

Coordinate the worker agents to do this task: **$ARGUMENTS**

Follow the `coordinate-message-bus` skill, driving the `./bus` CLI:

1. `./bus agents` — confirm the workers are present (they must be online first).
2. Break the task into one or more gh issues (`gh issue create --label status:open`), one cohesive change each. Set `BUS_GH_REPO` if not already.
3. Dispatch: `./bus send --from coordinator --to <agent> --topic issue-<N> "..."` — pass free-form bodies as one argv value or use `-` plus stdin; give each agent a clear role and exact expectation (claim --worktree, build, commit, push, open a PR, report).
4. Watch with `./bus board` / `./bus tail -n 15`; report progress.
5. When PRs land, gate them (`gh pr diff`, run gates), then merge; advance the state machine with `./bus status --as coordinator --issue <N> --set status:merged`.

Never concatenate `$ARGUMENTS` or bus message bodies into shell strings. Report back: which issues you created, who you assigned, and how to watch progress.
