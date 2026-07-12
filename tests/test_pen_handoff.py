import json
from datetime import datetime, timedelta, timezone

import pytest


def huddle_meta(bus_module, driver="alice"):
    return {
        "issue": 79,
        "opener": "alice",
        "participants": ["alice", "bob"],
        "driver": driver,
        "branch": bus_module.huddle_branch(79),
        "base": "dev",
        "base_commit": "base",
        "session": "huddle:issue-79:session",
        "status": "open",
    }


def test_pen_pass_aborts_and_keeps_pen_when_checkpoint_fails(
    bus_module, fake_redis, ns, monkeypatch
):
    meta = huddle_meta(bus_module)
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), json.dumps({"challenger": "bob"}))
    monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", lambda *_args, **_kwargs: 1)

    rc = bus_module.cmd_pen_pass(
        fake_redis,
        ns(as_agent="alice", issue=79, to="bob"),
    )

    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert json.loads(fake_redis.get(bus_module.k_huddle(79))) == meta
    assert json.loads(fake_redis.get(bus_module.k_penchal(79))) == {"challenger": "bob"}
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_checkpoint_push_failure_keeps_pen_and_emits_no_checkpoint(
    bus_module, fake_redis, ns, monkeypatch
):
    fake_redis.set(bus_module.k_pen(79), "alice")
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "huddle_worktree", lambda *_args: "/worktree")

    def git(repo, *args, check=True):
        # B2 (#82): _synced_tip resolves the leased-against tip before staging.
        if repo == "/worktree" and args == ("rev-parse", "--verify", "origin/huddle/issue-79"):
            return 0, "base", ""
        if repo == "/worktree" and args == ("add", "-A"):
            return 0, "", ""
        if repo == "/worktree" and args == ("diff", "--cached", "--quiet"):
            return 1, "", ""
        if repo == "/worktree" and args[:4] == ("-c", "user.email=alice@bus", "-c", "user.name=alice"):
            return 0, "", ""
        if repo == "/worktree" and args == (
            "merge-base",
            "--is-ancestor",
            "HEAD",
            "origin/huddle/issue-79",
        ):
            return 1, "", ""
        # _leased_push: resolve HEAD, confirm it descends from the synced tip, then the
        # leased push itself is rejected by a racing writer.
        if repo == "/worktree" and args == ("rev-parse", "HEAD"):
            return 0, "headsha", ""
        if repo == "/worktree" and args == ("merge-base", "--is-ancestor", "base", "HEAD"):
            return 0, "", ""
        if repo == "/worktree" and args == (
            "push",
            "--porcelain",
            "--force-with-lease=refs/heads/huddle/issue-79:base",
            "origin",
            "HEAD:huddle/issue-79",
        ):
            return 1, "", "rejected"
        raise AssertionError(f"unexpected git call: {repo} {args}")

    monkeypatch.setattr(bus_module, "git", git)

    rc = bus_module.cmd_pen_checkpoint(fake_redis, ns(as_agent="alice", issue=79))

    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_checkpoint_guard_and_noop_paths(bus_module, fake_redis, ns, monkeypatch, capsys):
    assert bus_module.cmd_pen_checkpoint(fake_redis, ns(as_agent="alice", issue=79)) == 1

    fake_redis.set(bus_module.k_pen(79), "alice")
    monkeypatch.setattr(
        bus_module,
        "main_repo_dir",
        lambda: (_ for _ in ()).throw(RuntimeError("not a repo")),
    )
    assert bus_module.cmd_pen_checkpoint(fake_redis, ns(as_agent="alice", issue=79)) == 1

    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "huddle_worktree", lambda *_args: None)
    assert bus_module.cmd_pen_checkpoint(fake_redis, ns(as_agent="alice", issue=79)) == 1

    monkeypatch.setattr(bus_module, "huddle_worktree", lambda *_args: "/worktree")

    def git(_repo, *args, check=True):
        if args == ("rev-parse", "--verify", "origin/huddle/issue-79"):
            return 0, "base", ""
        if args == ("add", "-A"):
            return 0, "", ""
        if args == ("diff", "--cached", "--quiet"):
            return 0, "", ""
        if args == ("merge-base", "--is-ancestor", "HEAD", "origin/huddle/issue-79"):
            return 0, "", ""
        raise AssertionError(args)

    monkeypatch.setattr(bus_module, "git", git)
    assert bus_module.cmd_pen_checkpoint(fake_redis, ns(as_agent="alice", issue=79)) == 0
    assert "nothing to commit" in capsys.readouterr().out


def test_checkpoint_success_pushes_and_announces(bus_module, fake_redis, ns, monkeypatch, capsys):
    fake_redis.set(bus_module.k_pen(79), "alice")
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "huddle_worktree", lambda *_args: "/worktree")

    def git(_repo, *args, check=True):
        # B2 (#82): synced tip we lease against.
        if args == ("rev-parse", "--verify", "origin/huddle/issue-79"):
            return 0, "base", ""
        if args == ("add", "-A"):
            return 0, "", ""
        if args == ("diff", "--cached", "--quiet"):
            return 0, "", ""
        if args == ("merge-base", "--is-ancestor", "HEAD", "origin/huddle/issue-79"):
            return 1, "", ""
        # _leased_push: HEAD resolves to abc1234, descends from the synced tip, the
        # leased push lands, and the post-push ls-remote confirms remote == HEAD.
        if args == ("rev-parse", "HEAD"):
            return 0, "abc1234", ""
        if args == ("merge-base", "--is-ancestor", "base", "HEAD"):
            return 0, "", ""
        if args == (
            "push",
            "--porcelain",
            "--force-with-lease=refs/heads/huddle/issue-79:base",
            "origin",
            "HEAD:huddle/issue-79",
        ):
            return 0, "", ""
        if args == ("ls-remote", "--heads", "origin", "huddle/issue-79"):
            return 0, "abc1234\trefs/heads/huddle/issue-79", ""
        raise AssertionError(args)

    monkeypatch.setattr(bus_module, "git", git)

    assert bus_module.cmd_pen_checkpoint(fake_redis, ns(as_agent="alice", issue=79)) == 0
    assert "abc1234" in capsys.readouterr().out
    assert fake_redis.xlen(bus_module.k_stream("main")) == 1


@pytest.mark.parametrize(
    ("has_meta", "to_agent", "error"),
    [
        (False, "bob", "no huddle"),
        (True, "carol", "not a participant"),
    ],
)
def test_pen_pass_guard_paths_keep_pen(
    bus_module, fake_redis, ns, monkeypatch, has_meta, to_agent, error, capsys
):
    fake_redis.set(bus_module.k_pen(79), "alice")
    if has_meta:
        fake_redis.set(bus_module.k_huddle(79), json.dumps(huddle_meta(bus_module)))
    checkpoint_called = []
    monkeypatch.setattr(
        bus_module,
        "cmd_pen_checkpoint",
        lambda *_args: checkpoint_called.append(True) or 0,
    )

    rc = bus_module.cmd_pen_pass(fake_redis, ns(as_agent="alice", issue=79, to=to_agent))

    assert rc == 1
    assert checkpoint_called == []
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert error in capsys.readouterr().err


def test_pen_pass_success_checkpoints_then_moves_pen_and_clears_challenge(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    meta = huddle_meta(bus_module)
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), json.dumps({"challenger": "bob"}))
    checkpoint_seen = []

    def checkpoint(r, args):
        checkpoint_seen.append((r.get(bus_module.k_pen(79)), args.to))
        return 0

    monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", checkpoint)

    rc = bus_module.cmd_pen_pass(fake_redis, ns(as_agent="alice", issue=79, to="bob"))

    assert rc == 0
    assert checkpoint_seen == [("alice", "bob")]
    assert fake_redis.get(bus_module.k_pen(79)) == "bob"
    updated = json.loads(fake_redis.get(bus_module.k_huddle(79)))
    assert updated["driver"] == "bob"
    assert fake_redis.get(bus_module.k_penchal(79)) is None
    assert "passed the pen" in capsys.readouterr().out
    assert fake_redis.xlen(bus_module.k_stream("main")) == 1


# ---- #94: `pen pass` / `pen take` must ACT on _set_driver's False --------------
#
# `_set_driver` returns False when the huddle metadata key is gone. All three write
# paths used to discard that: they wrote the pen key and announced a handoff anyway.
# Because `huddle close` is the only thing that deletes k_pen, the blind `r.set`
# then RESURRECTED a pen key that nothing would ever clean up, attached to a huddle
# that no longer existed. The fix writes the driver FIRST and aborts on False, so a
# huddle closed under us leaves nothing behind.


def test_pen_pass_into_a_huddle_closed_mid_flight_writes_nothing(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    """The close lands during `pen checkpoint` — a real window: that step does a git
    commit and push, so it is the longest gap in the whole command."""
    fake_redis.set(bus_module.k_huddle(79), json.dumps(huddle_meta(bus_module)))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), json.dumps({"challenger": "bob"}))

    def checkpoint_then_close(_r, _args):
        fake_redis.delete(bus_module.k_huddle(79))
        return 0

    monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", checkpoint_then_close)

    rc = bus_module.cmd_pen_pass(fake_redis, ns(as_agent="alice", issue=79, to="bob"))

    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"   # NOT handed to bob
    assert fake_redis.get(bus_module.k_huddle(79)) is None   # and not resurrected
    assert json.loads(fake_redis.get(bus_module.k_penchal(79))) == {"challenger": "bob"}
    assert "changed while passing" in capsys.readouterr().err
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0  # no handoff announced


def test_pen_take_unheld_into_a_huddle_closed_mid_flight_creates_no_pen_key(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    """The unheld-pen path is the one that CREATES a pen key from nothing, so it is
    the one that can strand a pen on a closed huddle."""
    fake_redis.set(bus_module.k_huddle(79), json.dumps(huddle_meta(bus_module, driver="")))
    real_get = fake_redis.get
    fired = {"done": False}

    def close_when_the_pen_is_read(key, *a, **kw):
        value = real_get(key, *a, **kw)
        if key == bus_module.k_pen(79) and not fired["done"]:
            fired["done"] = True
            fake_redis.delete(bus_module.k_huddle(79))
        return value

    monkeypatch.setattr(fake_redis, "get", close_when_the_pen_is_read)

    rc = bus_module.cmd_pen_take(
        fake_redis, ns(as_agent="bob", issue=79, reason="driver left"))

    assert fired["done"], "the close never landed — the race was not exercised"
    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) is None   # no pen for a dead huddle
    assert fake_redis.get(bus_module.k_huddle(79)) is None
    assert "changed while taking" in capsys.readouterr().err
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_pen_take_force_into_a_huddle_closed_mid_flight_writes_nothing(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    """Same for the force-take path (absent driver, challenge past the grace window)."""
    meta = huddle_meta(bus_module)
    challenge = {"challenger": "bob", "reason": "stale",
                 "ts": (datetime.now(timezone.utc)
                        - timedelta(seconds=bus_module.PEN_TAKE_GRACE + 5)).isoformat(),
                 "session": meta["session"], "driver": "alice"}
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), json.dumps(challenge))

    def absent_and_close(_r, holder):
        assert holder == "alice"
        fake_redis.delete(bus_module.k_huddle(79))
        return False   # alice is absent, so the force-take proceeds

    monkeypatch.setattr(bus_module, "_holder_present", absent_and_close)

    rc = bus_module.cmd_pen_take(fake_redis, ns(as_agent="bob", issue=79, reason="stale"))

    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"   # not stolen into a void
    assert fake_redis.get(bus_module.k_huddle(79)) is None
    assert json.loads(fake_redis.get(bus_module.k_penchal(79))) == challenge
    assert "changed while taking" in capsys.readouterr().err
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_pen_pass_close_reopen_race_does_not_move_new_huddle_pen(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    old_meta = huddle_meta(bus_module)
    old_meta["session"] = "huddle:issue-79:old"
    new_meta = huddle_meta(bus_module)
    new_meta["session"] = "huddle:issue-79:new"
    new_meta["participants"] = ["alice"]
    fake_redis.set(bus_module.k_huddle(79), json.dumps(old_meta))
    fake_redis.set(bus_module.k_pen(79), "alice")

    def checkpoint_then_reopen(_r, _args):
        fake_redis.set(bus_module.k_huddle(79), json.dumps(new_meta))
        fake_redis.set(bus_module.k_pen(79), "alice")
        return 0

    monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", checkpoint_then_reopen)

    rc = bus_module.cmd_pen_pass(fake_redis, ns(as_agent="alice", issue=79, to="bob"))

    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert json.loads(fake_redis.get(bus_module.k_huddle(79))) == new_meta
    assert "changed while passing" in capsys.readouterr().err
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_pen_take_challenge_close_reopen_race_does_not_seed_new_huddle(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    old_meta = huddle_meta(bus_module, driver="alice")
    old_meta["session"] = "huddle:issue-79:old"
    new_meta = huddle_meta(bus_module, driver="alice")
    new_meta["session"] = "huddle:issue-79:new"
    new_meta["participants"] = ["alice"]
    fake_redis.set(bus_module.k_huddle(79), json.dumps(old_meta))
    fake_redis.set(bus_module.k_pen(79), "alice")

    def reopen_before_challenge_record(_r, holder):
        assert holder == "alice"
        fake_redis.set(bus_module.k_huddle(79), json.dumps(new_meta))
        fake_redis.set(bus_module.k_pen(79), "alice")
        return True

    monkeypatch.setattr(bus_module, "_holder_present", reopen_before_challenge_record)

    rc = bus_module.cmd_pen_take(fake_redis, ns(as_agent="bob", issue=79, reason="ask"))

    assert rc == 1
    assert fake_redis.get(bus_module.k_penchal(79)) is None
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert json.loads(fake_redis.get(bus_module.k_huddle(79))) == new_meta
    assert "changed while challenging" in capsys.readouterr().err
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_pen_take_absent_driver_taken_immediately_stale_challenge_irrelevant(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    # #94: an absent driver (no presence anywhere) is treated as gone, so the pen
    # is taken IMMEDIATELY — no PEN_TAKE_GRACE wait. A challenge lingering from a
    # prior huddle session is irrelevant: the take is justified by absence, not by
    # any challenge. (This intentionally drops the earlier brief-lapse grace; pen
    # ops refresh presence, so a live driver is never absent.)
    meta = huddle_meta(bus_module, driver="alice")
    meta["session"] = "huddle:issue-79:new"
    old = (datetime.now(timezone.utc) - timedelta(seconds=bus_module.PEN_TAKE_GRACE + 5))
    stale_challenge = {
        "challenger": "bob", "reason": "old huddle", "ts": old.isoformat(),
        "session": "huddle:issue-79:old", "driver": "alice",
    }
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), json.dumps(stale_challenge))
    monkeypatch.setattr(bus_module, "_holder_present", lambda _r, _holder: False)

    rc = bus_module.cmd_pen_take(fake_redis, ns(as_agent="bob", issue=79, reason="new huddle"))

    assert rc == 0
    assert fake_redis.get(bus_module.k_pen(79)) == "bob"
    meta_after = json.loads(fake_redis.get(bus_module.k_huddle(79)))
    assert meta_after["driver"] == "bob"
    assert fake_redis.get(bus_module.k_penchal(79)) is None  # stale challenge cleared
    assert "took the pen" in capsys.readouterr().out


def test_pen_take_force_aborts_when_challenge_refreshes_mid_take(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    meta = huddle_meta(bus_module, driver="alice")
    old = (datetime.now(timezone.utc) - timedelta(seconds=bus_module.PEN_TAKE_GRACE + 5))
    stale = {
        "challenger": "bob", "reason": "old", "ts": old.isoformat(),
        "session": meta["session"], "driver": "alice",
    }
    fresh = {
        "challenger": "bob", "reason": "fresh", "ts": datetime.now(timezone.utc).isoformat(),
        "session": meta["session"], "driver": "alice",
    }
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    fake_redis.set(bus_module.k_pen(79), "alice")
    fake_redis.set(bus_module.k_penchal(79), json.dumps(stale))

    def refresh_before_force_take(_r, holder):
        assert holder == "alice"
        fake_redis.set(bus_module.k_penchal(79), json.dumps(fresh))
        return False

    monkeypatch.setattr(bus_module, "_holder_present", refresh_before_force_take)

    rc = bus_module.cmd_pen_take(fake_redis, ns(as_agent="bob", issue=79, reason="force"))

    assert rc == 1
    assert fake_redis.get(bus_module.k_pen(79)) == "alice"
    assert json.loads(fake_redis.get(bus_module.k_penchal(79))) == fresh
    assert "changed while taking" in capsys.readouterr().err
