"""Seat choice by intelligence band and quota headroom (scripts/swarm_config.py), plus the agy workspace snapshot."""
import importlib.machinery
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = os.path.join(os.path.dirname(__file__), os.pardir, "scripts")
sys.path.insert(0, SCRIPTS)
import swarm_config  # noqa: E402

CFG = {
    "seats": {"claude": {"kind": "claude"}, "claude-b": {"kind": "claude"}, "codex": {"kind": "codex"},
              "agy": {"kind": "agy"}},
    "models": [
        {"model": "opus", "index": 59, "seats": ["claude", "claude-b"]},
        {"model": "sonnet", "index": 55, "seats": ["claude", "claude-b"]},
        {"model": "sol", "index": 57, "seats": ["codex"], "effort": "high"},
        {"model": "neon", "index": 61, "seats": ["agy"]},
        {"model": "haiku", "index": 36, "seats": ["claude"]},
    ],
    "roles": {"worker": {"min": 54, "max": 58}, "verifier": {"min": 57, "max": 62, "writes": False}},
    "unknown_score": 0.0,
}


class ChooseTest(unittest.TestCase):
    def test_most_headroom_wins_inside_the_band(self):
        seat = swarm_config.choose(CFG, "worker", {"claude": -10, "claude-b": 16, "codex": 3})
        self.assertEqual(seat, {"harness": "claude-b", "model": "sonnet", "effort": None})

    def test_band_excludes_models_outside_it(self):
        seat = swarm_config.choose(CFG, "worker", {"claude": 50, "claude-b": -50, "codex": -50})
        self.assertEqual(seat["model"], "sonnet")

    def test_throttled_seat_is_skipped(self):
        seat = swarm_config.choose(CFG, "worker", {"claude": "throttled", "claude-b": "throttled", "codex": -20})
        self.assertEqual(seat, {"harness": "codex", "model": "sol", "effort": "high"})

    def test_read_only_seat_never_takes_a_writing_role(self):
        seat = swarm_config.choose(CFG, "worker", {"agy": 99, "claude": -1, "claude-b": -2, "codex": -3})
        self.assertNotEqual(seat["harness"], "agy")

    def test_read_only_role_may_use_agy_with_unknown_quota(self):
        seat = swarm_config.choose(CFG, "verifier", {"claude": -5, "claude-b": -5, "codex": -5, "agy": None})
        self.assertEqual(seat, {"harness": "agy", "model": "neon", "effort": None})

    def test_tie_goes_to_the_smarter_model(self):
        seat = swarm_config.choose(CFG, "verifier", {"claude": 0, "claude-b": -9, "codex": 0, "agy": "throttled"})
        self.assertEqual(seat["model"], "opus")

    def test_no_candidate_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as caught:
            swarm_config.choose(CFG, "worker", {k: "throttled" for k in ("claude", "claude-b", "codex")})
        self.assertIn("54-58", str(caught.exception))

    def test_unknown_role_is_a_clear_error(self):
        with self.assertRaises(SystemExit) as caught:
            swarm_config.choose(CFG, "monitor", {})
        self.assertIn("roles.monitor", str(caught.exception))


def load_worker():
    loader = importlib.machinery.SourceFileLoader("swarm_worker", os.path.join(SCRIPTS, "swarm-worker"))
    spec = importlib.util.spec_from_loader("swarm_worker", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class SnapshotTest(unittest.TestCase):
    def test_snapshot_has_diff_and_new_files(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as ws:
            subprocess.run(["git", "init", "-q", ws], check=True)
            with open(os.path.join(ws, "a.py"), "w") as f:
                f.write("x = 1\n")
            subprocess.run(["git", "-C", ws, "add", "a.py"], check=True)
            subprocess.run(["git", "-C", ws, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "a"],
                           check=True)
            with open(os.path.join(ws, "a.py"), "w") as f:
                f.write("x = 2\n")
            with open(os.path.join(ws, "new.py"), "w") as f:
                f.write("print('new')\n")
            snap = worker.workspace_snapshot(ws)
        self.assertIn("+x = 2", snap)
        self.assertIn("----- new.py -----\nprint('new')", snap)


if __name__ == "__main__":
    unittest.main()
