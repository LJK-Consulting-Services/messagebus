"""B3 (#83) — connection resilience, reconnect, and graceful drain.

The real kill+restart durability proof lives in test_redis_durability.py (it
needs a real redis-server). These are the hermetic parts: what connect() asks
redis-py for, that a long-lived loop rides out a ConnectionError instead of
dying, and that drain hands back exactly the state it should.
"""
import json

import pytest
import redis


def test_connect_configures_retry_without_lengthening_the_block(bus_module, monkeypatch):
    """connect() must ask for retries + health checks while KEEPING socket_timeout
    at 5s — the chunked-block invariant (BLOCK_CHUNK_MS < SOCKET_TIMEOUT) depends
    on it, and a longer socket timeout would let a single XREAD BLOCK outlive the
    client's own read deadline."""
    seen = {}

    class FakeClient:
        def ping(self):
            return True

    monkeypatch.setattr(
        bus_module.redis.Redis, "from_url",
        classmethod(lambda _cls, url, **kw: seen.update(kw, url=url) or FakeClient()),
    )

    bus_module.connect("redis://127.0.0.1:6379/0")

    assert seen["socket_timeout"] == bus_module.SOCKET_TIMEOUT == 5
    assert bus_module.BLOCK_CHUNK_MS / 1000 < bus_module.SOCKET_TIMEOUT
    assert seen["health_check_interval"] == 30
    # the redis-py exceptions, NOT the builtins — builtin ConnectionError is an
    # OSError and would never match what redis-py raises.
    assert seen["retry_on_error"] == [redis.exceptions.ConnectionError,
                                      redis.exceptions.TimeoutError]
    assert seen["retry"]._retries == 3


def test_reconnect_retries_until_the_server_returns(bus_module, monkeypatch):
    slept, attempts = [], []

    def flaky(_url):
        attempts.append(1)
        if len(attempts) < 3:
            raise redis.exceptions.ConnectionError("connection refused")
        return "live-client"

    monkeypatch.setattr(bus_module, "connect", flaky)

    got = bus_module.reconnect("redis://x", sleep=slept.append)

    assert got == "live-client"
    assert len(attempts) == 3
    assert slept == list(bus_module.RECONNECT_BACKOFF[:3])  # backs off, doesn't spin


def test_reconnect_gives_up_on_a_bounded_budget(bus_module, monkeypatch):
    """A Redis that never comes back must not hang the turn forever."""
    slept = []
    monkeypatch.setattr(bus_module, "connect", lambda _url: (_ for _ in ()).throw(
        redis.exceptions.ConnectionError("down")))

    assert bus_module.reconnect("redis://x", sleep=slept.append) is None
    assert slept == list(bus_module.RECONNECT_BACKOFF)  # every attempt, then stop


def test_wait_survives_a_redis_restart(bus_module, fake_redis, ns, events_of,
                                       monkeypatch, capsys):
    """The core B3 acceptance: an agent blocked in `bus wait` rides out a restart,
    resumes from its persisted cursor, and reports the reconnect as an event."""
    bus_module.cmd_send(fake_redis, ns(frm="alice", to="bob", topic=None,
                                       reply_to="", kind="msg", body="after the restart"))
    capsys.readouterr()

    calls = []
    real_read = bus_module.read_from_cursor

    def one_blip(r, *a, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise redis.exceptions.ConnectionError("server went away")
        return real_read(r, *a, **kw)

    monkeypatch.setattr(bus_module, "read_from_cursor", one_blip)
    # the restarted server is the same fakeredis: AOF means the cursor and the
    # stream are still there, which is exactly what the reconnect relies on.
    monkeypatch.setattr(bus_module, "connect", lambda _url: fake_redis)
    monkeypatch.setattr(bus_module.time, "sleep", lambda _s: None)

    rc = bus_module.cmd_wait(fake_redis, ns(as_agent="bob", topic=None, reply_to=None,
                                            timeout=5.0, url="redis://x"))

    assert rc == bus_module.RC_DELIVERED
    assert "after the restart" in capsys.readouterr().out  # message NOT lost
    reconnects = events_of(fake_redis, bus_module.EV_RECONNECT)
    assert len(reconnects) == 1
    assert reconnects[0]["fields"] == {"agent": "bob", "room": "main", "cmd": "wait"}
    # presence was re-registered on the far side of the restart
    assert fake_redis.get(bus_module.k_presence("main", "bob"))


def test_wait_advances_the_cursor_past_traffic_not_for_us(bus_module, fake_redis, ns, capsys):
    """The cursor must move past EVERY message seen, not just the delivered ones —
    otherwise unrelated chatter is re-read on every wake and `wait` never settles."""
    bus_module.cmd_send(fake_redis, ns(frm="alice", to="carol", topic=None,
                                       reply_to="", kind="msg", body="not for bob"))
    mine = bus_module.cmd_send(fake_redis, ns(frm="alice", to="bob", topic=None,
                                              reply_to="", kind="msg", body="for bob"))
    capsys.readouterr()
    assert mine == 0

    rc = bus_module.cmd_wait(fake_redis, ns(as_agent="bob", topic=None, reply_to=None,
                                            timeout=5.0))

    assert rc == bus_module.RC_DELIVERED
    newest = fake_redis.xrevrange(bus_module.k_stream("main"), count=1)[0][0]
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) == newest


def test_wait_delivers_even_when_the_cursor_advance_dies(bus_module, fake_redis, ns,
                                                         monkeypatch, capsys):
    """A server that dies on the cursor write must not cost us the messages we had
    already read. We deliver them and leave the cursor behind — they get re-read
    next turn (at-least-once, which this bus already is). Discarding them instead
    would silently eat a __SHUTDOWN__."""
    bus_module.cmd_send(fake_redis, ns(frm="alice", to="bob", topic=None,
                                       reply_to="", kind="msg", body="delivered once"))
    capsys.readouterr()

    cursor_key = bus_module.k_cursor("main", "bob")
    cursor_writes = []
    real_set = fake_redis.set

    def die_on_first_cursor_write(key, *a, **kw):
        # only the CURSOR write dies — touch_presence also goes through set(), and
        # it runs before the loop, outside the guard.
        if key == cursor_key:
            cursor_writes.append(key)
            if len(cursor_writes) == 1:
                raise redis.exceptions.ConnectionError("died mid-iteration")
        return real_set(key, *a, **kw)

    monkeypatch.setattr(fake_redis, "set", die_on_first_cursor_write)
    monkeypatch.setattr(bus_module, "connect", lambda _url: fake_redis)
    monkeypatch.setattr(bus_module.time, "sleep", lambda _s: None)

    rc = bus_module.cmd_wait(fake_redis, ns(as_agent="bob", topic=None, reply_to=None,
                                            timeout=5.0, url="redis://x"))

    assert rc == bus_module.RC_DELIVERED
    assert "delivered once" in capsys.readouterr().out  # NOT swallowed
    # the cursor never advanced, so the message is simply re-read next turn
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) is None


def test_wait_never_swallows_messages_when_the_cursor_write_dies(bus_module, fake_redis, ns,
                                                                 monkeypatch, capsys):
    """The cursor write LANDED server-side but its reply was lost. The cursor is
    therefore already advanced, so a re-read would skip the message — meaning if
    we discarded `delivered` here it would be gone for good. For a __SHUTDOWN__
    that is unrecoverable: the agent would never stop. Deliver what we read."""
    bus_module.cmd_send(fake_redis, ns(frm="operator", to="bob", topic=None, reply_to="",
                                       kind="msg", body=bus_module.SHUTDOWN))
    capsys.readouterr()

    cursor_key = bus_module.k_cursor("main", "bob")
    real_set = fake_redis.set

    def apply_then_lose_the_reply(key, *a, **kw):
        out = real_set(key, *a, **kw)
        if key == cursor_key:
            # the write is committed; only the ACK is lost
            raise redis.exceptions.ConnectionError("reply lost after the write landed")
        return out

    monkeypatch.setattr(fake_redis, "set", apply_then_lose_the_reply)
    monkeypatch.setattr(bus_module, "connect", lambda _url: fake_redis)
    monkeypatch.setattr(bus_module.time, "sleep", lambda _s: None)

    rc = bus_module.cmd_wait(fake_redis, ns(as_agent="bob", topic=None, reply_to=None,
                                            timeout=5.0, url="redis://x"))

    assert rc == bus_module.RC_SHUTDOWN  # NOT RC_NONE — the kill-switch got through
    assert bus_module.SHUTDOWN in capsys.readouterr().out


def test_wait_stays_inside_its_timeout_when_redis_never_returns(bus_module, fake_redis, ns,
                                                                monkeypatch, capsys):
    """`--timeout N` is a promise. reconnect() sleeps a 30s schedule, so without a
    deadline a dead Redis would hang every agent's turn ~30s past its budget."""
    monkeypatch.setattr(bus_module, "read_from_cursor", lambda *a, **kw: (_ for _ in ()).throw(
        redis.exceptions.ConnectionError("gone")))
    monkeypatch.setattr(bus_module, "connect", lambda _url: (_ for _ in ()).throw(
        redis.exceptions.ConnectionError("still gone")))

    slept = []
    monkeypatch.setattr(bus_module.time, "sleep", slept.append)

    rc = bus_module.cmd_wait(fake_redis, ns(as_agent="bob", topic=None, reply_to=None,
                                            timeout=2.0, url="redis://x"))

    assert rc == bus_module.RC_REDIS_DOWN
    # never sleeps past the caller's 2s budget, even though the schedule sums to 30s
    assert sum(slept) <= 2.0
    assert sum(bus_module.RECONNECT_BACKOFF) == 30  # the budget it would have burned


def test_reconnect_honours_a_deadline(bus_module, monkeypatch):
    monkeypatch.setattr(bus_module, "connect", lambda _url: (_ for _ in ()).throw(
        redis.exceptions.ConnectionError("down")))
    slept = []
    # a deadline already in the past: give up immediately, sleep not at all
    assert bus_module.reconnect("redis://x", sleep=slept.append,
                                deadline=bus_module.time.monotonic() - 1) is None
    assert slept == []


def test_wait_reports_a_redis_that_never_returns(bus_module, fake_redis, ns, monkeypatch, capsys):
    monkeypatch.setattr(bus_module, "read_from_cursor", lambda *a, **kw: (_ for _ in ()).throw(
        redis.exceptions.ConnectionError("gone")))
    monkeypatch.setattr(bus_module, "reconnect", lambda *a, **kw: None)

    rc = bus_module.cmd_wait(fake_redis, ns(as_agent="bob", topic=None, reply_to=None,
                                            timeout=5.0, url="redis://x"))

    assert rc == bus_module.RC_REDIS_DOWN
    assert "could not reconnect" in capsys.readouterr().err


def test_watch_resumes_from_its_local_cursor_not_from_now(bus_module, fake_redis, ns,
                                                          events_of, monkeypatch, capsys):
    """watch has no persisted cursor. On reconnect it MUST resume from the last id
    it saw — resetting to "$" would silently swallow everything published across
    the restart. It also stays an observer: no event, no presence, no cursor."""
    seen_starts, calls = [], []
    real_xread = fake_redis.xread

    def blip_once(streams, **kw):
        seen_starts.append(list(streams.values())[0])
        calls.append(1)
        if len(calls) == 2:
            raise redis.exceptions.ConnectionError("server went away")
        if len(calls) > 2:
            raise KeyboardInterrupt  # stop the follow loop
        return real_xread(streams, **kw)

    first = fake_redis.xadd(bus_module.k_stream("main"),
                            bus_module.make_fields("alice", "all", "before"))
    monkeypatch.setattr(fake_redis, "xread", blip_once)
    monkeypatch.setattr(bus_module, "connect", lambda _url: fake_redis)
    monkeypatch.setattr(bus_module.time, "sleep", lambda _s: None)

    rc = bus_module.cmd_watch(fake_redis, ns(n=1, topic=None, url="redis://x"))

    assert rc == 0
    assert seen_starts[-1] == first  # resumed at the last id seen, NOT "$"
    assert "reconnected" in capsys.readouterr().err
    assert events_of(fake_redis) == []          # observer wrote no event at all
    assert not fake_redis.get(bus_module.k_presence("main", "watch"))


def test_watch_pins_dollar_to_a_real_id_before_blocking(bus_module, fake_redis, ns, capsys):
    """`-n 0` (or an empty room) leaves `last` as "$", which each client re-resolves
    to ITS current tail. Carried across a reconnect that would skip everything
    published during the outage. Pin it to a concrete id up front."""
    existing = fake_redis.xadd(bus_module.k_stream("main"),
                               bus_module.make_fields("alice", "all", "already here"))
    starts = []
    real_xread = fake_redis.xread

    def capture(streams, **kw):
        starts.append(list(streams.values())[0])
        raise KeyboardInterrupt  # one look, then stop the follow loop

    fake_redis.xread = capture

    assert bus_module.cmd_watch(fake_redis, ns(n=0, topic=None, url="redis://x")) == 0

    assert starts == [existing]  # a real id, not "$"
    fake_redis.xread = real_xread
    capsys.readouterr()


# ---- graceful drain --------------------------------------------------------

def test_held_by_selects_only_this_agents_keys(bus_module, fake_redis):
    """Tested directly: inside cmd_drain a broken filter here is masked by the CAS
    on the pen (it would refuse to delete a peer's token anyway), so the selection
    itself has to be pinned down on its own."""
    fake_redis.set(bus_module.k_pen(44), "claude-2")
    fake_redis.set(bus_module.k_pen(45), "codex-1")
    fake_redis.set(bus_module.k_pen(9), "claude-2")

    held = bus_module._held_by(fake_redis, "bus:pen:issue:*", "claude-2")

    assert held == ["9", "44"]  # only ours, and numerically sorted ('9' before '44')


def test_drain_releases_pens_and_presence_but_keeps_claims(bus_module, fake_redis, ns,
                                                           events_of, monkeypatch, capsys):
    monkeypatch.setattr(bus_module, "_ws_meta", lambda *_a, **_kw: None)  # no worktree
    fake_redis.set(bus_module.k_pen(44), "claude-2")
    fake_redis.set(bus_module.k_pen(45), "codex-1")       # someone else's pen
    fake_redis.set(bus_module.k_lock(44), "claude-2")     # durable claim
    fake_redis.set(bus_module.k_lock(45), "codex-1")      # someone else's claim
    bus_module.touch_presence(fake_redis, "main", "claude-2")

    rc = bus_module.cmd_drain(fake_redis, ns(as_agent="claude-2", force=False, json=True))

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pens_released"] == ["44"] and out["presence_cleared"] == 1
    assert out["claims_held"] == ["44"]  # never reports (or touches) a peer's claim
    assert fake_redis.get(bus_module.k_lock(45)) == "codex-1"
    assert fake_redis.get(bus_module.k_pen(44)) is None      # peers can take it now
    assert fake_redis.get(bus_module.k_pen(45)) == "codex-1"  # never touched
    assert fake_redis.get(bus_module.k_lock(44)) == "claude-2"  # claim NOT dropped
    assert fake_redis.get(bus_module.k_presence("main", "claude-2")) is None
    assert fake_redis.get(bus_module.k_huddle(44)) is None    # huddle not closed

    # two events: one for the pen that moved, plus the always-emitted departure
    # (presence going away is itself gate-relevant, so it is never silent).
    drains = events_of(fake_redis, bus_module.EV_DRAIN)
    assert [d["fields"] for d in drains] == [
        {"agent": "claude-2", "issue": 44, "pen_released": True, "unpushed": False},
        {"agent": "claude-2", "pens_released": 1, "claims_held": 1,
         "presence_cleared": True},
    ]


def test_drain_leaves_the_pen_takeable_at_once_and_syncs_huddle_meta(
        bus_module, fake_redis, ns, monkeypatch, capsys):
    """drain's whole purpose: peers resume NOW, not after PEN_TAKE_GRACE. And the
    huddle metadata must not keep naming a driver who has left — `huddle status`
    would say `driver: claude-2` while `pen status` says nobody holds it."""
    monkeypatch.setattr(bus_module, "_ws_meta", lambda *_a, **_kw: None)
    fake_redis.set(bus_module.k_pen(44), "claude-2")
    fake_redis.set(bus_module.k_huddle(44), json.dumps({
        "issue": 44, "opener": "claude-2", "participants": ["claude-2", "codex-1"],
        "driver": "claude-2", "branch": "huddle/issue-44", "status": "open"}))
    bus_module.touch_presence(fake_redis, "main", "claude-2")

    assert bus_module.cmd_drain(fake_redis, ns(as_agent="claude-2", force=False)) == 0

    assert json.loads(fake_redis.get(bus_module.k_huddle(44)))["driver"] == ""

    # a peer takes the now-unheld pen immediately — no challenge, no 120s grace
    rc = bus_module.cmd_pen_take(fake_redis, ns(as_agent="codex-1", issue=44,
                                                reason="driver drained"))

    assert rc == 0
    assert fake_redis.get(bus_module.k_pen(44)) == "codex-1"
    assert json.loads(fake_redis.get(bus_module.k_huddle(44)))["driver"] == "codex-1"
    assert not fake_redis.get(bus_module.k_penchal(44))  # not a pending challenge
    out = capsys.readouterr().out
    assert "took the unheld pen" in out
    assert "force-take available" not in out  # the old 120s-wait path


def test_drain_is_never_silent_even_with_no_pen(bus_module, fake_redis, ns, events_of,
                                                monkeypatch, capsys):
    """Clearing presence drops the agent out of the done-gate's present-participant
    check, so a huddle can then close without its sign-off. A drain that emitted
    nothing would be a silent way to vanish from a gate."""
    monkeypatch.setattr(bus_module, "_ws_meta", lambda *_a, **_kw: None)
    bus_module.touch_presence(fake_redis, "main", "claude-2")

    assert bus_module.cmd_drain(fake_redis, ns(as_agent="claude-2", force=False,
                                               json=True)) == 0

    assert fake_redis.get(bus_module.k_presence("main", "claude-2")) is None
    drains = events_of(fake_redis, bus_module.EV_DRAIN)
    assert len(drains) == 1
    assert drains[0]["fields"] == {"agent": "claude-2", "pens_released": 0,
                                   "claims_held": 0, "presence_cleared": True}
    capsys.readouterr()


def test_drain_refuses_to_strand_unpushed_work(bus_module, fake_redis, ns, events_of,
                                               monkeypatch, capsys, tmp_path):
    """Dropping the pen while our worktree holds commits that reached no remote
    would hand the next driver a tip that is missing them. Fail closed."""
    monkeypatch.setattr(bus_module, "_ws_meta", lambda *_a, **_kw: {
        "issue": 44, "agent": "claude-2", "path": str(tmp_path),
        "branch": "huddle/issue-44", "base_commit": "abc1234"})
    monkeypatch.setattr(bus_module, "worktree_unpushed", lambda *_a: True)
    fake_redis.set(bus_module.k_pen(44), "claude-2")
    bus_module.touch_presence(fake_redis, "main", "claude-2")

    rc = bus_module.cmd_drain(fake_redis, ns(as_agent="claude-2", force=False))

    assert rc == 1
    err = capsys.readouterr().err
    assert "REFUSING" in err and "pen checkpoint" in err
    assert fake_redis.get(bus_module.k_pen(44)) == "claude-2"      # still ours
    assert fake_redis.get(bus_module.k_presence("main", "claude-2"))  # nothing drained

    # --force drops it anyway, and says so in the event
    assert bus_module.cmd_drain(fake_redis, ns(as_agent="claude-2", force=True)) == 0
    assert fake_redis.get(bus_module.k_pen(44)) is None
    drains = events_of(fake_redis, bus_module.EV_DRAIN)
    assert drains[0]["fields"]["unpushed"] is True


def test_drain_never_steals_a_pen_that_already_moved(bus_module, fake_redis, ns,
                                                     monkeypatch, capsys):
    """The pen is released by CAS. If a peer force-took it between our scan and
    our delete, the delete must be a no-op — never yank the pen out of the hands
    of whoever holds it now."""
    monkeypatch.setattr(bus_module, "_ws_meta", lambda *_a, **_kw: None)
    fake_redis.set(bus_module.k_pen(44), "claude-2")

    real_compare_delete = bus_module.compare_delete

    def steal_first(r, key, value):
        r.set(key, "codex-1")  # peer takes the pen right before our CAS lands
        return real_compare_delete(r, key, value)

    monkeypatch.setattr(bus_module, "compare_delete", steal_first)

    assert bus_module.cmd_drain(fake_redis, ns(as_agent="claude-2", force=False,
                                               json=True)) == 0

    assert fake_redis.get(bus_module.k_pen(44)) == "codex-1"  # new holder intact
    assert json.loads(capsys.readouterr().out)["pens_released"] == []


@pytest.mark.parametrize("cfg,expected", [
    ({"appendonly": "yes", "save": ""}, "aof"),
    ({"appendonly": "no", "save": "3600 1"}, "rdb"),
    ({"appendonly": "no", "save": ""}, "none"),
])
def test_redis_persistence_mode(bus_module, monkeypatch, cfg, expected):
    class Client:
        def config_get(self, *_keys):
            return cfg

    assert bus_module.redis_persistence(Client()) == expected


def test_doctor_reports_a_live_redis_that_wont_answer_config(bus_module, monkeypatch, capsys):
    """A server that is UP but refuses CONFIG is a healthy bus with an unknown
    durability mode — it must never be printed as `redis: FAIL`."""
    class Client:
        def config_get(self, *_keys):
            raise redis.exceptions.ResponseError("unknown command 'CONFIG'")

    monkeypatch.setattr(bus_module, "connect", lambda _url: Client())
    monkeypatch.setattr(bus_module, "gh", lambda *_a, **_kw: (0, "", ""))
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")

    rc = bus_module.cmd_doctor("redis://x")
    out = capsys.readouterr().out

    assert rc == 0
    assert "redis: OK" in out and "redis: FAIL" not in out
    assert "persistence: unknown" in out
