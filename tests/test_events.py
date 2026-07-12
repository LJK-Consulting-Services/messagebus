import json

import pytest


def jsonl(output):
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def test_redact_event_fields_keeps_scalars_and_drops_sensitive(bus_module):
    redacted = bus_module.redact_event_fields(
        {
            "issue": 79,
            "closer": "claude-2",
            "ok": True,
            "empty": None,
            "url": "https://example.com/x?token=abc",  # URL-shaped -> dropped
            "scheme": "redis://user:pw@host",           # any '://' -> dropped
            "long": "z" * 201,                          # over-length -> dropped
            "obj": {"nested": 1},                       # non-scalar -> dropped
            "seq": [1, 2],                              # non-scalar -> dropped
        }
    )
    assert redacted == {
        "issue": 79,
        "closer": "claude-2",
        "ok": True,
        "empty": None,
        "url": "<redacted>",
        "scheme": "<redacted>",
        "long": "<redacted>",
        "obj": "<redacted>",
        "seq": "<redacted>",
    }


def test_announce_event_writes_typed_envelope_and_redacts(bus_module, fake_redis):
    bus_module.announce_event(
        fake_redis, "main", "claude-2", bus_module.EV_DONEGATE_BLOCK,
        {"issue": 79, "leak": "http://secret/tok"}, topic="issue-79",
    )
    entries = fake_redis.xrange(bus_module.k_stream("main"))
    assert len(entries) == 1
    fields = entries[0][1]
    assert fields["kind"] == "event"
    assert fields["to"] == "all"
    assert fields["topic"] == "issue-79"
    payload = json.loads(fields["body"])
    assert payload["schema_version"] == bus_module.EVENT_SCHEMA_VERSION
    assert payload["event"] == bus_module.EV_DONEGATE_BLOCK
    assert payload["fields"] == {"issue": 79, "leak": "<redacted>"}
    assert "secret" not in fields["body"]


def test_announce_event_rejects_unknown_type(bus_module, fake_redis):
    with pytest.raises(ValueError):
        bus_module.announce_event(fake_redis, "main", "claude-2", "not_a_real_event", {})
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_parse_event_rejects_non_events_and_bad_json(bus_module):
    assert bus_module.parse_event({"kind": "msg", "body": "{}"}) is None
    assert bus_module.parse_event({"kind": "event", "body": "not json"}) is None
    assert bus_module.parse_event({"kind": "event", "body": "[1,2]"}) is None
    assert bus_module.parse_event({"kind": "event", "body": '{"nope": 1}'}) is None
    good = {"kind": "event", "body": '{"event": "x", "fields": {}}'}
    assert bus_module.parse_event(good) == {"event": "x", "fields": {}}


def _seed(bus_module, r):
    # human traffic that events must ignore
    r.xadd(bus_module.k_stream("main"),
           bus_module.make_fields("alice", "all", "hello", kind="msg"))
    bus_module.announce_event(r, "main", "claude-2", bus_module.EV_DONEGATE_BLOCK,
                              {"issue": 1, "open_reasons": 2})
    bus_module.announce_event(r, "main", "claude-2", bus_module.EV_DONEGATE_BLOCK,
                              {"issue": 2, "open_reasons": 3})


def test_cmd_events_lists_oldest_first_json_and_ignores_human_msgs(
    bus_module, fake_redis, ns, capsys
):
    _seed(bus_module, fake_redis)
    capsys.readouterr()

    assert bus_module.cmd_events(fake_redis, ns(n=20, type=None, json=True)) == 0
    rows = jsonl(capsys.readouterr().out)
    assert [row["event"] for row in rows] == [
        bus_module.EV_DONEGATE_BLOCK,
        bus_module.EV_DONEGATE_BLOCK,
    ]
    assert [row["fields"]["issue"] for row in rows] == [1, 2]  # oldest-first
    assert all("body" not in row for row in rows)


def test_cmd_events_type_filter_and_human_table(bus_module, fake_redis, ns, capsys):
    _seed(bus_module, fake_redis)
    capsys.readouterr()

    assert bus_module.cmd_events(
        fake_redis, ns(n=20, type=bus_module.EV_DONEGATE_BLOCK, json=False)
    ) == 0
    out = capsys.readouterr().out
    assert out.count(bus_module.EV_DONEGATE_BLOCK) == 2
    assert "open_reasons=2" in out
    assert "hello" not in out  # human message excluded


def test_cmd_events_n_caps_to_newest(bus_module, fake_redis, ns, capsys):
    _seed(bus_module, fake_redis)
    capsys.readouterr()

    assert bus_module.cmd_events(fake_redis, ns(n=1, type=None, json=True)) == 0
    rows = jsonl(capsys.readouterr().out)
    assert [row["fields"]["issue"] for row in rows] == [2]  # the single newest


def test_cmd_events_unknown_type_fails(bus_module, fake_redis, ns, capsys):
    assert bus_module.cmd_events(fake_redis, ns(n=20, type="bogus_type", json=False)) == 1
    assert "unknown --type" in capsys.readouterr().err
