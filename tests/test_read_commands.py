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


def test_prune_refuses_lagging_cursor_then_force_trims(bus_module, fake_redis, ns, capsys):
    ids = [xadd(bus_module, fake_redis, "alice", "all", f"msg-{i}") for i in range(5)]
    fake_redis.set(bus_module.k_cursor("main", "bob"), ids[0])

    assert bus_module.cmd_prune(fake_redis, ns(keep=2, force=False)) == 1
    assert fake_redis.xlen(bus_module.k_stream("main")) == 5
    assert "REFUSING" in capsys.readouterr().err

    assert bus_module.cmd_prune(fake_redis, ns(keep=2, force=True)) == 0
    assert fake_redis.xlen(bus_module.k_stream("main")) <= 3
    assert "dropping unread" in capsys.readouterr().err


def test_join_and_agents_presence(bus_module, fake_redis, ns, capsys):
    last = xadd(bus_module, fake_redis, "alice", "all", "before join")

    assert bus_module.cmd_join(fake_redis, ns(as_agent="bob", json=True)) == 0
    joined = json.loads(capsys.readouterr().out)
    assert joined["cursor"] == last
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) == last

    assert bus_module.cmd_agents(fake_redis, ns(json=True)) == 0
    assert json.loads(capsys.readouterr().out)[0]["agent"] == "bob"
