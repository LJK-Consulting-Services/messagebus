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


def test_huddle_open_does_not_blindly_reset_pen_after_metadata_create(
    bus_module, fake_redis, ns, monkeypatch
):
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: [])
    monkeypatch.setattr(bus_module, "create_shared_branch", lambda *_args, **_kwargs: (0, "base123"))
    monkeypatch.setattr(bus_module, "set_status_label", mock.Mock())
    monkeypatch.setattr(bus_module, "gh", mock.Mock(return_value=(0, "", "")))

    def create_meta_then_peer_takes(_r, issue, holder, meta_json, pen_holder=None):
        assert (issue, holder, pen_holder) == (79, fake_redis.get(bus_module.k_lock(79)), "alice")
        fake_redis.set(bus_module.k_huddle(79), meta_json)
        fake_redis.set(bus_module.k_pen(79), "bob")
        return 1

    monkeypatch.setattr(bus_module, "compare_set_huddle_meta", create_meta_then_peer_takes)

    assert bus_module.cmd_huddle_open(
        fake_redis,
        ns(as_agent="alice", issue=79, base="dev", ttl=300, allow_stale=False),
    ) == 0

    assert fake_redis.get(bus_module.k_pen(79)) == "bob"


def test_huddle_close_does_not_delete_reopened_session_state(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    """A close is bound to the session it started against.

    The close reads the metadata under WATCH, then tears the huddle down in the same
    transaction (#92). If the huddle is closed and REOPENED inside that window, the
    WATCH aborts our EXEC and we re-read — but what we re-read is a different huddle.
    Re-gating it and proceeding would destroy a session the caller never targeted, and
    `--force` would do it without even consulting the gate. So a session swap refuses.

    The swap is driven from inside `donegate`, which is exactly where the real window
    is (between the watched read and EXEC), so this exercises the true WATCH abort and
    retry rather than stubbing out the teardown.
    """
    old_meta = make_huddle(bus_module)
    old_meta["session"] = "huddle:issue-79:old"
    new_meta = make_huddle(bus_module)
    new_meta["session"] = "huddle:issue-79:new"
    fake_redis.set(bus_module.k_huddle(79), json.dumps(old_meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_lock(79), old_meta["session"])
    monkeypatch.setattr(bus_module, "set_status_label", mock.Mock())
    monkeypatch.setattr(bus_module, "gh", mock.Mock(return_value=(0, "", "")))

    fired = []

    def reopen_under_the_close(_r, issue, meta, _closer):
        # First gate call only: the huddle we are closing is torn down and a fresh
        # one opens behind the same key, exactly as a real close+open race would.
        if not fired:
            fired.append(True)
            assert bus_module.huddle_meta_holder(meta, issue) == old_meta["session"]
            fake_redis.set(bus_module.k_huddle(79), json.dumps(new_meta))
            fake_redis.set(bus_module.k_lock(79), new_meta["session"])
            fake_redis.set(bus_module.k_pen(79), "alice")
        return True, []

    monkeypatch.setattr(bus_module, "donegate", reopen_under_the_close)

    rc = bus_module.cmd_huddle_close(fake_redis, ns(as_agent="alice", issue=79, force=False))

    assert fired, "the concurrent reopen never landed — the race was not exercised"
    assert rc == 1
    # The new session is untouched: its lock, its pen and its metadata all survive.
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert fake_redis.get(bus_module.k_lock(79)) == new_meta["session"]
    assert json.loads(fake_redis.get(bus_module.k_huddle(79))) == new_meta
    assert "changed sessions" in capsys.readouterr().err


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
        json.dumps({"challenger": "bob", "reason": "stale", "ts": old,
                    "session": meta["session"], "driver": "alice"}),
    )
    assert bus_module.cmd_pen_take(fake_redis, ns(as_agent="bob", issue=79, reason="stale")) == 0
    assert fake_redis.get(bus_module.k_pen(79)) == "bob"
    assert json.loads(fake_redis.get(bus_module.k_huddle(79)))["driver"] == "bob"


def test_pen_dispatch_and_unblock_empty_failure(bus_module, fake_redis, ns):
    fake_redis.set(bus_module.k_pen(79), "alice")
    assert bus_module.cmd_pen(fake_redis, ns(pen_cmd="deny", as_agent="alice", issue=79, reason="no")) == 1
    assert bus_module.cmd_unblock(fake_redis, ns(as_agent="alice", issue=79)) == 1


def test_pen_deny_does_not_delete_refreshed_challenge(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    meta = make_huddle(bus_module)
    old_challenge = json.dumps({
        "challenger": "bob", "reason": "old", "ts": "2026-01-01T00:00:00+00:00",
        "session": meta["session"], "driver": "alice"})
    new_challenge = json.dumps({
        "challenger": "bob", "reason": "new", "ts": "2026-01-01T00:01:00+00:00",
        "session": meta["session"], "driver": "alice"})
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), old_challenge)
    real_get = fake_redis.get
    fired = {"done": False}

    def refresh_after_read(key, *args, **kwargs):
        value = real_get(key, *args, **kwargs)
        if key == bus_module.k_penchal(79) and not fired["done"]:
            fired["done"] = True
            fake_redis.set(bus_module.k_penchal(79), new_challenge)
        return value

    monkeypatch.setattr(fake_redis, "get", refresh_after_read)

    rc = bus_module.cmd_pen_deny(fake_redis, ns(as_agent="alice", issue=79, reason="no"))

    assert fired["done"]
    assert rc == 1
    assert fake_redis.get(bus_module.k_penchal(79)) == new_challenge
    assert "challenge changed" in capsys.readouterr().err
