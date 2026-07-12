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
# The sites above lose a participant by OVERWRITING the blob. This one loses one by
# never re-reading it: the gate ran on a snapshot taken before the join, and the
# close then deleted the very state the joiner needed in order to sign.


PRESENT = ("agent-a", "agent-b", "agent-c")


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

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        # The shared branch is not what's under test; pin the tip so the gate turns
        # purely on who has signed it.
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: tip)
        # Closer + B have both signed AT THE TIP: without C the gate passes, so a
        # close that ignores C returns 0 rather than failing for some unrelated reason.
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        # An ABSENT participant does not hold the gate, so C must be present — else
        # this would pass for the wrong reason (C skipped, not C checked).
        for agent in PRESENT:
            r.set(bus_module.k_presence("integration", agent), "now", ex=60)

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
        verify.delete(*(bus_module.k_presence("integration", a) for a in PRESENT))


def test_real_redis_close_still_closes_when_the_gate_is_genuinely_met(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """The WATCH/retry must not break the ordinary close — without this, the test
    above would also pass against a `cmd_huddle_close` that never closes anything."""
    issue = _issue_id()
    tip = "c" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: tip)
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        for agent in ("agent-a", "agent-b"):
            r.set(bus_module.k_presence("integration", agent), "now", ex=60)

        assert bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False)) == 0

        # Lock released and every huddle key gone — the close's whole contract.
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_lock(issue)) is None
        assert verify.get(bus_module.k_signoff(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        verify.delete(*(bus_module.k_presence("integration", a)
                        for a in ("agent-a", "agent-b")))
