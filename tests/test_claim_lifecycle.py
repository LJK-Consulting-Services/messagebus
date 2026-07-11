from unittest import mock


def test_claim_acquire_release_and_ttl(bus_module, fake_redis, ns, no_github, capsys):
    claim = ns(as_agent="alice", issue=79, ttl=60, worktree=False, base="dev")

    assert bus_module.cmd_claim(fake_redis, claim) == 0
    assert fake_redis.get(bus_module.k_lock(79)) == "alice"
    assert 0 < fake_redis.ttl(bus_module.k_lock(79)) <= 60

    assert bus_module.cmd_claim(
        fake_redis,
        ns(as_agent="bob", issue=79, ttl=60, worktree=False, base="dev"),
    ) == 1
    assert fake_redis.get(bus_module.k_lock(79)) == "alice"

    assert bus_module.cmd_renew(fake_redis, ns(as_agent="alice", issue=79, ttl=120)) == 0
    assert 60 <= fake_redis.ttl(bus_module.k_lock(79)) <= 120

    assert bus_module.cmd_release(fake_redis, ns(as_agent="bob", issue=79)) == 1
    assert fake_redis.get(bus_module.k_lock(79)) == "alice"

    assert bus_module.cmd_release(fake_redis, ns(as_agent="alice", issue=79)) == 0
    assert fake_redis.get(bus_module.k_lock(79)) is None
    assert "released issue #79" in capsys.readouterr().out


def test_claim_with_worktree_failure_rolls_back_and_skips_side_effects(
    bus_module, fake_redis, ns, monkeypatch
):
    status = mock.Mock()
    gh = mock.Mock(return_value=(0, "", ""))
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: [])
    monkeypatch.setattr(bus_module, "set_status_label", status)
    monkeypatch.setattr(bus_module, "gh", gh)
    monkeypatch.setattr(bus_module, "ws_create", lambda *_args, **_kwargs: (1, None))

    rc = bus_module.cmd_claim(
        fake_redis,
        ns(as_agent="alice", issue=80, ttl=60, worktree=True, base="dev"),
    )

    assert rc == 1
    assert fake_redis.get(bus_module.k_lock(80)) is None
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0
    status.assert_not_called()
    gh.assert_not_called()


def test_claim_fails_closed_when_status_claimed_label_outlives_lock(
    bus_module, fake_redis, ns, monkeypatch
):
    status = mock.Mock()
    gh = mock.Mock(return_value=(0, "", ""))
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: ["status:claimed"])
    monkeypatch.setattr(bus_module, "set_status_label", status)
    monkeypatch.setattr(bus_module, "gh", gh)

    rc = bus_module.cmd_claim(
        fake_redis,
        ns(as_agent="alice", issue=81, ttl=60, worktree=False, base="dev"),
    )

    assert rc == 1
    assert fake_redis.get(bus_module.k_lock(81)) is None
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0
    status.assert_not_called()
    gh.assert_not_called()


def test_claim_warns_on_label_read_failure_and_surfaces_gh_error(
    bus_module, fake_redis, ns, monkeypatch, capsys
):
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: None)
    monkeypatch.setattr(
        bus_module,
        "set_status_label",
        mock.Mock(side_effect=RuntimeError("gh down")),
    )

    rc = bus_module.cmd_claim(
        fake_redis,
        ns(as_agent="alice", issue=82, ttl=60, worktree=False, base="dev"),
    )

    assert rc == 0
    assert fake_redis.get(bus_module.k_lock(82)) == "alice"
    err = capsys.readouterr().err
    assert "could not read labels" in err
    assert "gh update failed: gh down" in err


def test_claim_refuses_label_claimed_by_other_holder(
    bus_module, fake_redis, ns, monkeypatch
):
    fake_redis.set(bus_module.k_lock(83), "bob")
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: ["status:claimed"])

    rc = bus_module.cmd_claim(
        fake_redis,
        ns(as_agent="alice", issue=83, ttl=60, worktree=False, base="dev"),
    )

    assert rc == 1
    assert fake_redis.get(bus_module.k_lock(83)) == "bob"
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0


def test_claim_already_held_by_same_agent_is_idempotent(bus_module, fake_redis, ns, no_github):
    fake_redis.set(bus_module.k_lock(84), "alice")

    rc = bus_module.cmd_claim(
        fake_redis,
        ns(as_agent="alice", issue=84, ttl=60, worktree=False, base="dev"),
    )

    assert rc == 0
    assert fake_redis.get(bus_module.k_lock(84)) == "alice"
    assert fake_redis.xlen(bus_module.k_stream("main")) == 0
