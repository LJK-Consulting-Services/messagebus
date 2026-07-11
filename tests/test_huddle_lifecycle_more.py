import json
from datetime import datetime, timedelta, timezone
from unittest import mock


TIP = "c" * 40


def make_huddle(bus_module, *, driver="alice", participants=None):
    participants = participants or ["alice", "bob"]
    return {
        "issue": 79,
        "opener": "alice",
        "participants": participants,
        "driver": driver,
        "branch": bus_module.huddle_branch(79),
        "base": "dev",
        "base_commit": "base123",
        "session": "huddle:issue-79:session",
        "status": "open",
        "created_at": "2026-01-01T00:00:00+00:00",
    }


def test_huddle_open_join_status_signoff_unblock_and_close_success(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    status = mock.Mock()
    gh = mock.Mock(return_value=(0, "", ""))
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: [])
    monkeypatch.setattr(bus_module, "create_shared_branch", lambda *_args, **_kwargs: (0, "base123"))
    monkeypatch.setattr(bus_module, "set_status_label", status)
    monkeypatch.setattr(bus_module, "gh", gh)
    monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: TIP)

    assert bus_module.cmd_huddle_open(
        fake_redis,
        ns(as_agent="alice", issue=79, base="dev", ttl=300, allow_stale=False),
    ) == 0
    meta = json.loads(fake_redis.get(bus_module.k_huddle(79)))
    assert meta["participants"] == ["alice"]
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"

    assert bus_module.cmd_huddle_join(fake_redis, ns(as_agent="bob", issue=79)) == 0
    meta = json.loads(fake_redis.get(bus_module.k_huddle(79)))
    assert meta["participants"] == ["alice", "bob"]

    assert bus_module.cmd_huddle_status(fake_redis, ns(issue=79, json=True)) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["driver"] == "alice"

    assert bus_module.cmd_signoff(fake_redis, ns(as_agent="alice", issue=79, block=None)) == 0
    assert bus_module.cmd_signoff(fake_redis, ns(as_agent="bob", issue=79, block=None)) == 0
    assert json.loads(fake_redis.get(bus_module.k_signoff(79))) == {"alice": TIP, "bob": TIP}

    assert bus_module.cmd_signoff(fake_redis, ns(as_agent="bob", issue=79, block="needs test")) == 0
    assert bus_module.cmd_unblock(fake_redis, ns(as_agent="bob", issue=79)) == 0
    assert json.loads(fake_redis.get(bus_module.k_block(79))) == []

    assert bus_module.cmd_huddle_close(fake_redis, ns(as_agent="alice", issue=79, force=False)) == 0
    assert fake_redis.get(bus_module.k_huddle(79)) is None
    assert fake_redis.get(bus_module.k_pen(79)) is None
    assert status.call_args_list[-1].args == (79, "status:pr-open")


def test_huddle_open_branch_failure_rolls_back_lock(bus_module, fake_redis, ns, monkeypatch):
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: [])
    monkeypatch.setattr(bus_module, "create_shared_branch", lambda *_args, **_kwargs: (1, "no branch"))

    assert bus_module.cmd_huddle_open(
        fake_redis,
        ns(as_agent="alice", issue=79, base="dev", ttl=300, allow_stale=False),
    ) == 1
    assert fake_redis.get(bus_module.k_lock(79)) is None
    assert fake_redis.get(bus_module.k_huddle(79)) is None


def test_pen_status_take_force_and_deny(bus_module, fake_redis, ns, monkeypatch, capsys):
    meta = make_huddle(bus_module)
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_presence("main", "alice"), "now")

    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")

    def git(_repo, *args, check=True):
        if args == ("fetch", "origin", "huddle/issue-79"):
            return 0, "", ""
        if args == ("rev-parse", "--short", "origin/huddle/issue-79"):
            return 0, "abc123", ""
        raise AssertionError(args)

    monkeypatch.setattr(bus_module, "git", git)
    assert bus_module.cmd_pen_status(fake_redis, ns(issue=79, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["holder"] == "alice"

    assert bus_module.cmd_pen_take(
        fake_redis,
        ns(as_agent="bob", issue=79, reason="better patch"),
    ) == 0
    assert json.loads(fake_redis.get(bus_module.k_penchal(79)))["challenger"] == "bob"
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"

    assert bus_module.cmd_pen_deny(
        fake_redis,
        ns(as_agent="alice", issue=79, reason="still active"),
    ) == 0
    assert fake_redis.get(bus_module.k_penchal(79)) is None

    fake_redis.delete(bus_module.k_presence("main", "alice"))
    old = (datetime.now(timezone.utc) - timedelta(seconds=bus_module.PEN_TAKE_GRACE + 5)).isoformat()
    fake_redis.set(
        bus_module.k_penchal(79),
        json.dumps({"challenger": "bob", "reason": "stale", "ts": old}),
    )
    assert bus_module.cmd_pen_take(fake_redis, ns(as_agent="bob", issue=79, reason="stale")) == 0
    assert fake_redis.get(bus_module.k_pen(79)) == "bob"
    assert json.loads(fake_redis.get(bus_module.k_huddle(79)))["driver"] == "bob"


def test_pen_dispatch_and_unblock_empty_failure(bus_module, fake_redis, ns):
    fake_redis.set(bus_module.k_pen(79), "alice")
    assert bus_module.cmd_pen(fake_redis, ns(pen_cmd="deny", as_agent="alice", issue=79, reason="no")) == 1
    assert bus_module.cmd_unblock(fake_redis, ns(as_agent="alice", issue=79)) == 1
