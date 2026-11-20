"""End-to-end: scripts/swarm-worker against a real local Redis, driving a fake seat.

Skips when Redis is not reachable at BUS_TEST_URL (default redis://127.0.0.1:6379/15).
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import uuid

try:
    import redis
except ImportError:  # pragma: no cover
    redis = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
BUS = os.path.join(ROOT, "bus")
WORKER = os.path.join(ROOT, "scripts", "swarm-worker")
URL = os.environ.get("BUS_TEST_URL", "redis://127.0.0.1:6379/15")

FAKE_SEAT = textwrap.dedent("""\
    import json, os, subprocess, sys
    prompt = sys.stdin.read()
    log = os.environ["FAKE_LOG"]
    with open(log, "a") as f:
        f.write(json.dumps({"session": os.environ.get("SWARM_SESSION", ""), "prompt": prompt}) + "\\n")
    if "SLEEP" in prompt:
        import time
        time.sleep(30)
    if "CRASH" in prompt:
        sys.stderr.write("boom: seat fell over\\n")
        sys.exit(7)
    if os.environ.get("SWARM_SEAT_REPLY") == "stdout":
        print("verified: looks good")
        sys.exit(0)
    if os.environ.get("FAKE_NO_POST"):
        print(json.dumps({"type": "result", "result": "final answer from the seat", "session_id": "s",
                          "usage": {"input_tokens": 1, "output_tokens": 1}}))
        sys.exit(0)
    subprocess.run([sys.executable, os.environ["BUS_BIN"], "--url", os.environ["BUS_URL"],
                    "--room", os.environ["BUS_ROOM"], "send", "--from", os.environ["SWARM_AGENT"],
                    "--to", "coord", "ack"], check=True, capture_output=True)
    print(json.dumps({"type": "result", "session_id": "sess-1", "total_cost_usd": 0.25,
                      "usage": {"input_tokens": 100, "output_tokens": 10,
                                "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0}}))
""")


def redis_up():
    """True when Redis answers at URL. With BUS_TEST_REQUIRE_REDIS set (CI), a missing Redis fails the run instead of
    silently skipping every E2E test."""
    try:
        up = redis is not None and redis.Redis.from_url(URL).ping()
    except redis.RedisError:
        up = False
    if not up and os.environ.get("BUS_TEST_REQUIRE_REDIS"):
        raise RuntimeError(f"BUS_TEST_REQUIRE_REDIS is set but Redis is not reachable at {URL}")
    return up


class BusE2E(unittest.TestCase):
    """Helpers for E2E tests on a throwaway room; no tests of its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.room = "swt-" + uuid.uuid4().hex[:8]
        self.r = redis.Redis.from_url(URL, decode_responses=True)
        self.log = os.path.join(self.tmp, "seat.log")
        seat = os.path.join(self.tmp, "fake_seat.py")
        with open(seat, "w") as f:
            f.write(FAKE_SEAT)
        self.env = dict(os.environ, BUS_URL=URL, BUS_ROOM=self.room, BUS_BIN=BUS,
                        BUS_DIR=os.path.join(self.tmp, "state"), BUS_WAIT_SECS="1",
                        SWARM_HEARTBEAT_SECS="1", FAKE_LOG=self.log,
                        SWARM_CONFIG=os.path.join(self.tmp, "no-config.yaml"),
                        SWARM_SEAT_CMD=f"{sys.executable} {seat}")
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait(timeout=5)
        for key in self.r.scan_iter(match=f"bus:*:{self.room}*"):
            self.r.delete(key)
        self.r.delete(f"bus:stream:{self.room}")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def bus(self, *args, stdin=None):
        return subprocess.run([sys.executable, BUS, "--url", URL, "--room", self.room, *args],
                              input=stdin, capture_output=True, text=True, env=self.env)

    def join_coordinator(self):
        self.bus("join", "--as", "coord", "--role", "coordinator")

    def start_worker(self, agent="w1", **env):
        self.join_coordinator()
        self.bus("join", "--as", agent, "--replay", "--role", "worker", "--harness", "cmd")
        p = subprocess.Popen([sys.executable, WORKER, agent, "cmd"], env=dict(self.env, **env),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(p)
        return p

    def wait_for(self, predicate, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.2)
        self.fail("timed out waiting")

    def posts(self):
        return [f for _, f in self.r.xrange(f"bus:stream:{self.room}")]

    def member(self, agent="w1"):
        return self.r.hgetall(f"bus:member:{self.room}:{agent}")

    def seat_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class SwarmWorkerE2E(BusE2E):
    def test_message_runs_seat_records_usage_and_returns_to_waiting(self):
        self.start_worker()
        self.bus("send", "--from", "coord", "--to", "w1", "do the thing")
        self.wait_for(lambda: any(p["body"] == "ack" for p in self.posts()))
        m = self.wait_for(lambda: self.member().get("turns") == "1" and self.member())
        self.assertEqual((m["tokens_in"], m["tokens_cached"], m["tokens_out"]), ("100", "900", "10"))
        self.assertAlmostEqual(float(m["cost_usd"]), 0.25)
        self.wait_for(lambda: self.member().get("state") == "waiting")
        self.assertIn("do the thing", self.seat_calls()[0]["prompt"])

    def test_second_wake_resumes_the_session(self):
        self.start_worker()
        self.bus("send", "--from", "coord", "--to", "w1", "one")
        self.wait_for(lambda: self.member().get("turns") == "1")
        self.bus("send", "--from", "coord", "--to", "w1", "two")
        self.wait_for(lambda: self.member().get("turns") == "2")
        self.assertEqual([c["session"] for c in self.seat_calls()], ["", "sess-1"])

    def test_brief_sent_before_worker_starts_is_delivered(self):
        self.bus("send", "--from", "coord", "--to", "@w1", "early brief")
        self.start_worker()
        self.wait_for(lambda: self.member().get("turns") == "1")
        self.assertIn("early brief", self.seat_calls()[0]["prompt"])

    def test_seat_crash_is_posted_to_the_coordinator(self):
        self.start_worker(SWARM_COORDINATOR="coord")
        self.bus("send", "--from", "coord", "--to", "w1", "CRASH please")
        post = self.wait_for(lambda: next((p for p in self.posts() if p.get("kind") == "system"), None))
        self.assertEqual(post["to"], "coord")
        self.assertIn("boom: seat fell over", post["body"])

    def test_stdout_reply_mode_posts_output_for_the_seat(self):
        self.start_worker(SWARM_SEAT_REPLY="stdout")
        self.bus("send", "--from", "coord", "--to", "w1", "verify")
        post = self.wait_for(lambda: next((p for p in self.posts() if p["from"] == "w1"), None))
        self.assertEqual(post["to"], "coord")
        self.assertIn("verified: looks good", post["body"])

    def test_shutdown_stops_the_worker(self):
        p = self.start_worker()
        self.bus("send", "--from", "coord", "--to", "all", "__SHUTDOWN__")
        self.assertEqual(p.wait(timeout=15), 0)

    def system_post(self):
        return self.wait_for(lambda: next((p for p in self.posts() if p.get("kind") == "system"), None))

    def test_seat_that_cannot_start_is_reported_and_worker_keeps_running(self):
        p = self.start_worker(SWARM_COORDINATOR="coord", SWARM_SEAT_CMD="/nonexistent/seat-binary")
        self.bus("send", "--from", "coord", "--to", "w1", "go")
        post = self.system_post()
        self.assertEqual(post["to"], "coord")
        self.assertIn("FileNotFoundError", post["body"])
        self.wait_for(lambda: self.member().get("state") == "waiting")
        self.assertIsNone(p.poll())

    def test_seat_timeout_is_reported(self):
        self.start_worker(SWARM_COORDINATOR="coord", SWARM_SEAT_TIMEOUT="1")
        self.bus("send", "--from", "coord", "--to", "w1", "SLEEP now")
        self.assertIn("timed out", self.system_post()["body"])

    def test_done_from_a_non_coordinator_does_not_retire(self):
        p = self.start_worker(SWARM_COORDINATOR="coord")
        self.bus("send", "--from", "w9", "--to", "all", "--kind", "done", "I declare victory")
        self.wait_for(lambda: self.member().get("turns") == "1")
        self.assertIsNone(p.poll())
        self.assertNotEqual(self.member()["state"], "retired")

    def test_replayed_done_from_an_earlier_swarm_is_skipped(self):
        self.join_coordinator()
        self.bus("send", "--from", "coord", "--to", "all", "--kind", "done", "old swarm over")
        self.bus("send", "--from", "coord", "--to", "@w1", "new brief")
        p = self.start_worker(SWARM_COORDINATOR="coord")
        self.wait_for(lambda: self.member().get("turns") == "1")
        self.assertIsNone(p.poll())
        self.assertIn("new brief", self.seat_calls()[0]["prompt"])

    def test_done_post_retires_the_worker(self):
        p = self.start_worker(SWARM_COORDINATOR="coord")
        self.bus("send", "--from", "coord", "--to", "all", "--kind", "done", "Epic shipped: PR 63")
        self.assertEqual(p.wait(timeout=15), 0)
        self.assertEqual(self.member()["state"], "retired")
        self.assertEqual(self.member()["doing"], "Epic shipped: PR 63")

    def test_worker_posts_final_text_when_seat_did_not_post(self):
        self.start_worker(FAKE_NO_POST="1")
        self.bus("send", "--from", "coord", "--to", "w1", "quiet please")
        post = self.wait_for(lambda: next((p for p in self.posts() if p["from"] == "w1"), None))
        self.assertEqual(post["to"], "coord")
        self.assertEqual(post["body"], "final answer from the seat")

    def test_second_worker_with_same_id_refuses(self):
        self.start_worker()
        self.wait_for(lambda: self.member().get("state") == "waiting")
        dup = subprocess.run([sys.executable, WORKER, "w1", "cmd"], env=self.env,
                             capture_output=True, text=True, timeout=15)
        self.assertEqual(dup.returncode, 3)
        self.assertIn("already running", dup.stderr)


if __name__ == "__main__":
    unittest.main()
