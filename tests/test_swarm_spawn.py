"""End-to-end: scripts/swarm-spawn picks a seat, joins the member, posts the brief and starts its worker."""
import json
import os
import signal
import subprocess
import sys
import unittest

from test_swarm_worker import ROOT, URL, BusE2E, redis_up

SPAWN = os.path.join(ROOT, "scripts", "swarm-spawn")


@unittest.skipUnless(redis_up(), "local Redis not reachable")
class SwarmSpawnE2E(BusE2E):
    def tearDown(self):
        for pid in getattr(self, "pids", []):
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        super().tearDown()

    def spawn(self, *args, config=None):
        env = dict(self.env)
        if config:
            path = os.path.join(self.tmp, "swarm.yaml")
            with open(path, "w") as f:
                f.write(config)
            env["SWARM_CONFIG"] = path
        res = subprocess.run([sys.executable, SPAWN, "--url", URL, "--room", self.room, *args],
                             capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.pids = getattr(self, "pids", []) + [out["pid"]]
        return out

    def test_spawn_with_explicit_harness_delivers_brief(self):
        brief = os.path.join(self.tmp, "brief.md")
        with open(brief, "w") as f:
            f.write("build the parser")
        out = self.spawn("--as", "w1", "--harness", "cmd", "--model", "fake-1", "--group", "api",
                         "--role", "worker", "--brief", brief, "--from", "coord")
        self.assertEqual(out["harness"], "cmd")
        self.wait_for(lambda: self.member().get("turns") == "1")
        member = self.member()
        self.assertEqual((member["harness"], member["model"], member["role"]), ("cmd", "fake-1", "worker"))
        self.assertIn("build the parser", self.seat_calls()[0]["prompt"])
        self.assertEqual(self.r.smembers(f"bus:groups:{self.room}:w1"), {"api"})

    def test_spawn_picks_seat_by_role_band_and_headroom(self):
        picker = os.path.join(self.tmp, "picker.py")
        with open(picker, "w") as f:
            f.write('import json\nprint(json.dumps({"cmd": 12.5, "other": 3}))\n')
        config = (f"seats:\n  cmd:\n    kind: cmd\n  other:\n    kind: cmd\n"
                  f"models:\n  - {{model: smart-1, index: 55, seats: [other, cmd], effort: high}}\n"
                  f"  - {{model: dull-1, index: 30, seats: [cmd]}}\n"
                  f"roles:\n  worker: {{min: 50, max: 60}}\n"
                  f"picker: [{sys.executable}, {picker}]\n")
        out = self.spawn("--as", "w2", config=config)
        self.assertEqual((out["harness"], out["model"]), ("cmd", "smart-1"))
        self.wait_for(lambda: self.member("w2").get("state") == "waiting")

    def test_spawn_rejects_unknown_seat(self):
        res = subprocess.run([sys.executable, SPAWN, "--url", URL, "--room", self.room, "--as", "w3",
                              "--harness", "nope"], capture_output=True, text=True, env=self.env)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no seat 'nope'", res.stderr)


if __name__ == "__main__":
    unittest.main()
