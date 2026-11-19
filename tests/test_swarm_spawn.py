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

    def test_spawns_in_one_room_spread_across_seats(self):
        picker = os.path.join(self.tmp, "picker.py")
        with open(picker, "w") as f:
            f.write('import json\nprint(json.dumps({"cmd": 20, "other": 15}))\n')
        config = (f"seats:\n  cmd:\n    kind: cmd\n  other:\n    kind: cmd\n"
                  f"models:\n  - {{model: smart-1, index: 55, seats: [cmd, other]}}\n"
                  f"roles:\n  worker: {{min: 50, max: 60}}\n"
                  f"spread_penalty: 10\npicker: [{sys.executable}, {picker}]\n")
        first = self.spawn("--as", "w1", config=config)
        second = self.spawn("--as", "w2", config=config)
        self.assertEqual((first["harness"], second["harness"]), ("cmd", "other"))

    def spread_config(self):
        picker = os.path.join(self.tmp, "picker.py")
        with open(picker, "w") as f:
            f.write('import json, time\ntime.sleep(0.5)\nprint(json.dumps({"cmd": 20, "other": 15}))\n')
        return (f"seats:\n  cmd:\n    kind: cmd\n  other:\n    kind: cmd\n"
                f"models:\n  - {{model: smart-1, index: 55, seats: [cmd, other]}}\n"
                f"roles:\n  worker: {{min: 50, max: 60}}\n"
                f"spread_penalty: 10\npicker: [{sys.executable}, {picker}]\n")

    def test_concurrent_spawns_still_spread(self):
        path = os.path.join(self.tmp, "swarm.yaml")
        with open(path, "w") as f:
            f.write(self.spread_config())
        env = dict(self.env, SWARM_CONFIG=path)
        procs = [subprocess.Popen([sys.executable, SPAWN, "--url", URL, "--room", self.room, "--as", agent],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
                 for agent in ("w1", "w2")]
        outs = [json.loads(p.communicate(timeout=60)[0]) for p in procs]
        self.pids = [o["pid"] for o in outs]
        self.assertEqual(sorted(o["harness"] for o in outs), ["cmd", "other"])

    def test_respawn_keeps_its_seat_instead_of_counting_itself(self):
        config = self.spread_config()
        first = self.spawn("--as", "w1", config=config)
        os.kill(first["pid"], signal.SIGKILL)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(first["pid"])], capture_output=True).returncode)
        again = self.spawn("--as", "w1", config=config)
        self.assertEqual((first["harness"], again["harness"]), ("cmd", "cmd"))

    def test_spawn_refuses_a_member_that_is_running(self):
        self.spawn("--as", "w1", "--harness", "cmd")
        self.wait_for(lambda: self.member().get("state") == "waiting")
        cursor = self.r.get(f"bus:cursor:{self.room}:w1")
        res = subprocess.run([sys.executable, SPAWN, "--url", URL, "--room", self.room, "--as", "w1",
                              "--harness", "cmd"], capture_output=True, text=True, env=self.env, timeout=30)
        self.assertEqual(res.returncode, 3)
        self.assertIn("already running", res.stderr)
        self.assertEqual(self.r.get(f"bus:cursor:{self.room}:w1"), cursor)

    def test_respawn_after_the_worker_died_keeps_the_cursor(self):
        out = self.spawn("--as", "w1", "--harness", "cmd")
        self.bus("send", "--from", "coord", "--to", "w1", "first")
        self.wait_for(lambda: self.member().get("turns") == "1")
        os.kill(out["pid"], signal.SIGKILL)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(out["pid"])], capture_output=True).returncode)
        self.spawn("--as", "w1", "--harness", "cmd")
        self.bus("send", "--from", "coord", "--to", "w1", "second")
        self.wait_for(lambda: self.member().get("turns") == "2")
        self.assertEqual(len(self.seat_calls()), 2)
        self.assertIn("second", self.seat_calls()[1]["prompt"])
        self.assertNotIn("\nfirst\n", self.seat_calls()[1]["prompt"])

    def test_worker_killed_mid_wake_gets_the_post_again_on_respawn(self):
        out = self.spawn("--as", "w1", "--harness", "cmd")
        self.bus("send", "--from", "coord", "--to", "w1", "SLEEP then work")
        self.wait_for(lambda: len(self.seat_calls()) == 1)
        os.kill(out["pid"], signal.SIGKILL)
        self.wait_for(lambda: subprocess.run(["kill", "-0", str(out["pid"])], capture_output=True).returncode)
        self.spawn("--as", "w1", "--harness", "cmd")
        self.wait_for(lambda: len(self.seat_calls()) == 2, timeout=40)
        self.assertIn("SLEEP then work", self.seat_calls()[1]["prompt"])

    def test_spawn_rejects_unknown_seat(self):
        res = subprocess.run([sys.executable, SPAWN, "--url", URL, "--room", self.room, "--as", "w3",
                              "--harness", "nope"], capture_output=True, text=True, env=self.env)
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("no seat 'nope'", res.stderr)


if __name__ == "__main__":
    unittest.main()
