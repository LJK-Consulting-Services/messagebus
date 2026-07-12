import importlib.machinery
import importlib.util
import io
import json
import pathlib
import sys
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

import fakeredis


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.modules.setdefault("redis", types.SimpleNamespace(WatchError=Exception))
LOADER = importlib.machinery.SourceFileLoader("bus_mod", str(ROOT / "bus"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
bus = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(bus)


def FakeRedis():
    """fakeredis, which runs the bus's REAL Lua (the pinned dev env has `lupa`).

    This used to be a hand-rolled dict with an `eval` that ignored the script text and
    dispatched on ARITY, so a one-key script was executed as a compare-and-DELETE
    whatever it actually said. Every CAS test in this file therefore asserted the
    double's behaviour, not the bus's: corrupting CAS_DELETE_LUA or CAS_SET_META_LUA
    left the suite green (#116).
    """
    return fakeredis.FakeRedis(decode_responses=True)


class HuddleLockTest(unittest.TestCase):
    def test_unique_huddle_holder_remains_recognizable(self):
        first = bus.new_huddle_holder(27)
        second = bus.new_huddle_holder(27)

        self.assertTrue(first.startswith("huddle:issue-27:"))
        self.assertTrue(bus._is_huddle_lock(first))
        self.assertNotEqual(first, second)

    def test_metadata_holder_falls_back_for_legacy_huddles(self):
        self.assertEqual(
            bus.huddle_meta_holder({"issue": 27}, 27),
            bus.huddle_holder(27),
        )

    def test_metadata_holder_accepts_session_and_holder_keys(self):
        self.assertEqual(
            bus.huddle_meta_holder({"session": "huddle:issue-27:session"}, 27),
            "huddle:issue-27:session",
        )
        self.assertEqual(
            bus.huddle_meta_holder({"holder": "huddle:issue-27:holder"}, 27),
            "huddle:issue-27:holder",
        )

    def test_compare_set_huddle_meta_requires_exact_session_holder(self):
        r = FakeRedis()
        lock = bus.k_lock(27)
        meta = bus.k_huddle(27)
        r.set(lock, "huddle:issue-27:old")

        self.assertEqual(
            bus.compare_set_huddle_meta(r, 27, "huddle:issue-27:new", "{}"),
            0,
        )
        self.assertIsNone(r.get(meta))

        self.assertEqual(
            bus.compare_set_huddle_meta(r, 27, "huddle:issue-27:old", "{}"),
            1,
        )
        self.assertEqual(r.get(meta), "{}")

    def test_cleanup_shared_branch_refuses_moved_remote_branch(self):
        git_calls = []

        def fake_git(_main, *args, check=True):
            git_calls.append(args)
            if args[:3] == ("ls-remote", "--heads", "origin"):
                return 0, "def456\trefs/heads/huddle/issue-27", ""
            raise AssertionError(f"unexpected git call {args}")

        with mock.patch.object(bus, "valid_git_ref", return_value=True), \
             mock.patch.object(bus, "git", side_effect=fake_git):
            rc, msg = bus.cleanup_shared_branch(
                "/repo", "huddle/issue-27", "abc123",
            )

        self.assertEqual(rc, 1)
        self.assertIn("moved to def456", msg)
        self.assertEqual(len(git_calls), 1)

    def test_cleanup_shared_branch_deletes_only_with_exact_lease(self):
        git_calls = []

        def fake_git(_main, *args, check=True):
            git_calls.append(args)
            if args[:3] == ("ls-remote", "--heads", "origin"):
                return 0, "abc123\trefs/heads/huddle/issue-27", ""
            if args[0] == "push":
                return 0, "", ""
            raise AssertionError(f"unexpected git call {args}")

        with mock.patch.object(bus, "valid_git_ref", return_value=True), \
             mock.patch.object(bus, "git", side_effect=fake_git):
            rc, msg = bus.cleanup_shared_branch(
                "/repo", "huddle/issue-27", "abc123",
            )

        self.assertEqual(rc, 0)
        self.assertIn("deleted remote branch", msg)
        self.assertIn(
            "--force-with-lease=refs/heads/huddle/issue-27:abc123",
            git_calls[1],
        )

    def test_cleanup_lost_huddle_branch_leaves_active_session_branch(self):
        r = FakeRedis()
        r.set(bus.k_lock(27), "huddle:issue-27:new-owner")

        with mock.patch.object(bus, "cleanup_shared_branch") as cleanup:
            rc, msg = bus.cleanup_lost_huddle_branch(
                r, 27, "/repo", "huddle/issue-27", "abc123",
            )

        self.assertEqual(rc, 1)
        self.assertIn("left remote branch", msg)
        cleanup.assert_not_called()

    def test_cleanup_lost_huddle_branch_leaves_metadata_owned_branch(self):
        r = FakeRedis()
        r.set(bus.k_huddle(27), json.dumps({"issue": 27}))

        with mock.patch.object(bus, "cleanup_shared_branch") as cleanup:
            rc, msg = bus.cleanup_lost_huddle_branch(
                r, 27, "/repo", "huddle/issue-27", "abc123",
            )

        self.assertEqual(rc, 1)
        self.assertIn("huddle metadata now exists", msg)
        cleanup.assert_not_called()

    def test_open_lost_lock_after_branch_creation_aborts_without_deleting_new_session(self):
        r = FakeRedis()

        def create_branch(_main, _base, _branch, allow_stale=False):
            r.set(bus.k_lock(27), "huddle:issue-27:new-owner")
            return 0, "abc123"

        args = types.SimpleNamespace(
            issue=27,
            as_agent="agent-a",
            ttl=28800,
            base="dev",
            allow_stale=False,
            room="main",
        )

        with mock.patch.object(bus, "main_repo_dir", return_value="/repo"), \
             mock.patch.object(bus, "issue_labels", return_value=[]), \
             mock.patch.object(bus, "create_shared_branch", side_effect=create_branch), \
             mock.patch.object(bus, "cleanup_shared_branch") as cleanup, \
             mock.patch.object(bus, "set_status_label") as labels, \
             mock.patch.object(bus, "gh") as gh, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            rc = bus.cmd_huddle_open(r, args)

        self.assertEqual(rc, 1)
        self.assertIsNone(r.get(bus.k_huddle(27)))
        cleanup.assert_not_called()
        labels.assert_not_called()
        gh.assert_not_called()
        self.assertIn("lost lock on #27", stderr.getvalue())
        self.assertIn("left remote branch", stderr.getvalue())

    def test_open_lost_lock_after_branch_creation_cleans_orphan_branch(self):
        r = FakeRedis()
        cleanup_calls = []

        def create_branch(_main, _base, _branch, allow_stale=False):
            r.delete(bus.k_lock(27))
            return 0, "abc123"

        def cleanup(main, branch, expected_commit):
            cleanup_calls.append((main, branch, expected_commit))
            return 0, f"deleted remote branch {branch}"

        args = types.SimpleNamespace(
            issue=27,
            as_agent="agent-a",
            ttl=28800,
            base="dev",
            allow_stale=False,
            room="main",
        )

        with mock.patch.object(bus, "main_repo_dir", return_value="/repo"), \
             mock.patch.object(bus, "issue_labels", return_value=[]), \
             mock.patch.object(bus, "create_shared_branch", side_effect=create_branch), \
             mock.patch.object(bus, "cleanup_shared_branch", side_effect=cleanup), \
             mock.patch.object(bus, "set_status_label") as labels, \
             mock.patch.object(bus, "gh") as gh, \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as stderr:
            rc = bus.cmd_huddle_open(r, args)

        self.assertEqual(rc, 1)
        self.assertIsNone(r.get(bus.k_huddle(27)))
        self.assertEqual(cleanup_calls, [("/repo", "huddle/issue-27", "abc123")])
        labels.assert_not_called()
        gh.assert_not_called()
        self.assertIn("deleted remote branch", stderr.getvalue())

    def test_successful_open_records_exact_holder_in_metadata(self):
        r = FakeRedis()
        args = types.SimpleNamespace(
            issue=27,
            as_agent="agent-a",
            ttl=28800,
            base="dev",
            allow_stale=False,
            room="main",
        )

        with mock.patch.object(bus, "main_repo_dir", return_value="/repo"), \
             mock.patch.object(bus, "issue_labels", return_value=[]), \
             mock.patch.object(bus, "create_shared_branch", return_value=(0, "abc123")), \
             mock.patch.object(bus, "set_status_label"), \
             mock.patch.object(bus, "gh"), \
             mock.patch.object(bus, "announce"), \
             redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            rc = bus.cmd_huddle_open(r, args)

        self.assertEqual(rc, 0)
        meta = json.loads(r.get(bus.k_huddle(27)))
        self.assertEqual(r.get(bus.k_lock(27)), meta["session"])
        self.assertTrue(bus._is_huddle_lock(meta["session"]))


if __name__ == "__main__":
    unittest.main()
