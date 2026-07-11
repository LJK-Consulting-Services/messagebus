import json


def test_ws_create_path_and_list_success(bus_module, fake_redis, ns, tmp_path, monkeypatch, capsys):
    root = tmp_path / "worktrees"
    main = tmp_path / "repo"
    fake_redis.set(bus_module.k_lock(79), "alice")
    git_calls = []

    def git(repo, *args, check=True):
        git_calls.append((repo, args))
        if args == ("fetch", "origin", "dev"):
            return 0, "", ""
        if args == ("rev-parse", "--verify", "origin/dev"):
            return 0, "base123", ""
        if args == ("rev-parse", "--verify", "refs/heads/feat/issue-79-alice"):
            return 1, "", ""
        if args[:2] == ("worktree", "add"):
            return 0, "", ""
        raise AssertionError(f"unexpected git call {repo} {args}")

    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: str(main))
    monkeypatch.setattr(bus_module, "worktree_root", lambda _main: str(root))
    monkeypatch.setattr(bus_module, "valid_git_ref", lambda _main, _ref: True)
    monkeypatch.setattr(bus_module, "git", git)

    rc = bus_module.cmd_ws_create(
        fake_redis,
        ns(as_agent="alice", issue=79, base="dev", type="feat", allow_stale=False, allow_nested=False),
    )

    path = capsys.readouterr().out.strip().splitlines()[-1]
    assert rc == 0
    assert path.endswith("issue-79-alice")
    assert any(args[:2] == ("worktree", "add") for _repo, args in git_calls)

    assert bus_module.cmd_ws_path(fake_redis, ns(issue=79)) == 0
    assert path in capsys.readouterr().out

    monkeypatch.setattr(bus_module, "worktree_dirty", lambda _path: True)
    monkeypatch.setattr(bus_module, "worktree_unpushed", lambda _path, _base: False)
    fake_redis.set(bus_module.k_presence("main", "alice"), "now")

    assert bus_module.cmd_ws_list(fake_redis, ns(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["dirty"] in {"yes", "gone"}
    assert rows[0]["present"] == "yes"


def test_ws_create_fail_closed_branches(bus_module, fake_redis, ns, tmp_path, monkeypatch):
    fake_redis.set(bus_module.k_lock(79), "alice")
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: str(tmp_path / "repo"))
    monkeypatch.setattr(bus_module, "worktree_root", lambda _main: str(tmp_path / "worktrees"))

    rc, path = bus_module.ws_create(fake_redis, "main", "bob", 79)
    assert (rc, path) == (1, None)

    rc, path = bus_module.ws_create(fake_redis, "main", "alice", 79, base="-p")
    assert (rc, path) == (1, None)


def test_create_shared_branch_success_and_existing_branch(bus_module, monkeypatch):
    calls = []

    def git(_repo, *args, check=True):
        calls.append(args)
        if args == ("fetch", "origin", "dev"):
            return 0, "", ""
        if args == ("rev-parse", "--verify", "origin/dev"):
            return 0, "base123", ""
        if args == ("ls-remote", "--heads", "origin", "huddle/issue-79"):
            return 0, "", ""
        if args == ("push", "origin", "base123:refs/heads/huddle/issue-79"):
            return 0, "", ""
        raise AssertionError(args)

    monkeypatch.setattr(bus_module, "valid_git_ref", lambda _main, _ref: True)
    monkeypatch.setattr(bus_module, "git", git)

    assert bus_module.create_shared_branch("/repo", "dev", "huddle/issue-79") == (0, "base123")

    monkeypatch.setattr(
        bus_module,
        "git",
        lambda _repo, *args, check=True: (0, "abc\trefs/heads/huddle/issue-79", "")
        if args == ("ls-remote", "--heads", "origin", "huddle/issue-79")
        else (0, "base123", ""),
    )
    rc, msg = bus_module.create_shared_branch("/repo", "dev", "huddle/issue-79")
    assert rc == 1
    assert "already exists" in msg
