import json
import os
import uuid
from datetime import datetime, timedelta, timezone
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


def _fire_once_after_pipeline_execute(monkeypatch, r, action):
    state = {"fired": False}
    orig_pipeline = r.pipeline

    def pipeline(*a, **kw):
        pipe = orig_pipeline(*a, **kw)
        real_execute = pipe.execute

        def execute(*execute_args, **execute_kw):
            result = real_execute(*execute_args, **execute_kw)
            if not state["fired"]:
                state["fired"] = True
                action()
            return result

        pipe.execute = execute
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


# ---- #94: a CAS/existence result the caller drops on the floor -----------------
#
# The real-server half. fakeredis can reproduce the STATE a race leaves behind, but
# the guarantees claimed here are Redis's own — a real Lua CAS, and a real
# WATCH/MULTI. Both of these also FAIL against the pre-fix bus, which is what makes
# them evidence rather than decoration.


def test_real_redis_pen_pass_into_a_huddle_closed_mid_flight_leaves_no_pen(
    bus_module, ns, redis_url, monkeypatch, capsys,
):
    """#94 item 2, on a real server.

    Pre-fix, `pen pass` discarded `_set_driver`'s False and ran its blind
    `r.set(k_pen(...))` anyway — resurrecting a pen key for a huddle that had just
    closed (close is the only path that deletes k_pen, so nothing would ever reap it)
    and announcing a handoff into it.

    The concurrent writer deletes the huddle keys directly rather than calling
    `cmd_huddle_close`: the Redis-side mutation is identical (close DELs exactly these
    keys) and driving the real command would fire its gh side effects at the live repo
    from a test.
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
        assert "is gone" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_pen_pass_close_after_driver_write_leaves_no_orphan_pen(
    bus_module, ns, redis_url, monkeypatch,
):
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        monkeypatch.setattr(bus_module, "cmd_pen_checkpoint", lambda _r, _args: 0)
        state = _fire_once_after_pipeline_execute(
            monkeypatch, r, lambda: _delete_issue_keys(closer, bus_module, issue))

        assert bus_module.cmd_pen_pass(
            r, ns(issue=issue, as_agent="agent-d", to="agent-a", room="integration")) == 0

        assert state["fired"], "the close-after-write race was not exercised"
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_pen(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_pen_take_unheld_close_after_driver_write_leaves_no_orphan_pen(
    bus_module, ns, redis_url, monkeypatch,
):
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "")
        state = _fire_once_after_pipeline_execute(
            monkeypatch, r, lambda: _delete_issue_keys(closer, bus_module, issue))

        assert bus_module.cmd_pen_take(
            r, ns(issue=issue, as_agent="agent-a", reason="driver left",
                  room="integration")) == 0

        assert state["fired"], "the close-after-write race was not exercised"
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_pen(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_pen_take_force_close_after_driver_write_leaves_no_orphan_pen(
    bus_module, ns, redis_url, monkeypatch,
):
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)
    challenge = {"challenger": "agent-a", "reason": "stale",
                 "ts": (datetime.now(timezone.utc)
                        - timedelta(seconds=bus_module.PEN_TAKE_GRACE + 5)).isoformat()}

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        r.set(bus_module.k_penchal(issue), json.dumps(challenge), ex=60)
        monkeypatch.setattr(bus_module, "_holder_present", lambda _r, _holder: False)
        state = _fire_once_after_pipeline_execute(
            monkeypatch, r, lambda: _delete_issue_keys(closer, bus_module, issue))

        assert bus_module.cmd_pen_take(
            r, ns(issue=issue, as_agent="agent-a", reason="stale",
                  room="integration")) == 0

        assert state["fired"], "the close-after-write race was not exercised"
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_pen(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_drain_clears_the_driver_when_its_own_del_reply_was_lost(
    bus_module, ns, redis_url, monkeypatch,
):
    """#94 item 1, on a real server: the real WATCH/MULTI CAS inside `_set_driver` is
    what has to save us.

    `retry_on_error` (B3, #83) re-sends an EVAL whose reply was lost; the re-send's
    GET sees the key its OWN first attempt deleted, so the CAS reports 0 for a delete
    that landed. Gating the driver-clear on that 0 released the pen but left
    `meta['driver']` naming the drained agent.

    Only `compare_delete` is stubbed — to the exact state double execution leaves
    behind (key gone, result 0). Everything downstream is the real thing.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-d", "agent-a"], "agent-d")
        r.set(bus_module.k_pen(issue), "agent-d", ex=60)
        monkeypatch.setattr(bus_module, "_unpushed_pen_issues", lambda *_a: [])

        def lost_reply_delete(client, key, value):
            assert (key, value) == (bus_module.k_pen(issue), "agent-d")
            client.delete(key)   # attempt 1 landed...
            return 0             # ...and the re-sent attempt found it already gone

        monkeypatch.setattr(bus_module, "compare_delete", lost_reply_delete)

        assert bus_module.cmd_drain(
            r, ns(as_agent="agent-d", force=False, room="integration")) == 0

        assert verify.get(bus_module.k_pen(issue)) is None
        meta = json.loads(verify.get(bus_module.k_huddle(issue)))
        assert meta["driver"] == "", "drain left a stale driver on a pen it released"
        assert meta["participants"] == ["agent-d", "agent-a"]  # nothing else disturbed
    finally:
        _delete_issue_keys(verify, bus_module, issue)
