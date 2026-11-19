"""Seat choice by intelligence band and quota headroom (scripts/swarm_config.py), plus the agy workspace snapshot."""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

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


class BandValidationTest(unittest.TestCase):
    def test_band_without_min_is_a_config_error(self):
        cfg = dict(CFG, roles={"worker": {"max": 58}})
        with self.assertRaises(SystemExit) as caught:
            swarm_config.choose(cfg, "worker", {})
        self.assertIn("numeric min and max", str(caught.exception))


def load_worker():
    loader = importlib.machinery.SourceFileLoader("swarm_worker", os.path.join(SCRIPTS, "swarm-worker"))
    spec = importlib.util.spec_from_loader("swarm_worker", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


OMP_TURN_END = {"type": "turn_end", "message": {
    "role": "assistant", "content": [{"type": "text", "text": "ok"}],
    "usage": {"input": 4, "output": 4, "cacheRead": 49533, "cacheWrite": 7245, "totalTokens": 56786,
              "cost": {"total": 0.0389346}}}}


class SeatOutputTest(unittest.TestCase):
    """Event shapes taken from real seat runs (omp `-p --mode json`, 2026-11-19)."""

    def test_omp_turn_end_usage_cost_and_text(self):
        worker = load_worker()
        lines = [{"type": "session", "id": "01a1f292"}, {"type": "message_end", "message": {"role": "user"}},
                 OMP_TURN_END, {"type": "agent_end", "messages": []}]
        out = worker.parse_output("\n".join(json.dumps(line) for line in lines))
        self.assertEqual((out["in"], out["out"], out["text"]), (56782, 4, "ok"))
        self.assertAlmostEqual(out["cost"], 0.0389346)

    def test_claude_result_still_parses(self):
        worker = load_worker()
        line = {"type": "result", "result": "done", "session_id": "s1", "total_cost_usd": 0.5,
                "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 90}}
        out = worker.parse_output(json.dumps(line))
        self.assertEqual((out["in"], out["out"], out["cost"], out["session"], out["text"]), (100, 2, 0.5, "s1", "done"))

    def test_seat_without_stdin_gets_a_closed_empty_stdin(self):
        worker = load_worker()
        seen = {}

        def fake_run(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        w = types.SimpleNamespace(seat={"kind": "omp"}, workspace=None, seat_timeout=5)
        with mock.patch.object(worker.subprocess, "run", fake_run), \
                mock.patch.dict(worker.SEATS, {"omp": lambda *_: (["omp"], None, {})}):
            worker.Worker.invoke(w, "prompt", "sid")
        self.assertEqual(seen["input"], "")

    def test_prompts_name_this_checkouts_bus(self):
        worker = load_worker()
        w = types.SimpleNamespace(agent="w1", room="r", coordinator="coordinator")
        self.assertIn(worker.BUS_PATH, worker.Worker.render(w, []))
        self.assertIn(worker.BUS_PATH, worker.Worker.system_prompt(w))
        self.assertNotIn("{BUS}", worker.Worker.system_prompt(w))


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

    def git(self, ws, *args):
        subprocess.run(["git", "-C", ws, "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                       capture_output=True)

    def test_snapshot_shows_work_committed_since_the_base(self):
        worker = load_worker()
        with tempfile.TemporaryDirectory() as ws:
            subprocess.run(["git", "init", "-q", "-b", "main", ws], check=True)
            with open(os.path.join(ws, "a.py"), "w") as f:
                f.write("x = 1\n")
            self.git(ws, "add", "a.py")
            self.git(ws, "commit", "-qm", "base")
            self.git(ws, "switch", "-qc", "feature")
            with open(os.path.join(ws, "b.py"), "w") as f:
                f.write("y = 'committed by the worker'\n")
            self.git(ws, "add", "b.py")
            self.git(ws, "commit", "-qm", "work")
            snap = worker.workspace_snapshot(ws)
        self.assertIn("commits since", snap)
        self.assertIn("+y = 'committed by the worker'", snap)

    def test_snapshot_says_when_it_is_truncated(self):
        worker = load_worker()
        text = worker.truncate("z" * (worker.SNAPSHOT_LIMIT + 10))
        self.assertIn("snapshot truncated: 10 more characters", text)


if __name__ == "__main__":
    unittest.main()
