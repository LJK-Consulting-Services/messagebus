"""Tests for the status-transition legality gate (bus `status_transition`).

Pure-function tests: no Redis, no gh, no network. The `bus` script has no .py
extension, so we load it as a module by path. Importing it runs its top-level
`import redis` but does NOT connect (redis-py connects lazily), so these run
against just the dependency being importable.

Run:  python -m unittest discover -s tests   (from the repo root)
"""
import importlib.util
import importlib.machinery
import os
import types
import unittest
from unittest import mock

_BUS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bus")


def _load_bus():
    # `bus` has no .py extension, so name a SourceFileLoader explicitly rather
    # than relying on extension-based loader inference (which yields spec=None).
    loader = importlib.machinery.SourceFileLoader("busmod", _BUS_PATH)
    spec = importlib.util.spec_from_loader("busmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bus = _load_bus()
T = bus.status_transition

OPEN = "status:open"
CLAIMED = "status:claimed"
PR = "status:pr-open"
MERGED = "status:merged"
DEPLOYED = "status:deployed"
VERIFIED = "status:verified"


class StatusTransition(unittest.TestCase):
    def test_no_prior_state_allows_any_set(self):
        for label in bus.STATUS_LABELS:
            self.assertEqual(T(None, label), "ok", label)

    def test_foreign_or_stale_current_label_allows_any_set(self):
        self.assertEqual(T("status:bogus", OPEN), "ok")
        self.assertEqual(T("needs-triage", MERGED), "ok")

    def test_self_loop_is_noop(self):
        for label in bus.STATUS_LABELS:
            self.assertEqual(T(label, label), "noop", label)

    def test_forward_by_one_is_ok(self):
        self.assertEqual(T(OPEN, CLAIMED), "ok")
        self.assertEqual(T(CLAIMED, PR), "ok")
        self.assertEqual(T(MERGED, DEPLOYED), "ok")
        self.assertEqual(T(DEPLOYED, VERIFIED), "ok")

    def test_forward_skip_is_ok(self):
        self.assertEqual(T(OPEN, VERIFIED), "ok")
        self.assertEqual(T(OPEN, PR), "ok")
        self.assertEqual(T(CLAIMED, MERGED), "ok")

    def test_allowed_back_edges(self):
        self.assertEqual(T(CLAIMED, OPEN), "ok")   # abandon a claim
        self.assertEqual(T(PR, CLAIMED), "ok")     # PR closed -> back to work

    def test_illegal_backward_moves(self):
        self.assertEqual(T(VERIFIED, OPEN), "illegal")
        self.assertEqual(T(MERGED, CLAIMED), "illegal")
        self.assertEqual(T(DEPLOYED, PR), "illegal")
        self.assertEqual(T(PR, OPEN), "illegal")   # not one of the two back-edges

    def test_unknown_target_label_raises(self):
        with self.assertRaises(ValueError):
            T(OPEN, "status:bogus")

    def test_legal_next_set_is_nonempty_except_terminal(self):
        # mirrors how cmd_status builds its "Legal next:" hint
        for current in bus.STATUS_LABELS[:-1]:
            legal = [l for l in bus.STATUS_LABELS if T(current, l) == "ok"]
            self.assertTrue(legal, f"{current} should have a legal forward move")
        # verified is terminal: no legal onward move
        self.assertEqual([l for l in bus.STATUS_LABELS if T(VERIFIED, l) == "ok"], [])


class EffectiveStatus(unittest.TestCase):
    E = staticmethod(bus.effective_status)

    def test_none_and_empty(self):
        self.assertIsNone(self.E(None))
        self.assertIsNone(self.E([]))

    def test_ignores_foreign_labels(self):
        self.assertIsNone(self.E(["needs-triage", "bug"]))
        self.assertEqual(self.E(["bug", OPEN]), OPEN)

    def test_single_status_label(self):
        self.assertEqual(self.E([CLAIMED]), CLAIMED)

    def test_picks_furthest_along_when_multiple(self):
        # order-independent: the max pipeline stage wins, not gh's list order
        self.assertEqual(self.E([OPEN, VERIFIED]), VERIFIED)
        self.assertEqual(self.E([VERIFIED, OPEN]), VERIFIED)
        self.assertEqual(self.E([CLAIMED, PR, OPEN]), PR)

    def test_furthest_along_makes_backward_still_illegal(self):
        # the whole point: a stray extra label can't downgrade enforcement
        current = self.E([OPEN, VERIFIED])
        self.assertEqual(T(current, OPEN), "illegal")


class CmdStatus(unittest.TestCase):
    """Command-path tests: patch gh/redis-touching helpers, drive cmd_status."""

    def _args(self, **kw):
        base = dict(issue=42, set=OPEN, as_agent="a", force=False, ttl=100, room="main")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def _r(self):
        return types.SimpleNamespace(get=lambda k: None, expire=lambda k, t: None)

    def test_read_failure_refuses_even_with_force(self):
        # the regression: --force must NOT force a transition we can't read/clean
        with mock.patch.object(bus, "issue_labels", return_value=None), \
             mock.patch.object(bus, "set_status_label") as sset, \
             mock.patch.object(bus, "gh") as g:
            rc = bus.cmd_status(self._r(), self._args(set=OPEN, force=True))
        self.assertEqual(rc, 1)
        sset.assert_not_called()
        g.assert_not_called()

    def test_forward_passes_single_snapshot_to_set(self):
        with mock.patch.object(bus, "issue_labels", return_value=[CLAIMED]) as il, \
             mock.patch.object(bus, "set_status_label") as sset, \
             mock.patch.object(bus, "gh"), \
             mock.patch.object(bus, "announce"):
            rc = bus.cmd_status(self._r(), self._args(set=PR))
        self.assertEqual(rc, 0)
        self.assertEqual(il.call_count, 1)  # read exactly once
        self.assertEqual(sset.call_args.kwargs.get("current_labels"), [CLAIMED])

    def test_illegal_backward_refused_without_force(self):
        with mock.patch.object(bus, "issue_labels", return_value=[VERIFIED]), \
             mock.patch.object(bus, "set_status_label") as sset, \
             mock.patch.object(bus, "gh"):
            rc = bus.cmd_status(self._r(), self._args(set=OPEN, force=False))
        self.assertEqual(rc, 1)
        sset.assert_not_called()

    def test_illegal_backward_allowed_with_force(self):
        with mock.patch.object(bus, "issue_labels", return_value=[VERIFIED]), \
             mock.patch.object(bus, "set_status_label") as sset, \
             mock.patch.object(bus, "gh"), \
             mock.patch.object(bus, "announce"):
            rc = bus.cmd_status(self._r(), self._args(set=OPEN, force=True))
        self.assertEqual(rc, 0)
        sset.assert_called_once()

    def test_noop_skips_write(self):
        with mock.patch.object(bus, "issue_labels", return_value=[CLAIMED]), \
             mock.patch.object(bus, "set_status_label") as sset, \
             mock.patch.object(bus, "gh") as g:
            rc = bus.cmd_status(self._r(), self._args(set=CLAIMED))
        self.assertEqual(rc, 0)
        sset.assert_not_called()
        g.assert_not_called()


class CmdClaim(unittest.TestCase):
    """Acquire-path policy: a claim that backslides warns but is NOT blocked."""

    def _args(self, **kw):
        base = dict(issue=42, as_agent="a", ttl=100, room="main", worktree=False, base="dev")
        base.update(kw)
        return types.SimpleNamespace(**base)

    def _r(self):
        return types.SimpleNamespace(
            set=lambda *a, **k: True, get=lambda k: None, delete=lambda k: None)

    def test_claim_on_late_issue_warns_but_proceeds(self):
        import io
        import contextlib
        with mock.patch.object(bus, "issue_labels", return_value=[VERIFIED]), \
             mock.patch.object(bus, "set_status_label") as sset, \
             mock.patch.object(bus, "gh"), \
             mock.patch.object(bus, "announce"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = bus.cmd_claim(self._r(), self._args())
        self.assertEqual(rc, 0)                       # not blocked
        sset.assert_called_once()                     # label still written
        # the write must NOT reuse the early snapshot (read at the top, before
        # lock/worktree); set_status_label takes its own fresh read to avoid a
        # widened read->write window.
        self.assertNotIn("current_labels", sset.call_args.kwargs)
        self.assertIn("was status:verified", err.getvalue())

    def test_claim_on_open_issue_no_warning(self):
        import io
        import contextlib
        with mock.patch.object(bus, "issue_labels", return_value=[OPEN]), \
             mock.patch.object(bus, "set_status_label"), \
             mock.patch.object(bus, "gh"), \
             mock.patch.object(bus, "announce"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                rc = bus.cmd_claim(self._r(), self._args())
        self.assertEqual(rc, 0)
        self.assertNotIn("moves it back", err.getvalue())


if __name__ == "__main__":
    unittest.main()
