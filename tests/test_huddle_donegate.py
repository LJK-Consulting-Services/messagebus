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


def scan_spy(fake_redis, monkeypatch):
    """Record the `match` pattern of every scan_iter the gate issues."""
    patterns = []
    inner = fake_redis.scan_iter

    def spy(match=None, **kwargs):
        patterns.append(match)
        return inner(match=match, **kwargs)

    monkeypatch.setattr(fake_redis, "scan_iter", spy)
    return patterns


def test_donegate_takes_one_presence_snapshot_not_a_scan_per_participant(
    bus_module, fake_redis, monkeypatch
):
    """#96: the gate scans the keyspace once, however many participants it has.

    Pre-fix this issued one `bus:presence:*:<agent>` scan per unsigned non-closer
    participant (2 here) — asserting the pattern list fails against the old shape.
    """
    meta = huddle_meta(bus_module)  # alice (closer), bob, carol
    fake_redis.set(bus_module.k_presence("other-room", "bob"), "now")
    fake_redis.set(bus_module.k_signoff(79), json.dumps({"alice": TIP}))
    monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: TIP)
    patterns = scan_spy(fake_redis, monkeypatch)

    ok, reasons = bus_module.donegate(fake_redis, 79, meta, "alice")

    assert patterns == ["bus:presence:*"]
    # ...and the snapshot preserves idle≠dead: present-and-unsigned bob freezes the
    # gate, absent carol does not.
    assert not ok
    assert any("bob has not signed off" in reason for reason in reasons)
    assert not any("carol has not signed off" in reason for reason in reasons)


def test_donegate_does_not_scan_when_every_participant_has_already_signed(
    bus_module, fake_redis, monkeypatch
):
    """Presence can only EXCUSE an unsigned participant, so a gate with nobody
    unsigned needs no snapshot at all — the passing path scans zero times."""
    meta = huddle_meta(bus_module)  # alice (closer), bob, carol
    fake_redis.set(bus_module.k_presence("other-room", "bob"), "now")
    fake_redis.set(
        bus_module.k_signoff(79), json.dumps({"alice": TIP, "bob": TIP, "carol": TIP})
    )
    monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: TIP)
    patterns = scan_spy(fake_redis, monkeypatch)

    assert bus_module.donegate(fake_redis, 79, meta, "alice") == (True, [])
    assert patterns == []


def test_donegate_skips_the_presence_scan_when_the_closer_is_the_only_participant(
    bus_module, fake_redis, monkeypatch
):
    """A solo huddle has nobody else to check — so it must not scan at all."""
    meta = huddle_meta(bus_module) | {"participants": ["alice"]}
    fake_redis.set(bus_module.k_signoff(79), json.dumps({"alice": TIP}))
    monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: TIP)
    patterns = scan_spy(fake_redis, monkeypatch)

    assert bus_module.donegate(fake_redis, 79, meta, "alice") == (True, [])
    assert patterns == []


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
    # B6 (#81): a blocked close emits exactly one structured donegate_block event
    # carrying the COUNT of open reasons — never the raw block reason text.
    events = fake_redis.xrange(bus_module.k_stream("main"))
    assert len(events) == 1
    payload = json.loads(events[0][1]["body"])
    assert events[0][1]["kind"] == "event"
    assert payload["event"] == bus_module.EV_DONEGATE_BLOCK
    assert payload["schema_version"] == bus_module.EVENT_SCHEMA_VERSION
    assert payload["fields"] == {"issue": 79, "closer": "alice", "open_reasons": 1}
    assert "untested" not in events[0][1]["body"]  # raw block reason never leaks
    status.assert_not_called()
    gh.assert_not_called()
