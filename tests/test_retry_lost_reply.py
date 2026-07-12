"""B3 (#83) — the hazard that connection retries introduce.

`retry_on_error` re-sends a command whose reply was lost. For `SET NX` that is
sharp: the re-send finds the key its OWN first attempt created, so NX reports a
loss for the attempt that actually won. Every `SET NX` in `bus` has to survive
its own reply going missing.

These simulate exactly that state — the lock already holds our value when the
(re-sent) `SET NX` runs — and assert the command still completes its side effects
instead of half-claiming and returning success.
"""
import json


def test_claim_completes_setup_when_its_own_set_nx_reply_was_lost(bus_module, fake_redis, ns,
                                                                  no_github, monkeypatch, capsys):
    """Without this, `bus claim --worktree` exits 0 having created NO worktree and
    set NO gh label — and the agent then works in the coordinator's main tree,
    which is precisely what the claim rollback exists to prevent."""
    created, labelled = [], []
    monkeypatch.setattr(bus_module, "ws_create",
                        lambda *a, **kw: created.append(a) or (0, "/tmp/wt"))
    monkeypatch.setattr(bus_module, "set_status_label",
                        lambda issue, label: labelled.append((issue, label)))

    # the state a lost SET NX reply leaves behind: OUR value is already in the lock,
    # so the retried SET NX returns "lost".
    fake_redis.set(bus_module.k_lock(42), "claude-2")

    rc = bus_module.cmd_claim(fake_redis, ns(as_agent="claude-2", issue=42, ttl=60,
                                             worktree=True, base="dev"))

    assert rc == 0
    assert len(created) == 1                       # the worktree really was set up
    assert labelled == [(42, "status:claimed")]    # and the gh state machine moved
    assert fake_redis.get(bus_module.k_lock(42)) == "claude-2"
    bodies = [f["body"] for _, f in fake_redis.xrange(bus_module.k_stream("main"))]
    assert any("claimed issue #42" in b for b in bodies)  # peers were told
    capsys.readouterr()


def test_claim_keeps_a_pre_existing_lock_when_the_worktree_fails(bus_module, fake_redis, ns,
                                                                 no_github, monkeypatch, capsys):
    """The rollback must only ever drop a lock THIS invocation created. Rolling
    back a claim we already held would abandon work someone is mid-way through."""
    monkeypatch.setattr(bus_module, "ws_create", lambda *a, **kw: (1, "boom"))
    fake_redis.set(bus_module.k_lock(42), "claude-2")

    rc = bus_module.cmd_claim(fake_redis, ns(as_agent="claude-2", issue=42, ttl=60,
                                             worktree=True, base="dev"))

    assert rc == 1
    assert fake_redis.get(bus_module.k_lock(42)) == "claude-2"  # NOT rolled back
    assert "keeping the claim you already held" in capsys.readouterr().err


def test_claim_still_refuses_an_issue_held_by_someone_else(bus_module, fake_redis, ns,
                                                           no_github, monkeypatch, capsys):
    """The lost-reply path must not become a way to steal a peer's claim."""
    created = []
    monkeypatch.setattr(bus_module, "ws_create",
                        lambda *a, **kw: created.append(a) or (0, "/tmp/wt"))
    fake_redis.set(bus_module.k_lock(42), "codex-1")

    rc = bus_module.cmd_claim(fake_redis, ns(as_agent="claude-2", issue=42, ttl=60,
                                             worktree=False, base="dev"))

    assert rc == 1
    assert created == []
    assert fake_redis.get(bus_module.k_lock(42)) == "codex-1"
    assert "already claimed by codex-1" in capsys.readouterr().err


def test_huddle_open_survives_its_own_lost_set_nx_reply(bus_module, fake_redis, ns,
                                                        no_github, monkeypatch, capsys):
    """Bailing out here would strand the issue under a phantom huddle lock with no
    metadata and no branch — which `bus reap` refuses to free (_is_huddle_lock) and
    `bus release` cannot touch. An unrecoverable lock, short of a redis-cli DEL."""
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "create_shared_branch",
                        lambda *a, **kw: (0, "abc1234"))

    # Force the token this invocation will generate, then pre-seed the lock with it:
    # exactly the state left by a SET NX whose reply was lost and got re-sent.
    session = "huddle:issue-44:fixed-token"
    monkeypatch.setattr(bus_module, "new_huddle_holder", lambda _issue: session)
    fake_redis.set(bus_module.k_lock(44), session)

    rc = bus_module.cmd_huddle_open(fake_redis, ns(as_agent="claude-2", issue=44, ttl=60,
                                                   base="dev", allow_stale=False))

    assert rc == 0
    meta = json.loads(fake_redis.get(bus_module.k_huddle(44)))
    assert meta["session"] == session and meta["status"] == "open"
    assert meta["driver"] == "claude-2"
    assert fake_redis.get(bus_module.k_pen(44)) == "claude-2"
    capsys.readouterr()


def test_huddle_open_still_refuses_a_different_live_session(bus_module, fake_redis, ns,
                                                            no_github, monkeypatch, capsys):
    """Only OUR OWN session token may be treated as 'we already won'."""
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "new_huddle_holder",
                        lambda _issue: "huddle:issue-44:mine")
    branches = []
    monkeypatch.setattr(bus_module, "create_shared_branch",
                        lambda *a, **kw: branches.append(a) or (0, "abc1234"))
    fake_redis.set(bus_module.k_lock(44), "huddle:issue-44:someone-elses")

    rc = bus_module.cmd_huddle_open(fake_redis, ns(as_agent="claude-2", issue=44, ttl=60,
                                                   base="dev", allow_stale=False))

    assert rc == 1
    assert branches == []  # never touched the shared branch
    assert fake_redis.get(bus_module.k_lock(44)) == "huddle:issue-44:someone-elses"
    assert "already locked by" in capsys.readouterr().err
