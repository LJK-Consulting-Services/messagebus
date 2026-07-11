import json


def xadd(bus_module, r, frm, to, body, *, kind="msg", topic="", reply_to=""):
    return r.xadd(
        bus_module.k_stream("main"),
        bus_module.make_fields(frm, to, body, kind=kind, topic=topic, reply_to=reply_to),
    )


def jsonl(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_tail_history_recent_and_terminal_escaping(bus_module, fake_redis, ns, capsys):
    xadd(bus_module, fake_redis, "alice", "all", "plain", topic="one")
    xadd(bus_module, fake_redis, "bob", "all", "bad\x01line\nok", topic="two")
    capsys.readouterr()

    assert bus_module.cmd_tail(fake_redis, ns(n=1, json=False)) == 0
    out = capsys.readouterr().out
    assert "bad\\x01line\nok" in out

    assert bus_module.cmd_history(fake_redis, ns(n=10, json=True)) == 0
    assert [msg["body"] for msg in jsonl(capsys.readouterr().out)] == [
        "plain",
        "bad\x01line\nok",
    ]

    newest = bus_module.emit_recent(fake_redis, "main", 5, True, topic="one")
    recent = jsonl(capsys.readouterr().out)
    assert [msg["topic"] for msg in recent] == ["one"]
    assert newest == fake_redis.xrevrange(bus_module.k_stream("main"), count=1)[0][0]


def test_thread_prints_root_subtree_and_missing_id_fails(bus_module, fake_redis, ns, capsys):
    root = xadd(bus_module, fake_redis, "alice", "all", "root")
    child = xadd(bus_module, fake_redis, "bob", "alice", "child", reply_to=root)
    xadd(bus_module, fake_redis, "carol", "bob", "grandchild", reply_to=child)
    xadd(bus_module, fake_redis, "mallory", "all", "unrelated")

    assert bus_module.cmd_thread(fake_redis, ns(id=child, json=True)) == 0
    assert [msg["body"] for msg in jsonl(capsys.readouterr().out)] == [
        "root",
        "child",
        "grandchild",
    ]

    assert bus_module.cmd_thread(fake_redis, ns(id="0-1", json=True)) == 1
    assert "not in room" in capsys.readouterr().err


def test_inbox_peeks_without_advancing_cursor(bus_module, fake_redis, ns, capsys):
    xadd(bus_module, fake_redis, "alice", "bob", "direct")
    xadd(bus_module, fake_redis, "carol", "all", "broadcast question", kind="question")
    xadd(bus_module, fake_redis, "dave", "alice", "other direct")
    xadd(bus_module, fake_redis, "bob", "all", "self question", kind="question")
    capsys.readouterr()

    assert bus_module.cmd_inbox(fake_redis, ns(as_agent="bob", json=True)) == 0
    assert [msg["body"] for msg in jsonl(capsys.readouterr().out)] == [
        "direct",
        "broadcast question",
    ]
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) is None

    assert bus_module.cmd_poll(fake_redis, ns(as_agent="bob", topic=None, json=True)) == 0
    assert [msg["body"] for msg in jsonl(capsys.readouterr().out)] == [
        "direct",
        "broadcast question",
    ]


def test_prune_refuses_lagging_cursor_then_force_trims(bus_module, fake_redis, ns,
                                                       events_of, capsys):
    ids = [xadd(bus_module, fake_redis, "alice", "all", f"msg-{i}") for i in range(5)]
    fake_redis.set(bus_module.k_cursor("main", "bob"), ids[0])

    assert bus_module.cmd_prune(fake_redis, ns(keep=2, force=False, dry_run=False)) == 1
    # every unread message is still there: a blocked trim deletes nothing
    assert all(fake_redis.xrange(bus_module.k_stream("main"), i, i) for i in ids)
    assert "REFUSING" in capsys.readouterr().err
    blocked = events_of(fake_redis, bus_module.EV_RETENTION_BLOCKED)
    assert len(blocked) == 1
    # the lag is machine-readable: who is behind (a count) and where the line is
    assert blocked[0]["fields"]["behind"] == 1
    assert blocked[0]["fields"]["boundary"] == ids[3]
    assert blocked[0]["fields"]["room"] == "main"

    assert bus_module.cmd_prune(fake_redis, ns(keep=2, force=True, dry_run=False)) == 0
    assert not fake_redis.xrange(bus_module.k_stream("main"), ids[0], ids[0])  # actually gone
    assert "dropped unread" in capsys.readouterr().err
    forced = events_of(fake_redis, bus_module.EV_RETENTION_FORCED)
    assert len(forced) == 1
    # 6 messages by now (5 seeded + the retention_blocked event, which rides the
    # same stream), minus keep=2. The forced event lands AFTER the trim, so the
    # trim can never delete the record of itself.
    assert forced[0]["fields"]["removed"] == 4
    assert forced[0]["fields"]["behind"] == 1


def test_prune_dry_run_reports_without_writing(bus_module, fake_redis, ns,
                                              events_of, capsys):
    ids = [xadd(bus_module, fake_redis, "alice", "all", f"msg-{i}") for i in range(5)]
    fake_redis.set(bus_module.k_cursor("main", "bob"), ids[0])
    before = fake_redis.xlen(bus_module.k_stream("main"))

    # blocked dry run: non-zero so a scheduled retention job can assert on it,
    # and NOTHING is written — not the trim, not even the blocked event.
    assert bus_module.cmd_prune(
        fake_redis, ns(keep=2, force=False, dry_run=True, json=True)) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["blocked"] is True and report["dry_run"] is True
    assert report["trimmed"] is False and report["removed"] == 0
    assert report["behind"] == [{"agent": "bob", "cursor": ids[0]}]
    assert fake_redis.xlen(bus_module.k_stream("main")) == before
    assert events_of(fake_redis, bus_module.EV_RETENTION_BLOCKED) == []

    # once nobody is behind, the dry run reports a clean trim — still writing nothing
    fake_redis.set(bus_module.k_cursor("main", "bob"), ids[4])
    assert bus_module.cmd_prune(
        fake_redis, ns(keep=2, force=False, dry_run=True, json=True)) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["blocked"] is False and report["behind"] == []
    assert report["boundary"] == ids[3] and report["trimmed"] is False
    assert fake_redis.xlen(bus_module.k_stream("main")) == before


def test_prune_never_uses_maxlen(bus_module, fake_redis, ns, capsys):
    """A MAXLEN trim drops the oldest N by count with no idea where cursors are.
    safe_trim_room must only ever trim by the cursor-checked MINID boundary."""
    calls = []
    ids = [xadd(bus_module, fake_redis, "alice", "all", f"msg-{i}") for i in range(5)]
    real_xadd = fake_redis.xadd
    fake_redis.xadd = lambda *a, **kw: calls.append(kw) or real_xadd(*a, **kw)

    res = bus_module.safe_trim_room(fake_redis, "main", 2)

    assert res["trimmed"] is True and res["removed"] == 3
    assert res["boundary"] == ids[3]
    assert all("maxlen" not in kw for kw in calls)  # no writer ever bounds by maxlen
    capsys.readouterr()


def test_join_and_agents_presence(bus_module, fake_redis, ns, capsys):
    last = xadd(bus_module, fake_redis, "alice", "all", "before join")

    assert bus_module.cmd_join(fake_redis, ns(as_agent="bob", json=True)) == 0
    joined = json.loads(capsys.readouterr().out)
    assert joined["cursor"] == last
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) == last

    assert bus_module.cmd_agents(fake_redis, ns(json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["agent"] == "bob"
