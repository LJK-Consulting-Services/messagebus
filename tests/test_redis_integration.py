import json
import os
import uuid
from urllib.parse import urlparse

import pytest


pytestmark = pytest.mark.integration
SAFE_REDIS_HOSTS = {"127.0.0.1", "localhost", "::1"}


@pytest.fixture
def redis_url():
    url = os.environ.get("BUS_INTEGRATION_REDIS_URL")
    if not url:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("BUS_INTEGRATION_REDIS_URL is required in CI")
        pytest.skip("set BUS_INTEGRATION_REDIS_URL to run Redis integration tests")
    parsed = urlparse(url)
    if parsed.scheme != "redis" or parsed.hostname not in SAFE_REDIS_HOSTS or parsed.password:
        pytest.fail(
            "BUS_INTEGRATION_REDIS_URL must point to local throwaway Redis "
            "(redis://127.0.0.1/... or redis://localhost/...) without credentials"
        )
    return url


def _issue_id():
    return 900_000 + (uuid.uuid4().int % 100_000)


def _delete_issue_keys(r, bus_module, issue):
    r.delete(
        bus_module.k_lock(issue),
        bus_module.k_huddle(issue),
        bus_module.k_pen(issue),
        bus_module.k_penchal(issue),
        bus_module.k_signoff(issue),
        bus_module.k_block(issue),
    )


def test_real_redis_send_poll_smoke(bus_module, ns, redis_url, capsys):
    room = f"ci-{uuid.uuid4().hex}"
    r = bus_module.connect(redis_url)

    try:
        assert bus_module.cmd_send(
            r,
            ns(room=room, frm="alice", to="bob", topic="issue-79", reply_to="", kind="msg", body="hi"),
        ) == 0
        capsys.readouterr()

        assert bus_module.cmd_poll(
            r,
            ns(room=room, as_agent="bob", topic="issue-79", json=True),
        ) == 0
        assert '"body": "hi"' in capsys.readouterr().out
    finally:
        for key in r.scan_iter(match=f"bus:*:{room}*"):
            r.delete(key)
        r.delete(bus_module.k_stream(room))


def test_real_redis_compare_delete_lock_runs_lua(bus_module, redis_url):
    issue = _issue_id()
    r = bus_module.connect(redis_url)

    try:
        r.set(bus_module.k_lock(issue), "agent-a", ex=60)

        assert bus_module.compare_delete_lock(r, issue, "agent-b") == 0
        assert r.get(bus_module.k_lock(issue)) == "agent-a"

        assert bus_module.compare_delete_lock(r, issue, "agent-a") == 1
        assert r.get(bus_module.k_lock(issue)) is None
    finally:
        _delete_issue_keys(r, bus_module, issue)


def test_real_redis_compare_set_huddle_meta_runs_lua(bus_module, redis_url):
    issue = _issue_id()
    session = f"huddle:issue-{issue}:session"
    meta_json = json.dumps({"issue": issue, "session": session, "status": "open"})
    r = bus_module.connect(redis_url)

    try:
        r.set(bus_module.k_lock(issue), session, ex=60)

        assert bus_module.compare_set_huddle_meta(r, issue, "wrong-holder", meta_json) == 0
        assert r.get(bus_module.k_huddle(issue)) is None

        assert bus_module.compare_set_huddle_meta(r, issue, session, meta_json) == 1
        r.expire(bus_module.k_huddle(issue), 60)
        assert json.loads(r.get(bus_module.k_huddle(issue))) == {
            "issue": issue,
            "session": session,
            "status": "open",
        }
        assert r.get(bus_module.k_lock(issue)) == session
    finally:
        _delete_issue_keys(r, bus_module, issue)


def test_real_redis_huddle_open_branch_failure_rolls_back_lock_with_lua(
    bus_module,
    ns,
    redis_url,
    no_github,
    monkeypatch,
    capsys,
):
    issue = _issue_id()
    r = bus_module.connect(redis_url)

    def fail_branch(_main, _base, _branch, allow_stale=False):
        holder = r.get(bus_module.k_lock(issue))
        assert holder and holder.startswith(f"huddle:issue-{issue}:")
        return 1, "branch create failed"

    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "create_shared_branch", fail_branch)

    try:
        rc = bus_module.cmd_huddle_open(
            r,
            ns(
                issue=issue,
                as_agent="agent-a",
                ttl=60,
                base="dev",
                allow_stale=False,
                room="integration",
            ),
        )

        assert rc == 1
        assert r.get(bus_module.k_lock(issue)) is None
        assert r.get(bus_module.k_huddle(issue)) is None
        assert "rolled back the lock" in capsys.readouterr().err
    finally:
        _delete_issue_keys(r, bus_module, issue)


# --- lost-update race on the huddle metadata blob -------------------------------
#
# `driver` and `participants` live in ONE JSON value. Any read-modify-write of it
# races `huddle join`, which grows `participants` under WATCH/MULTI. These drive a
# real concurrent write into the window between the read and the write and assert
# the other field survives. Interleave is deterministic, not timing-dependent.


def _seed_huddle(r, bus_module, issue, participants, driver):
    session = f"huddle:issue-{issue}:session"
    r.set(bus_module.k_lock(issue), session, ex=60)
    r.set(bus_module.k_huddle(issue), json.dumps({
        "issue": issue, "opener": participants[0], "participants": list(participants),
        "driver": driver, "branch": bus_module.huddle_branch(issue), "base": "dev",
        "base_commit": "0" * 40, "session": session, "status": "open",
        "created_at": "2026-07-11T00:00:00+00:00"}), ex=60)


def _fire_once_on_huddle_read(monkeypatch, r, bus_module, issue, action, where="any"):
    """Run `action` (a concurrent writer, on its own connection) exactly once, the
    first time the command under test READS the huddle key.

    Hooks both read paths on purpose. The fixed code reads the key only through a
    watching pipeline (`pipe.get`); the blind read-modify-write it replaces reads
    it through the client (`r.get`). Hooking only one would make the mutation check
    dishonest — it would never fire against one of the two versions, and the test
    would fail for want of a concurrent write rather than for losing it.

    where="pipeline" restricts the trigger to the watched window, which is how a
    test pins the WATCH-abort-and-retry path specifically.
    """
    hkey = bus_module.k_huddle(issue)
    state = {"fired": False}

    def hook(get):
        def wrapped(key, *a, **kw):
            val = get(key, *a, **kw)
            # one-shot: a retry must not re-fire, or the loop never settles
            if key == hkey and not state["fired"]:
                state["fired"] = True
                action()
            return val
        return wrapped

    if where == "any":
        monkeypatch.setattr(r, "get", hook(r.get))
    orig_pipeline = r.pipeline

    def pipeline(*a, **kw):
        pipe = orig_pipeline(*a, **kw)
        pipe.get = hook(pipe.get)
        return pipe

    monkeypatch.setattr(r, "pipeline", pipeline)
    return state


def test_real_redis_drain_keeps_a_join_that_lands_mid_meta_write(
    bus_module, ns, redis_url, monkeypatch,
):
    """drain clears its own `driver`; a join committing inside that window survives.

    Losing it is not cosmetic: the done-gate only requires sign-off from
    participants it can see, so an erased participant lets the huddle close with
    its reviewer never having signed.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    joiner = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        monkeypatch.setattr(bus_module, "_unpushed_pen_issues", lambda *_a: [])
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: bus_module.cmd_huddle_join(
                joiner, ns(issue=issue, as_agent="agent-b", room="integration")))

        assert bus_module.cmd_drain(
            r, ns(as_agent="agent-d", force=False, room="integration")) == 0

        assert state["fired"], "the concurrent join never landed — race not exercised"
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["participants"] == ["agent-d", "agent-a", "agent-b"]
        assert meta["driver"] == ""
        assert verify.get(bus_module.k_pen(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_pen_take_keeps_a_join_that_lands_mid_meta_write(
    bus_module, ns, redis_url, monkeypatch,
):
    """Same race on `pen take`'s unheld-pen path, which also rewrites `driver`."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    joiner = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "")
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: bus_module.cmd_huddle_join(
                joiner, ns(issue=issue, as_agent="agent-b", room="integration")))

        assert bus_module.cmd_pen_take(
            r, ns(issue=issue, as_agent="agent-a", reason="driver left",
                  room="integration")) == 0

        assert state["fired"], "the concurrent join never landed — race not exercised"
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["participants"] == ["agent-d", "agent-a", "agent-b"]
        assert meta["driver"] == "agent-a"
        assert verify.get(bus_module.k_pen(issue)) == "agent-a"
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_meta_write_retries_a_join_inside_the_watched_window(
    bus_module, ns, redis_url, monkeypatch,
):
    """Pin the WATCH path itself: the join lands strictly BETWEEN the watched read
    and EXEC, so the transaction must abort and re-read rather than commit a stale
    participant list."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    joiner = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "")
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: bus_module.cmd_huddle_join(
                joiner, ns(issue=issue, as_agent="agent-b", room="integration")),
            where="pipeline")

        assert bus_module.cmd_pen_take(
            r, ns(issue=issue, as_agent="agent-a", reason="driver left",
                  room="integration")) == 0

        assert state["fired"], "nothing wrote inside the watched window"
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["participants"] == ["agent-d", "agent-a", "agent-b"]
        assert meta["driver"] == "agent-a"
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_drain_does_not_clear_a_driver_a_concurrent_take_installed(
    bus_module, ns, redis_url, monkeypatch,
):
    """drain's "is the driver still me?" test is a CAS evaluated inside the
    transaction. A `pen take` that grabs the pen drain just released, in the window
    before drain rewrites the metadata, must keep its driver — on a stale read
    drain would blank out a driver that is live and holding the pen."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    taker = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        monkeypatch.setattr(bus_module, "_unpushed_pen_issues", lambda *_a: [])
        # Fires after drain has already CAS-deleted the pen key, so agent-a finds
        # the pen unheld and takes it for real.
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: bus_module.cmd_pen_take(
                taker, ns(issue=issue, as_agent="agent-a", reason="d is draining",
                          room="integration")))

        assert bus_module.cmd_drain(
            r, ns(as_agent="agent-d", force=False, room="integration")) == 0

        assert state["fired"], "the concurrent take never landed — race not exercised"
        assert verify.get(bus_module.k_pen(issue)) == "agent-a"
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["driver"] == "agent-a", "drain blanked a driver it no longer owned"
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_pen_pass_keeps_a_join_that_lands_mid_meta_write(
    bus_module, ns, redis_url, monkeypatch,
):
    """The fourth routed site: `pen pass` rewrites `driver` after checkpointing."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    joiner = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        # The handoff's git commit+push is not what's under test here.
        monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", lambda _r, _args: 0)
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: bus_module.cmd_huddle_join(
                joiner, ns(issue=issue, as_agent="agent-b", room="integration")))

        assert bus_module.cmd_pen_pass(
            r, ns(issue=issue, as_agent="agent-d", to="agent-a",
                  room="integration")) == 0

        assert state["fired"], "the concurrent join never landed — race not exercised"
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["participants"] == ["agent-d", "agent-a", "agent-b"]
        assert meta["driver"] == "agent-a"
        assert verify.get(bus_module.k_pen(issue)) == "agent-a"
    finally:
        _delete_issue_keys(verify, bus_module, issue)


# --- done-gate TOCTOU on the close path (#92) -----------------------------------
#
# The sites above lose a participant by OVERWRITING the blob. These lose the gate's
# INPUTS: the gate ran on a snapshot, and the close then deleted the very state a
# concurrent writer had just added — the joiner's chance to sign, or an open block.
#
# Both drive a real concurrent write into the gate->EXEC window on real Redis. The
# interleave is deterministic (an abort, not a sleep), so neither is timing-flaky.


def _seed_presence(r, bus_module, room, agents):
    """Presence for each agent, and the keys to delete in teardown. An ABSENT
    participant does not hold the done-gate, so a test that forgets this passes for
    the wrong reason — the participant is skipped, not checked."""
    keys = [bus_module.k_presence(room, a) for a in agents]
    for k in keys:
        r.set(k, "now", ex=60)
    return keys


def test_real_redis_close_regates_on_a_join_inside_the_gate_window(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """A join landing inside the close's gate window must not slip past the done-gate.

    This is a gate BYPASS, not a cosmetic race. A and B have both signed at the tip,
    so the gate is legitimately satisfied for the participants the closer read. C
    joins while the close is in flight; the pre-fix close gated on a participant list
    that never contained C, passed, and deleted the whole huddle — including the
    sign-off key C would have signed. C is silently merged over.

    Under the fix the join aborts the EXEC, the close re-gates against the fresh
    participant list, and C's missing sign-off refuses the close. The abort is what
    makes this deterministic rather than timing-dependent.
    """
    issue = _issue_id()
    tip = "c" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    joiner = bus_module.connect(redis_url)
    presence = []  # predeclared: the `finally` must not NameError over a seeding failure

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        # The shared branch is not what's under test; pin the tip so the gate turns
        # purely on who has signed it.
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: tip)
        # Closer + B have both signed AT THE TIP: without C the gate passes, so a
        # close that ignores C returns 0 rather than failing for some unrelated reason.
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration",
                                  ("agent-a", "agent-b", "agent-c"))

        # where="any" on purpose: the fixed close reads the key through the watching
        # pipeline, the pre-fix close through the client. Hooking only one would fire
        # against a single version — and the negative control would then fail for want
        # of a concurrent join rather than for losing it.
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: bus_module.cmd_huddle_join(
                joiner, ns(issue=issue, as_agent="agent-c", room="integration")))

        rc = bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False))

        assert state["fired"], "the concurrent join never landed — race not exercised"
        assert rc == 1, "the done-gate passed with a present, unsigned participant"
        # A refused close must leave the huddle INTACT — C still has to be able to
        # sign. Asserting rc alone would miss a close that refuses but deletes anyway.
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert "agent-c" in meta["participants"], "the join was lost by the close"
        assert verify.get(bus_module.k_signoff(issue)) is not None
        assert verify.get(bus_module.k_lock(issue)) is not None
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        if presence:
            verify.delete(*presence)


def test_real_redis_close_regates_on_a_block_raised_inside_the_gate_window(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """A block raised after the gate read the block list must not be closed over.

    `signoff --block` writes k_block and NOTHING else, so watching only the metadata
    key left this wide open: the gate read an empty block list, the WATCH stayed
    clean, EXEC committed — and the close's own delete destroyed the block. The
    huddle closed over a live objection, and the objection vanished with it.

    The hook fires on `_shared_tip`, which `donegate` calls AFTER its block read and
    before its sign-off read — landing the block precisely in the gate->EXEC window.
    Hooking the huddle read instead would be a weaker test: the block would land
    BEFORE the gate and be caught by the gate rather than by the WATCH.
    """
    issue = _issue_id()
    tip = "d" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    blocker = bus_module.connect(redis_url)
    presence = []

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        # Everyone has signed: the gate is satisfied but for the block, so a close
        # that misses the block returns 0 rather than failing for an unrelated reason.
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration", ("agent-a", "agent-b"))

        fired = {"n": 0}

        def tip_then_block(_issue):
            if not fired["n"]:
                fired["n"] = 1
                bus_module.cmd_signoff(blocker, ns(
                    issue=issue, as_agent="agent-b", room="integration",
                    block="found a data-loss bug, do NOT merge"))
            return tip

        monkeypatch.setattr(bus_module, "_shared_tip", tip_then_block)

        rc = bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False))

        assert fired["n"], "the concurrent block never landed — race not exercised"
        assert rc == 1, "the done-gate closed the huddle over an open block"
        # The block must SURVIVE the refused close — destroying it would silently
        # discard the objection even though the close itself was turned down.
        blocks = json.loads(verify.get(bus_module.k_block(issue)))
        assert [b["agent"] for b in blocks] == ["agent-b"], "the block was destroyed"
        assert verify.get(bus_module.k_huddle(issue)) is not None
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        if presence:
            verify.delete(*presence)


def test_real_redis_close_still_closes_when_the_gate_is_genuinely_met(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """The WATCH/retry must not break the ordinary close — without this, the two tests
    above would also pass against a `cmd_huddle_close` that never closes anything."""
    issue = _issue_id()
    tip = "c" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    presence = []

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: tip)
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration", ("agent-a", "agent-b"))

        assert bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False)) == 0

        # Lock released and every huddle key gone — the close's whole contract.
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_lock(issue)) is None
        assert verify.get(bus_module.k_signoff(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        if presence:
            verify.delete(*presence)


def test_real_redis_close_with_a_reaped_lock_clears_state_but_not_the_label(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """The MISS half of the CAS the close queues inside its MULTI.

    #92 moved the lock release from a client-side `compare_delete_lock` into the
    gated transaction as a queued EVAL. The hit path is covered above; this is the
    branch where a Lua CAS and a naive DEL actually differ, so it is the one worth
    proving against a real server. fakeredis cannot: it only speaks EVAL with `lupa`
    installed, and the mirror in conftest resolves the compare in Python rather than
    in the script.

    Contract when the lock is NOT ours (it expired, or was reaped and re-acquired):
    the CAS must return 0 and leave the OTHER holder's lock alone, the huddle state
    must still be cleared (close is the only orphan-cleanup path, and meta has no
    TTL), and the status label must NOT advance — doing so would clobber whoever now
    holds the claim.
    """
    issue = _issue_id()
    tip = "c" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    labels = []
    presence = []

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: tip)
        # `no_github` already neutralises gh; re-point set_status_label so we can assert
        # the close does NOT advance it.
        monkeypatch.setattr(bus_module, "set_status_label",
                            lambda _issue, label: labels.append(label))
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration", ("agent-a", "agent-b"))
        # Our session lock lapsed and someone else now holds the claim on this issue.
        r.set(bus_module.k_lock(issue), "someone-else", ex=60)

        rc = bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False))

        assert rc == 0
        assert verify.get(bus_module.k_lock(issue)) == "someone-else", \
            "the CAS deleted a lock that was not ours"
        assert verify.get(bus_module.k_huddle(issue)) is None   # orphan meta still reaped
        assert verify.get(bus_module.k_signoff(issue)) is None
        assert labels == [], "advanced the label while another agent held the claim"
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        if presence:
            verify.delete(*presence)


# ---- #94: a CAS/existence result the caller drops on the floor -----------------
#
# The real-server half. fakeredis can reproduce the STATE a race leaves behind, but
# the guarantees being claimed here are Redis's own — a real Lua CAS, and a real
# WATCH/MULTI that aborts when a watched key is touched. Both of these also FAIL
# against the pre-fix bus, which is what makes them evidence rather than decoration.


def test_real_redis_pen_pass_into_a_huddle_closed_mid_flight_leaves_no_pen(
    bus_module, ns, redis_url, monkeypatch, capsys,
):
    """#94 item 2, on a real server.

    Pre-fix, `pen pass` discarded `_set_driver`'s False and ran its blind
    `r.set(k_pen(...))` anyway — resurrecting a pen key for a huddle that had just
    closed (close is the only path that deletes k_pen, so nothing would ever reap
    it) and announcing a handoff into it.

    The concurrent writer deletes the huddle keys directly rather than calling
    `cmd_huddle_close`: the Redis-side mutation is identical (close DELs exactly
    these keys) and driving the real command would fire its gh side effects at the
    live repo from a test.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", lambda _r, _args: 0)
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: _delete_issue_keys(closer, bus_module, issue))

        rc = bus_module.cmd_pen_pass(
            r, ns(issue=issue, as_agent="agent-d", to="agent-a", room="integration"))

        assert state["fired"], "the concurrent close never landed — race not exercised"
        assert rc == 1                                       # aborts, and says so
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_pen(issue)) is None   # no resurrected pen key
        assert "changed while passing" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_drain_clears_driver_when_release_reply_was_lost(
    bus_module, ns, redis_url, monkeypatch,
):
    """#94 item 1, against a real server with real WATCH/MULTI semantics.

    B3 (#83) made Redis writes retryable, so release code must survive "first
    execution landed, reply was lost, operation ran again." The second pass sees
    the pen already gone; it must still report success only when the matching
    huddle session's driver is already blank.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        monkeypatch.setattr(bus_module, "_unpushed_pen_issues", lambda *_a: [])
        real_release = bus_module._release_pen_driver

        def lost_reply_retry(client, target, agent, expected_session):
            assert (target, agent) == (str(issue), "agent-d")
            assert real_release(client, target, agent, expected_session)
            return real_release(client, target, agent, expected_session)

        monkeypatch.setattr(bus_module, "_release_pen_driver", lost_reply_retry)

        assert bus_module.cmd_drain(
            r, ns(as_agent="agent-d", force=False, room="integration")) == 0

        assert verify.get(bus_module.k_pen(issue)) is None
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["driver"] == "", "drain left a stale driver on a pen it released"
        assert meta["participants"] == ["agent-d", "agent-a"]  # nothing else disturbed
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def _fire_once_on_multi(monkeypatch, r, action):
    """Fire `action` after every watched READ, immediately before EXEC.

    `_fire_once_on_huddle_read` fires too early to pin a WATCH: `_set_driver` reads the
    huddle key first and the challenge key later, so a write injected at the huddle read
    is still visible to the plain `challenge_expect` compare that follows. Only a write
    landing after ALL the reads leaves the WATCH as the sole thing that can catch it.

    Key-matching the CHALLENGE key instead (a `key=` axis on the existing helper) would
    sit in the same window today, but only because the challenge key happens to be the
    last thing read before EXEC. Hooking `multi()` says "after every read" directly, so
    it keeps pinning the WATCH if those reads are ever reordered.

    There is deliberately no key filter: this fires on the first `multi()` of ANY
    pipeline. That is precise only because nothing earlier in the driver-absent take
    opens one -- `touch_presence`, `_holder_present` and `_huddle_meta` all go through
    the plain client, so `_set_driver`'s is the first. A caller that opens a pipeline
    before the one under test would need the filter.
    """
    state = {"fired": False}
    orig_pipeline = r.pipeline

    def pipeline(*a, **kw):
        pipe = orig_pipeline(*a, **kw)
        orig_multi = pipe.multi

        def multi(*aa, **kk):
            if not state["fired"]:
                state["fired"] = True
                action()
            return orig_multi(*aa, **kk)

        pipe.multi = multi
        return pipe

    monkeypatch.setattr(r, "pipeline", pipeline)
    return state


def test_real_redis_take_aborts_on_a_challenge_raised_inside_the_watched_window(
    bus_module, ns, redis_url, monkeypatch,
):
    """#112: `k_penchal` is in `_set_driver`'s WATCH, and that is load-bearing.

    The absent-driver take passes `challenge_expect` and DELETEs the challenge key in
    its MULTI. A rival challenge recorded after the take has read the old one but before
    EXEC is invisible to that compare -- only the WATCH can catch it. Without the WATCH
    the take's MULTI silently destroys the rival challenge and the challenger waits
    forever on a driver who was never told they were challenged.

    Drop `chal_key` from the `pipe.watch(...)` line and this test goes red while the
    rest of the suite stays green.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    rival = bus_module.connect(redis_url)

    try:
        # Unique holder with NO presence anywhere -> the absent-driver path.
        driver = f"driver-{uuid.uuid4().hex}"
        _seed_huddle(r, bus_module, issue, [driver, "agent-a", "agent-c"], driver)
        r.set(bus_module.k_pen(issue), driver, ex=60)
        mine = json.dumps({"challenger": "agent-a", "reason": "d is gone"})
        r.set(bus_module.k_penchal(issue), mine)
        theirs = json.dumps({"challenger": "agent-c", "reason": "i want it too"})

        state = _fire_once_on_multi(
            monkeypatch, r, lambda: rival.set(bus_module.k_penchal(issue), theirs))

        rc = bus_module.cmd_pen_take(
            r, ns(issue=issue, as_agent="agent-a", reason="d is gone",
                  room="integration"))

        assert state["fired"], "the rival challenge never landed -- race not exercised"
        assert rc == 1, "the take committed against a challenge it never saw"
        assert verify.get(bus_module.k_pen(issue)) == driver, "pen moved anyway"
        assert verify.get(bus_module.k_penchal(issue)) == theirs, \
            "the rival challenge was silently deleted by the take's MULTI"
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_take_with_no_prior_challenge_aborts_when_one_is_raised(
    bus_module, ns, redis_url, monkeypatch,
):
    """#112: `challenge_expect=None` means "I expect NO challenge", not "skip the check".

    The absent-driver take reads the challenge key before the transaction; when nothing
    is recorded it passes `challenge_expect=None`. That used to SKIP the compare
    entirely, so a rival challenge raised inside the watched window was deleted by the
    take's MULTI without ever being read -- the same silent-destruction bug as the
    non-empty case, on the path that is arguably more likely (no challenge is the
    common state). Making `challenge_expect` required, with `_ANY_CHALLENGE` as the
    explicit "any value is moot" opt-out, turns `None` back into a real expectation.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    rival = bus_module.connect(redis_url)

    try:
        driver = f"driver-{uuid.uuid4().hex}"
        _seed_huddle(r, bus_module, issue, [driver, "agent-a", "agent-c"], driver)
        r.set(bus_module.k_pen(issue), driver, ex=60)
        # NO challenge recorded -> cmd_pen_take passes challenge_expect=None.
        theirs = json.dumps({"challenger": "agent-c", "reason": "i want it too"})

        state = _fire_once_on_multi(
            monkeypatch, r, lambda: rival.set(bus_module.k_penchal(issue), theirs))

        rc = bus_module.cmd_pen_take(
            r, ns(issue=issue, as_agent="agent-a", reason="d is gone",
                  room="integration"))

        assert state["fired"], "the rival challenge never landed -- race not exercised"
        assert rc == 1, "the take committed against a challenge it never saw"
        assert verify.get(bus_module.k_pen(issue)) == driver, "pen moved anyway"
        assert verify.get(bus_module.k_penchal(issue)) == theirs, \
            "the rival challenge was silently deleted by the take's MULTI"
    finally:
        _delete_issue_keys(verify, bus_module, issue)
