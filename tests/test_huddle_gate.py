"""Integration tests for the huddle done-gate / write-pen state machine.

Unlike the other suites (which test pure functions with no I/O), these drive the
REAL bus functions against an in-memory fakeredis, so the Redis semantics the
code actually relies on are exercised for real:

  - WATCH/MULTI compare-and-set in `_mutate_huddle_json` (lost-update safety)
  - `scan_iter` presence lookups in `_holder_present`
  - `xadd` announces

Only the two genuine external boundaries are mocked: the git shared-branch tip
(`_shared_tip`) and the gh label/comment writes (`set_status_label`, `gh`). That
keeps the gate logic under test while letting us control the "current tip" so the
anti-gaming staleness property can be checked deterministically.

Run:  python -m unittest discover -s tests   (needs redis-py + fakeredis)
"""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import types
import unittest
from unittest import mock

try:
    import fakeredis
except ImportError:  # pragma: no cover - suite skips cleanly without the dep
    fakeredis = None

if fakeredis is not None:
    # The shared double, which mirrors the bus's Lua CAS scripts in Python — on the
    # client AND on the pipeline. fakeredis only speaks EVAL when `lupa` is installed,
    # which CI does not install, so a raw FakeStrictRedis dies on the compare-and-delete
    # that `huddle close` now queues inside its MULTI (#92).
    #
    # Imported OUTSIDE the try above on purpose: folding it in would turn a broken
    # conftest into a silent "fakeredis not installed" skip of this whole suite, which is
    # the same shape of machine-dependent false green that let this file ship without
    # ever seeding k_lock.
    from conftest import BusFakeRedis

_BUS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bus")


def _load_bus():
    # `bus` has no .py extension, so name a SourceFileLoader explicitly (same
    # trick the other suites use). Importing runs its top-level `import redis`
    # but does not connect.
    loader = importlib.machinery.SourceFileLoader("busmod", _BUS_PATH)
    spec = importlib.util.spec_from_loader("busmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bus = _load_bus() if fakeredis else None

TIP1 = "1" * 40   # first shared-branch tip
TIP2 = "2" * 40   # tip after a later checkpoint (push)


def _args(**kw):
    """A stand-in for the argparse Namespace the cmd_* handlers read."""
    ns = types.SimpleNamespace(room="main", force=False, block=None)
    ns.__dict__.update(kw)
    return ns


@unittest.skipIf(fakeredis is None, "fakeredis not installed")
class HuddleGateTest(unittest.TestCase):
    ISSUE = 42

    SESSION = "huddle:issue-42:deadbeef"

    def setUp(self):
        self.r = BusFakeRedis(decode_responses=True)
        # A huddle with three participants; alice is opener/driver + pen holder.
        # (Written directly rather than via cmd_huddle_open, which would shell out
        # to git to create the shared branch — not what these tests exercise.)
        self.r.set(bus.k_huddle(self.ISSUE), json.dumps({
            "issue": self.ISSUE, "opener": "alice",
            "participants": ["alice", "bob", "carol"],
            "driver": "alice", "branch": bus.huddle_branch(self.ISSUE),
            "session": self.SESSION, "status": "open"}))
        self.r.set(bus.k_pen(self.ISSUE), "alice")
        # The session lock the close compare-and-deletes. It was never seeded here,
        # which was invisible while `compare_delete_lock` was stubbed to "we owned
        # it" — the close now runs the REAL CAS inside its transaction (#92), and an
        # absent lock is a miss, so a close that should tear down would silently not.
        self.r.set(bus.k_lock(self.ISSUE), self.SESSION)

        # The two external boundaries. `_shared_tip` reads self._tip so a test can
        # "advance" the branch mid-scenario to model a checkpoint/push.
        self._tip = TIP1
        for name, attr in (("_shared_tip", "shared_tip"),
                           ("set_status_label", "set_label"),
                           ("gh", "gh")):
            if name == "_shared_tip":
                p = mock.patch.object(bus, name, side_effect=lambda issue: self._tip)
            else:
                p = mock.patch.object(bus, name)
            setattr(self, attr, p.start())
            self.addCleanup(p.stop)
        # The cmd_* handlers print progress to stdout; keep the test output clean.
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)

    # -- helpers ----------------------------------------------------------

    def _meta(self):
        return bus._huddle_meta(self.r, self.ISSUE)

    def _present(self, *agents):
        for a in agents:
            bus.touch_presence(self.r, "main", a)

    def _signoff(self, agent):
        rc = bus.cmd_signoff(self.r, _args(issue=self.ISSUE, as_agent=agent))
        self.assertEqual(rc, 0, f"{agent} sign-off should succeed")

    def _block(self, agent, reason):
        rc = bus.cmd_signoff(self.r, _args(issue=self.ISSUE, as_agent=agent, block=reason))
        self.assertEqual(rc, 0)

    def _gate(self, closer="alice"):
        return bus.donegate(self.r, self.ISSUE, self._meta(), closer)

    # -- the happy path ---------------------------------------------------

    def test_all_present_signed_no_block_passes(self):
        self._present("alice", "bob", "carol")
        self._signoff("alice")
        self._signoff("bob")
        self._signoff("carol")
        ok, reasons = self._gate("alice")
        self.assertTrue(ok, reasons)
        self.assertEqual(reasons, [])

    def test_solo_huddle_closer_signed_passes(self):
        # A one-participant huddle closes on the closer's own sign-off.
        self.r.set(bus.k_huddle(self.ISSUE), json.dumps(
            {**self._meta(), "participants": ["alice"]}))
        self._present("alice")
        self._signoff("alice")
        ok, reasons = self._gate("alice")
        self.assertTrue(ok, reasons)

    # -- the floor: a huddle can never close with zero sign-offs ----------

    def test_closer_must_sign_off(self):
        # Everyone else signed, but the closer did not: refused.
        self._present("alice", "bob", "carol")
        self._signoff("bob")
        self._signoff("carol")
        ok, reasons = self._gate("alice")
        self.assertFalse(ok)
        self.assertTrue(any("alice" in why and "sign off" in why for why in reasons), reasons)

    def test_no_signoffs_at_all_refused(self):
        self._present("alice")
        ok, reasons = self._gate("alice")
        self.assertFalse(ok)

    # -- the anti-gaming property: sign-offs pin the tip SHA ---------------

    def test_signoff_goes_stale_after_a_checkpoint(self):
        # alice signs at TIP1, then a checkpoint pushes the branch to TIP2. Her
        # sign-off pinned TIP1, so the gate must NOT accept it against TIP2 --
        # otherwise you could sign good code then push a poison commit.
        self._present("alice")
        self._signoff("alice")           # pins TIP1
        ok, _ = self._gate("alice")
        self.assertTrue(ok)              # still good at TIP1
        self._tip = TIP2                 # someone checkpoints/pushes
        ok, reasons = self._gate("alice")
        self.assertFalse(ok, "a stale sign-off must not satisfy the gate")
        self.assertTrue(any(TIP2[:8] in why for why in reasons), reasons)

    def test_present_participant_signed_at_old_tip_refused(self):
        self._present("alice", "bob")
        self._signoff("alice")
        self._signoff("bob")
        self._tip = TIP2
        self._signoff("alice")           # alice re-signs at the new tip
        ok, reasons = self._gate("alice")
        self.assertFalse(ok)             # bob still pinned to TIP1
        self.assertTrue(any("bob" in why for why in reasons), reasons)

    # -- presence: idle != dead; absent participants don't freeze the gate -

    def test_absent_participant_does_not_freeze_the_gate(self):
        # carol never signed and is NOT present -> she does not block the close.
        self._present("alice", "bob")    # carol absent
        self._signoff("alice")
        self._signoff("bob")
        ok, reasons = self._gate("alice")
        self.assertTrue(ok, reasons)

    def test_present_participant_must_sign(self):
        # carol IS present and has not signed -> she blocks the close.
        self._present("alice", "bob", "carol")
        self._signoff("alice")
        self._signoff("bob")
        ok, reasons = self._gate("alice")
        self.assertFalse(ok)
        self.assertTrue(any("carol" in why for why in reasons), reasons)

    # -- blocks --------------------------------------------------------------

    def test_open_block_refuses_even_when_all_signed(self):
        self._present("alice", "bob", "carol")
        self._signoff("alice")
        self._signoff("bob")
        self._signoff("carol")
        self._block("carol", "the error path is untested")
        ok, reasons = self._gate("alice")
        self.assertFalse(ok)
        self.assertTrue(any("BLOCK by carol" in why for why in reasons), reasons)

    def test_unblock_lifts_the_block(self):
        self._present("alice", "bob", "carol")
        for a in ("alice", "bob", "carol"):
            self._signoff(a)
        self._block("carol", "hold on")
        self.assertFalse(self._gate("alice")[0])
        rc = bus.cmd_unblock(self.r, _args(issue=self.ISSUE, as_agent="carol"))
        self.assertEqual(rc, 0)
        self.assertTrue(self._gate("alice")[0])

    def test_unblock_without_a_block_is_an_error(self):
        rc = bus.cmd_unblock(self.r, _args(issue=self.ISSUE, as_agent="carol"))
        self.assertEqual(rc, 1)

    def test_re_blocking_replaces_not_duplicates(self):
        # A second block by the same agent updates the reason; it does not stack
        # (tests the replace-by-agent lambda inside cmd_signoff --block).
        self._block("carol", "first reason")
        self._block("carol", "second reason")
        blocks = json.loads(self.r.get(bus.k_block(self.ISSUE)))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["reason"], "second reason")

    # -- unreadable tip ------------------------------------------------------

    def test_unreadable_tip_refuses_and_still_lists_blocks(self):
        self._block("carol", "wait")
        self._tip = None                 # git rev-parse failed
        ok, reasons = self._gate("alice")
        self.assertFalse(ok)
        self.assertTrue(any("cannot read the shared branch tip" in why for why in reasons))
        self.assertTrue(any("BLOCK by carol" in why for why in reasons))

    # -- participant guard on sign-off ---------------------------------------

    def test_non_participant_cannot_sign_off(self):
        rc = bus.cmd_signoff(self.r, _args(issue=self.ISSUE, as_agent="mallory"))
        self.assertEqual(rc, 1)
        self.assertIsNone(self.r.get(bus.k_signoff(self.ISSUE)))

    # -- the gate wired through cmd_huddle_close -----------------------------

    def test_close_refused_when_gate_open_preserves_state(self):
        self._present("alice", "carol")   # carol present, unsigned
        self._signoff("alice")
        rc = bus.cmd_huddle_close(self.r, _args(issue=self.ISSUE, as_agent="alice"))
        self.assertEqual(rc, 1)
        # gate failed -> no status advance, huddle metadata still intact
        self.set_label.assert_not_called()
        self.assertIsNotNone(self._meta())

    def test_close_succeeds_when_gate_met_and_tears_down(self):
        self._present("alice", "bob")     # carol absent, so she doesn't block
        self._signoff("alice")
        self._signoff("bob")
        rc = bus.cmd_huddle_close(self.r, _args(issue=self.ISSUE, as_agent="alice"))
        self.assertEqual(rc, 0)
        self.set_label.assert_called_once_with(self.ISSUE, "status:pr-open")
        # all huddle state is cleared on close
        self.assertIsNone(self._meta())
        self.assertIsNone(self.r.get(bus.k_pen(self.ISSUE)))
        self.assertIsNone(self.r.get(bus.k_signoff(self.ISSUE)))

    def test_force_close_bypasses_an_open_gate(self):
        self._present("alice", "carol")   # gate would be open (carol unsigned)
        rc = bus.cmd_huddle_close(self.r, _args(issue=self.ISSUE, as_agent="alice", force=True))
        self.assertEqual(rc, 0)
        self.set_label.assert_called_once_with(self.ISSUE, "status:pr-open")
        self.assertIsNone(self._meta())

    def test_non_participant_cannot_close(self):
        rc = bus.cmd_huddle_close(self.r, _args(issue=self.ISSUE, as_agent="mallory"))
        self.assertEqual(rc, 1)
        self.assertIsNotNone(self._meta())   # untouched


@unittest.skipIf(fakeredis is None, "fakeredis not installed")
class MutateHuddleJsonTest(unittest.TestCase):
    """The WATCH/MULTI compare-and-set that guards block/sign-off lists.

    Every write is bound to a huddle session (#115), so each case seeds a live
    huddle first — an unbound `_mutate_huddle_json` no longer exists to test.
    """
    ISSUE = 4242
    SESSION = "huddle:issue-4242:tok"

    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)
        self.KEY = bus.k_signoff(self.ISSUE)
        self._open_huddle(self.SESSION)

    def _open_huddle(self, session):
        self.r.set(bus.k_huddle(self.ISSUE), json.dumps({
            "issue": self.ISSUE, "participants": ["agent-a"], "driver": "agent-a",
            "session": session, "status": "open"}))

    def _mutate(self, fn, default):
        """Always carries the session the huddle was opened in — the cases below
        move the WORLD (close it, reopen it under a new session), never the
        caller's token, which is exactly the in-flight write being modelled."""
        return bus._mutate_huddle_json(self.r, self.KEY, fn, default, self.ISSUE,
                                       self.SESSION)

    def test_applies_fn_to_missing_default(self):
        out = self._mutate(lambda d: {**d, "a": 1}, {})
        self.assertEqual(out, {"a": 1})
        self.assertEqual(json.loads(self.r.get(self.KEY)), {"a": 1})

    def test_composes_across_calls(self):
        self._mutate(lambda d: {**d, "a": 1}, {})
        out = self._mutate(lambda d: {**d, "b": 2}, {})
        self.assertEqual(out, {"a": 1, "b": 2})

    def test_retries_on_concurrent_write(self):
        # Force exactly one WATCH conflict: the first time fn runs, a *different*
        # client writes the key after WATCH but before EXECUTE, so redis aborts
        # the transaction and _mutate_huddle_json must retry (and see the new value).
        state = {"clashed": False}

        def fn(d):
            if not state["clashed"]:
                state["clashed"] = True
                # concurrent writer bumps the value mid-transaction
                self.r.set(self.KEY, json.dumps({"n": 100}))
            return {"n": d.get("n", 0) + 1}

        out = self._mutate(fn, {})
        # after the retry it should have seen n=100 and incremented to 101
        self.assertEqual(out, {"n": 101})
        self.assertTrue(state["clashed"])

    # ---- #115: the write is bound to the huddle session ----------------------

    def test_refuses_and_writes_nothing_when_the_huddle_is_gone(self):
        """The resurrect. Pre-fix this SET the key back into existence for a
        huddle that no longer exists — no TTL, no owner, nothing to reap it."""
        self.r.delete(bus.k_huddle(self.ISSUE))

        self.assertIsNone(self._mutate(lambda d: {**d, "a": 1}, {}))
        self.assertIsNone(self.r.get(self.KEY), "resurrected a key for a dead huddle")

    def test_refuses_a_write_in_flight_across_a_close_and_reopen(self):
        """The half that clearing-on-open cannot close: the write lands AFTER the
        next huddle already opened, so it would poison a session that never saw it."""
        self.r.delete(bus.k_huddle(self.ISSUE))
        self._open_huddle("huddle:issue-4242:NEW-session")   # a different huddle

        # a signoff still carrying the OLD session's token
        self.assertIsNone(self._mutate(lambda d: {**d, "a": 1}, {}))
        self.assertIsNone(self.r.get(self.KEY), "poisoned the next huddle on the issue")

    def test_still_writes_when_the_session_is_unchanged(self):
        """The guard must not be so strict it refuses the ordinary path — a join
        rewrites the meta blob but keeps the session, and that must still write."""
        meta = json.loads(self.r.get(bus.k_huddle(self.ISSUE)))
        meta["participants"].append("agent-b")           # a concurrent huddle join
        self.r.set(bus.k_huddle(self.ISSUE), json.dumps(meta))

        self.assertEqual(self._mutate(lambda d: {**d, "a": 1}, {}), {"a": 1})


if __name__ == "__main__":
    unittest.main()
