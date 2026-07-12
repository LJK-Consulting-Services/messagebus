"""B2 git-ref collision regression tests (issue #82).

These prove the concurrency guards added in #82 actually close the race they
claim to — every test drives real git against a throwaway bare origin, so the
git-ref semantics themselves are exercised, not mocked. No Redis server needed:
the pure-git helpers take no `r`, and the one command test (`cmd_huddle_recover`)
runs against a tiny in-memory Redis stub.

Coverage maps to the coordinator's acceptance criteria for PR #86:
  1. concurrent-push race       -> test_leased_push_*  (exactly one winner)
  2. branch-create same-SHA     -> test_create_rejects_preexisting_same_sha
  3. branch-create older-commit -> test_create_rejects_preexisting_older_commit
                                   + test_empty_lease_push_rejects_existing_ref
                                     (proves the *empty-lease* mechanism, not just
                                      the pre-B2 ls-remote guard)
  4. bus huddle recover         -> test_recover_reattaches_dangling_commit
"""

import importlib.machinery
import importlib.util
import io
import os
import pathlib
import shutil
import subprocess
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout


ROOT = pathlib.Path(__file__).resolve().parents[1]
# `bus` is extension-less; load it as a module the same way the sibling suite does.
LOADER = importlib.machinery.SourceFileLoader("bus_mod_grc", str(ROOT / "bus"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
bus = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(bus)


def run(repo, *args):
    """Run a git command in `repo` and fail loudly on error (test fixtures only)."""
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout.strip()


def head(repo):
    return run(repo, "rev-parse", "HEAD")


def remote_tip(origin_bare, ref):
    """Tip of a branch as it actually exists on the origin (source of truth)."""
    return run(origin_bare, "rev-parse", f"refs/heads/{ref}")


class FakeRedis:
    """Minimal Redis stand-in for the recover path, which only reads the pen key
    (get), refreshes presence (set with ex=), and announces (xadd)."""

    def __init__(self):
        self.data = {}
        self.streams = []

    def get(self, key):
        return self.data.get(key)

    def set(self, key, value, ex=None):
        self.data[key] = value
        return True

    def xadd(self, key, fields, *a, **k):
        self.streams.append((key, fields))
        return f"{len(self.streams)}-0"


BASE = "dev"


class GitRefCollisionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="b2test-")
        self.origin = os.path.join(self.tmp, "origin.git")
        self.main = os.path.join(self.tmp, "main")
        subprocess.run(["git", "init", "--bare", "-b", BASE, self.origin], check=True,
                       capture_output=True)
        subprocess.run(["git", "init", "-b", BASE, self.main], check=True,
                       capture_output=True)
        self._identity(self.main)
        (pathlib.Path(self.main) / "seed.txt").write_text("seed\n")
        run(self.main, "add", "-A")
        run(self.main, "commit", "-m", "seed")
        run(self.main, "remote", "add", "origin", self.origin)
        run(self.main, "push", "-u", "origin", BASE)
        self.base_commit = head(self.main)
        # Save/patch env so main_repo_dir()/worktree_root() resolve to our fixtures.
        self._saved_env = {k: os.environ.get(k)
                           for k in ("BUS_REPO_DIR", "BUS_WORKTREE_ROOT")}
        os.environ["BUS_REPO_DIR"] = self.main
        os.environ["BUS_WORKTREE_ROOT"] = os.path.join(self.tmp, "wt")

    def tearDown(self):
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _identity(self, repo):
        run(repo, "config", "user.email", "b2@test.local")
        run(repo, "config", "user.name", "B2 Test")
        run(repo, "config", "commit.gpgsign", "false")

    def _clone(self, name):
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", "-q", self.origin, path], check=True,
                       capture_output=True)
        self._identity(path)
        return path

    def _commit_on(self, repo, branch, fname, text):
        """Check out `branch` at its origin tip and add one commit; return new HEAD."""
        run(repo, "fetch", "-q", "origin", branch)
        run(repo, "checkout", "-q", "-B", branch, f"origin/{branch}")
        (pathlib.Path(repo) / fname).write_text(text)
        run(repo, "add", "-A")
        run(repo, "commit", "-q", "-m", f"add {fname}")
        return head(repo)

    # ---- (1) concurrent-push race -----------------------------------------

    def test_leased_push_exactly_one_winner(self):
        branch = "huddle/issue-901"
        rc, base = bus.create_shared_branch(self.main, BASE, branch)
        self.assertEqual(rc, 0, base)
        self.assertEqual(base, self.base_commit)

        a = self._clone("driverA")
        b = self._clone("driverB")
        head_a = self._commit_on(a, branch, "a.txt", "A\n")
        head_b = self._commit_on(b, branch, "b.txt", "B\n")

        # Both drivers synced at `base` and push leased against it: origin is the
        # single serialization point, so exactly one lease can hold.
        rc_a, sha_a, err_a = bus._leased_push(a, branch, base)
        rc_b, sha_b, err_b = bus._leased_push(b, branch, base)

        outcomes = sorted([rc_a, rc_b])
        self.assertEqual(outcomes, [0, 1], f"want one winner/one loser: {err_a} | {err_b}")

        if rc_a == 0:
            winner_head, loser_repo, loser_head, loser_err = head_a, b, head_b, err_b
        else:
            winner_head, loser_repo, loser_head, loser_err = head_b, a, head_a, err_a

        # Winner's commit is what actually landed on origin — not lost, not clobbered.
        self.assertEqual(remote_tip(self.origin, branch), winner_head)
        # Loser got a BLOCKING lease-reject (not a post-push-mismatch), and its local
        # HEAD was NOT silently reset — its work is intact for `bus huddle recover`.
        self.assertIn("another writer moved", loser_err)
        self.assertEqual(head(loser_repo), loser_head)

    # ---- (2) branch-create same-SHA race ----------------------------------

    def test_create_rejects_preexisting_same_sha(self):
        branch = "huddle/issue-902"
        # A racing writer already created the shared branch at the SAME base tip.
        run(self.main, "push", "origin", f"{self.base_commit}:refs/heads/{branch}")
        rc, msg = bus.create_shared_branch(self.main, BASE, branch)
        self.assertEqual(rc, 1)
        self.assertIn("already exists", msg)
        # No mutation: the pre-existing ref is untouched.
        self.assertEqual(remote_tip(self.origin, branch), self.base_commit)

    # ---- (3) branch-create older-commit race ------------------------------

    def test_create_rejects_preexisting_older_commit(self):
        branch = "huddle/issue-903"
        # Pre-create the branch at an OLDER commit than the current base tip.
        older = self.base_commit
        (pathlib.Path(self.main) / "advance.txt").write_text("advance\n")
        run(self.main, "add", "-A")
        run(self.main, "commit", "-m", "advance base")
        run(self.main, "push", "origin", BASE)
        self.assertNotEqual(head(self.main), older)
        run(self.main, "push", "origin", f"{older}:refs/heads/{branch}")

        rc, msg = bus.create_shared_branch(self.main, BASE, branch)
        self.assertEqual(rc, 1)
        self.assertIn("already exists", msg)
        # Still at the older commit — create refused before any mutation.
        self.assertEqual(remote_tip(self.origin, branch), older)

    def test_empty_lease_push_rejects_existing_ref(self):
        """The B2 mechanism itself: the empty-expect --force-with-lease used by
        create_shared_branch rejects when the ref already exists, *before* mutating
        it. This is the second line of defense for the ls-remote->push TOCTOU window
        (the guard tested above predates B2)."""
        branch = "huddle/issue-904"
        older = self.base_commit
        run(self.main, "push", "origin", f"{older}:refs/heads/{branch}")
        # Newer commit we would try to create the branch at.
        (pathlib.Path(self.main) / "n.txt").write_text("n\n")
        run(self.main, "add", "-A")
        run(self.main, "commit", "-m", "newer")
        newer = head(self.main)

        rc, out, err = bus.git(self.main, "push", "--porcelain",
                               f"--force-with-lease=refs/heads/{branch}:",
                               "origin", f"{newer}:refs/heads/{branch}", check=False)
        self.assertNotEqual(rc, 0, "empty-lease must reject an existing ref")
        self.assertNotIn("[new branch]", out)
        # "before mutation": the ref is still at the older commit.
        self.assertEqual(remote_tip(self.origin, branch), older)

    # ---- (4) bus huddle recover -------------------------------------------

    def test_recover_reattaches_dangling_commit(self):
        issue = 905
        agent = "claude-2"
        branch = bus.huddle_branch(issue)
        rc, base = bus.create_shared_branch(self.main, BASE, branch)
        self.assertEqual(rc, 0, base)

        r = FakeRedis()
        r.data[bus.k_pen(issue)] = agent  # only the pen holder may recover

        # Driver gets a worktree at the shared tip, commits work, but the push
        # never lands (simulated: we don't push) -> a committed-but-unpushed commit.
        path = bus.huddle_worktree(r, self.main, issue, agent)
        self.assertTrue(path and os.path.isdir(path))
        self._identity(path)
        (pathlib.Path(path) / "work.txt").write_text("dangling work\n")
        run(path, "add", "-A")
        run(path, "commit", "-q", "-m", "driver work (unpushed)")
        dangling = head(path)
        self.assertNotEqual(dangling, base)
        self.assertEqual(remote_tip(self.origin, branch), base)  # still behind

        args = types.SimpleNamespace(issue=issue, as_agent=agent, room="main")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = bus.cmd_huddle_recover(r, args)
        self.assertEqual(rc, 0)
        # The dangling commit is now attached to the shared branch on origin.
        self.assertEqual(remote_tip(self.origin, branch), dangling)

        # Idempotent: a second recover with nothing to do returns 0 cleanly.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc2 = bus.cmd_huddle_recover(r, args)
        self.assertEqual(rc2, 0)
        self.assertEqual(remote_tip(self.origin, branch), dangling)


if __name__ == "__main__":
    unittest.main()
