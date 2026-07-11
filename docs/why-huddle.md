# Why the Huddle?

## The problem

Multiple AI agents pointed at one repository in one working directory collide in
two ways at once. First, the **filesystem**: two agents editing the same tree
overwrite each other's in-flight changes — while building this bus, a
coordinator edit landed on top of a driver's uncommitted work and was only
caught by a modified-file guard before it corrupted the build. Second, the
**git index**: agents sharing a branch stage and commit against the same
`.git`, so `git add`, `git commit`, and `git push` race — a branch tip gets
reset out from under an in-flight push and the commit is orphaned, reported as
"no commits between" even though the object still exists. The reflex fix,
handing every agent its own worktree and its own task, cures the collisions but
guarantees the agents never actually work *together* — it is divide-and-conquer,
one agent per task kept deliberately apart, the opposite of collaboration. What
we kept seeing was agents plucking separate tasks off a queue, never
co-authoring a single artifact. The huddle exists to close exactly that gap:
give agents a shared branch and real co-authorship without letting their edits
and commits stomp each other.

## The solution

The huddle separates the surfaces that were fighting while keeping one shared
artifact. Each agent gets a per-driver worktree, so filesystem edits happen in
private and the shared branch only changes through explicit checkpoints. The
write-pen serializes those checkpoints: one holder reviews the current tip,
adds its part, commits, and passes the pen before the next holder writes. That
makes collaboration adversarial in the useful sense, because every handoff is a
chance to block weak claims before building on them. The done-gate then requires
fresh signoffs at the final tip, so "done" means all collaborators accepted the
same commit, not that two agents separately believed their own local state was
finished.
