import json
import io


def send(bus_module, r, ns, *, frm="alice", to="bob", topic="", body="body"):
    return bus_module.cmd_send(
        r,
        ns(frm=frm, to=to, topic=topic, reply_to="", kind="msg", body=body),
    )


def json_lines(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_poll_exit_codes_for_empty_delivery_and_shutdown(
    bus_module, fake_redis, ns, capsys
):
    assert bus_module.cmd_poll(fake_redis, ns(as_agent="bob", topic=None, json=True)) == 10
    capsys.readouterr()

    assert send(bus_module, fake_redis, ns, topic="issue-79", body="work ready") == 0
    capsys.readouterr()

    assert bus_module.cmd_poll(fake_redis, ns(as_agent="bob", topic=None, json=True)) == 0
    delivered = json_lines(capsys.readouterr().out)
    assert [msg["body"] for msg in delivered] == ["work ready"]

    assert send(bus_module, fake_redis, ns, frm="operator", to="all", body=bus_module.SHUTDOWN) == 0
    capsys.readouterr()

    assert bus_module.cmd_poll(fake_redis, ns(as_agent="bob", topic=None, json=True)) == 11
    delivered = json_lines(capsys.readouterr().out)
    assert [msg["body"] for msg in delivered] == [bus_module.SHUTDOWN]


def test_send_reads_stdin_rejects_empty_and_prints_json(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    monkeypatch.setattr(bus_module.sys, "stdin", io.StringIO(" piped body \n"))

    assert bus_module.cmd_send(
        fake_redis,
        ns(frm="alice", to="bob", topic=None, reply_to="", kind="msg", body="-", json=True),
    ) == 0
    sent = json.loads(capsys.readouterr().out)
    assert sent["room"] == "main"
    fields = fake_redis.xrange(bus_module.k_stream("main"))[-1][1]
    assert fields["body"] == "piped body"

    assert bus_module.cmd_send(
        fake_redis,
        ns(frm="alice", to="bob", topic=None, reply_to="", kind="msg", body="", json=False),
    ) == 2
    assert "empty body" in capsys.readouterr().err


def test_topic_filter_advances_cursor_and_shutdown_bypasses_filter(
    bus_module, fake_redis, ns, capsys
):
    assert send(bus_module, fake_redis, ns, topic="other", body="skip") == 0
    assert send(bus_module, fake_redis, ns, topic="issue-79", body="deliver") == 0
    assert send(
        bus_module,
        fake_redis,
        ns,
        frm="operator",
        to="all",
        topic="other",
        body=bus_module.SHUTDOWN,
    ) == 0
    raw_ids = [msg_id for msg_id, _fields in fake_redis.xrange(bus_module.k_stream("main"))]
    capsys.readouterr()

    rc = bus_module.cmd_poll(
        fake_redis,
        ns(as_agent="bob", topic="issue-79", json=True),
    )

    assert rc == 11
    delivered = json_lines(capsys.readouterr().out)
    assert [msg["body"] for msg in delivered] == ["deliver", bus_module.SHUTDOWN]
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) == raw_ids[-1]

    assert bus_module.cmd_poll(fake_redis, ns(as_agent="bob", topic=None, json=True)) == 10


def test_topic_mismatch_is_consumed_not_redelivered_unscoped(
    bus_module, fake_redis, ns, capsys
):
    assert send(bus_module, fake_redis, ns, topic="other", body="consumed") == 0
    raw_id = fake_redis.xrange(bus_module.k_stream("main"))[-1][0]
    capsys.readouterr()

    assert bus_module.cmd_poll(
        fake_redis,
        ns(as_agent="bob", topic="issue-79", json=True),
    ) == 10
    assert fake_redis.get(bus_module.k_cursor("main", "bob")) == raw_id

    assert bus_module.cmd_poll(fake_redis, ns(as_agent="bob", topic=None, json=True)) == 10
    assert capsys.readouterr().out == ""
