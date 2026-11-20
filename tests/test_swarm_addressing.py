"""Swarm addressing, replay, member state and roster, against an in-memory fakeredis.

One test per row of the failure matrix in docs/swarm.md.
"""
import argparse
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import types
import unittest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

_BUS_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "bus")


def _load_bus():
    loader = importlib.machinery.SourceFileLoader("bus_swarm", _BUS_PATH)
    spec = importlib.util.spec_from_loader("bus_swarm", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


bus = _load_bus()
ROOM = "swarm-t"


def _args(**kw):
    ns = types.SimpleNamespace(room=ROOM, json=True, topic=None, reply_to=None,
                               kind="msg", replay=False, group=None, role=None,
                               model=None, workspace=None, conversation=None,
                               harness=None, doing=None)
    ns.__dict__.update(kw)
    return ns


class RecipientsValidatorTest(unittest.TestCase):
    def test_dedupes_and_keeps_order(self):
        self.assertEqual(bus.recipients("w1,w2,w1"), "w1,w2")

    def test_all_wins(self):
        self.assertEqual(bus.recipients("w1,all,@g"), "all")

    def test_accepts_groups(self):
        self.assertEqual(bus.recipients("@api,verifier"), "@api,verifier")

    def test_rejects_malformed(self):
        for value in ("w1,,w2", "@", "a b", "x:y", "", "@a/b"):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    bus.recipients(value)


class DeliverableTest(unittest.TestCase):
    def msg(self, to, frm="coord"):
        return {"to": to, "from": frm, "body": "hi", "topic": "", "reply_to": "", "kind": "msg"}

    def test_single_id_unchanged(self):
        self.assertTrue(bus.deliverable(self.msg("w1"), "w1"))
        self.assertFalse(bus.deliverable(self.msg("w1"), "w2"))

    def test_comma_list(self):
        self.assertTrue(bus.deliverable(self.msg("w1,w2"), "w2"))

    def test_group_member_receives(self):
        self.assertTrue(bus.deliverable(self.msg("@api"), "w1", groups={"api"}))
        self.assertFalse(bus.deliverable(self.msg("@api"), "w2", groups={"ui"}))

    def test_at_name_reaches_agent_with_that_id(self):
        self.assertTrue(bus.deliverable(self.msg("@w1"), "w1"))
        self.assertFalse(bus.deliverable(self.msg("@w1"), "w2"))

    def test_sender_in_group_skips_own_post(self):
        self.assertFalse(bus.deliverable(self.msg("@api", frm="w1"), "w1", groups={"api"}))


@unittest.skipIf(fakeredis is None, "fakeredis not installed")
class SwarmRedisTest(unittest.TestCase):
    def setUp(self):
        self.r = fakeredis.FakeStrictRedis(decode_responses=True)

    def run_cmd(self, fn, **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = fn(self.r, _args(**kw))
        return rc, out.getvalue(), err.getvalue()

    def send(self, frm, to, body="x", **kw):
        return self.run_cmd(bus.cmd_send, frm=frm, to=to, body=body, **kw)

    def poll(self, agent):
        _, out, _ = self.run_cmd(bus.cmd_poll, as_agent=agent)
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def test_send_to_empty_group_warns_but_sends(self):
        rc, _, err = self.send("coord", "@nobody")
        self.assertEqual(rc, 0)
        self.assertIn("@nobody", err)
        self.assertEqual(self.r.xlen(bus.k_stream(ROOM)), 1)

    def test_late_joiner_to_group_gets_backlog_on_replay(self):
        self.send("coord", "@api", body="brief for api")
        self.send("coord", "w9", body="not for w1")
        self.run_cmd(bus.cmd_join, as_agent="w1", replay=True, group=["api"])
        self.assertEqual([m["body"] for m in self.poll("w1")], ["brief for api"])

    def test_replay_skips_shutdown_sent_before_join(self):
        self.send("coord", "all", body=bus.SHUTDOWN)
        self.send("coord", "w1", body="new brief")
        self.run_cmd(bus.cmd_join, as_agent="w1", replay=True)
        self.send("coord", "w1", body="after join")
        bodies = [m["body"] for m in self.poll("w1")]
        self.assertEqual(bodies, ["new brief", "after join"])

    def test_replay_skips_done_sent_before_join(self):
        self.send("coord", "all", body="old swarm over", kind="done")
        self.run_cmd(bus.cmd_join, as_agent="w1", replay=True)
        self.assertEqual(self.poll("w1"), [])

    def test_shutdown_after_replay_join_still_stops(self):
        self.run_cmd(bus.cmd_join, as_agent="w1", replay=True)
        self.send("coord", "all", body=bus.SHUTDOWN)
        rc, _, _ = self.run_cmd(bus.cmd_poll, as_agent="w1")
        self.assertEqual(rc, bus.RC_SHUTDOWN)

    def test_join_without_replay_starts_at_now(self):
        self.send("coord", "all", body="old")
        self.run_cmd(bus.cmd_join, as_agent="w1")
        self.assertEqual(self.poll("w1"), [])

    def test_join_group_twice_is_idempotent(self):
        self.run_cmd(bus.cmd_join, as_agent="w1", group=["api"])
        self.run_cmd(bus.cmd_join, as_agent="w1", group=["api"])
        self.assertEqual(self.r.smembers(bus.k_groups(ROOM, "w1")), {"api"})

    def test_question_to_group_lands_in_member_inbox(self):
        self.run_cmd(bus.cmd_join, as_agent="w1", group=["api"])
        self.send("coord", "@api", body="which port?", kind="question")
        _, out, _ = self.run_cmd(bus.cmd_inbox, as_agent="w1")
        self.assertIn("which port?", out)

    def test_join_records_member_metadata(self):
        self.run_cmd(bus.cmd_join, as_agent="w1", role="worker", model="sonnet",
                     workspace="/tmp/w1", conversation="abc")
        member = self.r.hgetall(bus.k_member(ROOM, "w1"))
        self.assertEqual(member["role"], "worker")
        self.assertEqual(member["model"], "sonnet")
        self.assertEqual(member["workspace"], "/tmp/w1")
        self.assertEqual(member["conversation"], "abc")
        self.assertIn("joined", member)

    def test_state_sets_working_and_waiting(self):
        self.run_cmd(bus.cmd_state, as_agent="w1", state="working")
        self.assertEqual(self.r.hget(bus.k_member(ROOM, "w1"), "state"), "working")
        self.run_cmd(bus.cmd_state, as_agent="w1", state="waiting")
        self.assertEqual(self.r.hget(bus.k_member(ROOM, "w1"), "state"), "waiting")

    def test_state_records_what_the_agent_is_doing(self):
        self.run_cmd(bus.cmd_state, as_agent="w1", state="working", doing="writing migrations")
        self.assertEqual(self.r.hget(bus.k_member(ROOM, "w1"), "doing"), "writing migrations")

    def test_usage_accumulates_tokens_turns_and_cost(self):
        self.run_cmd(bus.cmd_usage, as_agent="w1", tokens_in=100, tokens_cached=900, tokens_out=20, cost=0.5)
        self.run_cmd(bus.cmd_usage, as_agent="w1", tokens_in=50, tokens_cached=0, tokens_out=5, cost=None)
        member = self.r.hgetall(bus.k_member(ROOM, "w1"))
        self.assertEqual((member["tokens_in"], member["tokens_cached"], member["tokens_out"], member["turns"]),
                         ("150", "900", "25", "2"))
        self.assertAlmostEqual(float(member["cost_usd"]), 0.5)

    def test_roster_shows_harness(self):
        self.run_cmd(bus.cmd_join, as_agent="w1", harness="codex", model="gpt-6-luna")
        _, out, _ = self.run_cmd(bus.cmd_roster)
        self.assertEqual(json.loads(out)[0]["harness"], "codex")

    def test_title_round_trips(self):
        self.run_cmd(bus.cmd_title, text="Epic 62")
        _, out, _ = self.run_cmd(bus.cmd_title, text=None)
        self.assertIn("Epic 62", out)

    def test_roster_lists_absent_member(self):
        self.run_cmd(bus.cmd_join, as_agent="w1", role="worker", group=["api"])
        self.r.delete(bus.k_presence(ROOM, "w1"))
        _, out, _ = self.run_cmd(bus.cmd_roster)
        roster = json.loads(out)
        self.assertEqual(roster[0]["agent"], "w1")
        self.assertFalse(roster[0]["present"])
        self.assertEqual(roster[0]["groups"], ["api"])


class ParserTest(unittest.TestCase):
    def test_state_rejects_unknown_value(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                bus.build_parser().parse_args(["state", "--as", "w1", "sleeping"])

    def test_join_accepts_swarm_flags(self):
        args = bus.build_parser().parse_args(
            ["join", "--as", "w1", "--replay", "--group", "api", "--group", "ui",
             "--role", "worker", "--model", "m", "--workspace", "/w", "--conversation", "c"])
        self.assertEqual(args.group, ["api", "ui"])
        self.assertTrue(args.replay)


if __name__ == "__main__":
    unittest.main()
