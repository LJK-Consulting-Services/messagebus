import json
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
        if repo == "/worktree" and args == ("push", "origin", "HEAD:huddle/issue-79"):
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
        if args == ("add", "-A"):
            return 0, "", ""
        if args == ("diff", "--cached", "--quiet"):
            return 0, "", ""
        if args == ("merge-base", "--is-ancestor", "HEAD", "origin/huddle/issue-79"):
            return 1, "", ""
        if args == ("push", "origin", "HEAD:huddle/issue-79"):
            return 0, "", ""
        if args == ("rev-parse", "--short", "HEAD"):
            return 0, "abc1234", ""
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
