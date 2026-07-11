import json
from unittest import mock


TIP = "a" * 40
OLD_TIP = "b" * 40


def huddle_meta(bus_module):
    return {
        "issue": 79,
        "opener": "alice",
        "participants": ["alice", "bob", "carol"],
        "driver": "alice",
        "branch": bus_module.huddle_branch(79),
        "base": "dev",
        "base_commit": "base",
        "session": "huddle:issue-79:session",
        "status": "open",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def test_donegate_requires_closer_floor_and_present_participant_current_tip(
    bus_module, fake_redis, monkeypatch
):
    meta = huddle_meta(bus_module)
    fake_redis.set(bus_module.k_presence("other-room", "bob"), "now")
    monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: TIP)

    ok, reasons = bus_module.donegate(fake_redis, 79, meta, "alice")

    assert not ok
    assert any("closing agent alice must sign off" in reason for reason in reasons)
    assert any("bob has not signed off" in reason for reason in reasons)
    assert not any("carol has not signed off" in reason for reason in reasons)

    fake_redis.set(bus_module.k_signoff(79), json.dumps({"alice": OLD_TIP, "bob": TIP}))
    ok, reasons = bus_module.donegate(fake_redis, 79, meta, "alice")

    assert not ok
    assert any("closing agent alice must sign off" in reason for reason in reasons)
    assert not any("bob has not signed off" in reason for reason in reasons)

    fake_redis.set(bus_module.k_signoff(79), json.dumps({"alice": TIP, "bob": OLD_TIP}))
    ok, reasons = bus_module.donegate(fake_redis, 79, meta, "alice")

    assert not ok
    assert any("bob has not signed off" in reason for reason in reasons)

    fake_redis.set(bus_module.k_signoff(79), json.dumps({"alice": TIP, "bob": TIP}))
    assert bus_module.donegate(fake_redis, 79, meta, "alice") == (True, [])


def test_huddle_close_open_block_keeps_critical_state_and_skips_status_update(
    bus_module, fake_redis, ns, monkeypatch
):
    session = "huddle:issue-79:session"
    meta = huddle_meta(bus_module)
    fake_redis.set(bus_module.k_lock(79), session)
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_signoff(79), json.dumps({"alice": TIP, "bob": TIP}))
    fake_redis.set(bus_module.k_block(79), json.dumps([{"agent": "bob", "reason": "untested"}]))
    status = mock.Mock()
    gh = mock.Mock(return_value=(0, "", ""))
    monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: TIP)
    monkeypatch.setattr(bus_module, "set_status_label", status)
    monkeypatch.setattr(bus_module, "gh", gh)

    rc = bus_module.cmd_huddle_close(
        fake_redis,
        ns(as_agent="alice", issue=79, force=False),
    )

    assert rc == 1
    assert fake_redis.get(bus_module.k_lock(79)) == session
    assert json.loads(fake_redis.get(bus_module.k_huddle(79))) == meta
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert json.loads(fake_redis.get(bus_module.k_block(79))) == [
        {"agent": "bob", "reason": "untested"}
    ]
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0
    status.assert_not_called()
    gh.assert_not_called()
