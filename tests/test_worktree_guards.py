import json
from unittest import mock


def test_worktree_unpushed_unknown_base_fails_closed(bus_module):
    assert bus_module.worktree_unpushed("/does/not/matter", "") is True


def test_ws_remove_unpushed_guard_preserves_worktree_metadata_and_skips_git_remove(
    bus_module, fake_redis, ns, tmp_path, monkeypatch
):
    root = tmp_path / "worktrees"
    path = root / "issue-79-alice"
    path.mkdir(parents=True)
    meta = {
        "issue": 79,
        "agent": "alice",
        "path": str(path),
        "branch": "feat/issue-79-alice",
        "base": "dev",
        "base_commit": "base",
        "kind": "solo",
        "detached": False,
    }
    fake_redis.set(bus_module.k_worktree(79), json.dumps(meta))
    git = mock.Mock(return_value=(0, "", ""))
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: str(tmp_path / "repo"))
    monkeypatch.setattr(bus_module, "worktree_root", lambda _main: str(root))
    monkeypatch.setattr(bus_module, "worktree_dirty", lambda _path: False)
    monkeypatch.setattr(bus_module, "worktree_unpushed", lambda _path, _base: True)
    monkeypatch.setattr(bus_module, "git", git)

    rc = bus_module.cmd_ws_remove(
        fake_redis,
        ns(as_agent="alice", issue=79, force=False),
    )

    assert rc == 1
    assert json.loads(fake_redis.get(bus_module.k_worktree(79))) == meta
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0
    git.assert_not_called()
