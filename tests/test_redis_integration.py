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
    """Seed a huddle as `cmd_huddle_open` would leave it.

    The pen goes to the DRIVER because that is the real invariant, not a convenience:
    `cmd_huddle_open` writes `driver` and `pen_holder` in one CAS
    (`compare_set_huddle_meta`) and `_set_driver` rewrites both in one MULTI, so
    `k_pen == meta["driver"]` in every live huddle. Seeding a huddle with no pen at
    all modelled a state the bus cannot produce — and a close test running against it
    was silently exercising a huddle nobody was driving.

    A blank `driver` is the one real pen-less state and seeds NO pen key, because that
    is what produces it: `_release_pen_driver` deletes `k_pen` and blanks `driver` in
    one transaction, so a drained huddle has neither. Writing an empty-string pen key
    instead would be a state the bus cannot reach, and `_take_unheld_pen` — which
    CAS-expects the key to be ABSENT — would refuse to take a pen nobody holds.
    """
    session = f"huddle:issue-{issue}:session"
    r.set(bus_module.k_lock(issue), session, ex=60)
    r.set(bus_module.k_huddle(issue), json.dumps({
        "issue": issue, "opener": participants[0], "participants": list(participants),
        "driver": driver, "branch": bus_module.huddle_branch(issue), "base": "dev",
        "base_commit": "0" * 40, "session": session, "status": "open",
        "created_at": "2026-07-11T00:00:00+00:00"}), ex=60)
    if driver:
        r.set(bus_module.k_pen(issue), driver, ex=60)


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


# --- the pen invariant: the shared tip is pinned, not polled (#95) ---------------
#
# The gate signs off a SPECIFIC tip, and `_shared_tip` lives in git, not Redis — no
# WATCH can freeze it. #95 is that gap: the gate reads T1, a push lands, and the close
# commits at T2, which nobody signed. The fix does not try to DETECT that push; it
# makes it unreachable, by requiring the closer to already hold the pen. Only
# `cmd_pen_checkpoint` and `cmd_huddle_recover` push the shared branch, and both are
# `_require_pen`-gated, so a pen-holding closer is the sole agent who could move the
# tip — and it does not push while it is closing.


def test_real_redis_close_refuses_when_the_closer_does_not_hold_the_pen(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """#95, exactly as filed: the driver pushes while a PEER closes.

    The interleave is ordinary, not exotic. agent-b drives (holds the pen); agent-a
    closes. Both signed at T1, so the gate is legitimately satisfied for the tip it
    reads. agent-b then checkpoints — a bare `git push`, which writes NO Redis key, so
    no WATCH fires and #92's transaction is untouched. The huddle closes at T2.

    Pre-fix this returned 0 and destroyed the huddle: closed at a tip nobody signed,
    defeating the property `cmd_signoff` explicitly claims ("any later checkpoint makes
    them stale, so the done-gate can't be gamed by signing good code then pushing a
    poison commit"). Post-fix agent-a never reaches the gate — it does not hold the pen.

    `_shared_tip` is stubbed to a COUNTER, not to a tip that flips. A flip would be
    theatre: `donegate` reads the tip exactly once per pass, so a second, moved value is
    never returned on any path — pre-fix the close reads T1, passes, and commits, and
    the tip it commits at is unsigned by construction, not because the stub said so. The
    counter asserts the property that actually distinguishes fixed from broken: the gate
    never runs at all, because the pen check refuses ahead of it.
    """
    issue = _issue_id()
    tip = "a" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    presence = []

    try:
        # agent-b drives and therefore holds the pen; agent-a is the closing peer.
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-b")
        assert verify.get(bus_module.k_pen(issue)) == "agent-b"
        # Everyone signed at the tip: the gate PASSES on what it reads, so a close that
        # ignores the pen returns 0 — it does not fail for some unrelated reason.
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration", ("agent-a", "agent-b"))

        tips = {"n": 0}

        def count_gate_tip_reads(_issue):
            tips["n"] += 1
            return tip

        monkeypatch.setattr(bus_module, "_shared_tip", count_gate_tip_reads)

        rc = bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False))

        assert rc == 1, "a non-pen-holder closed the huddle — the tip is unpinned (#95)"
        # A refused close must leave EVERYTHING intact: pre-fix the huddle was gone.
        assert verify.get(bus_module.k_huddle(issue)) is not None, "the huddle was destroyed"
        assert verify.get(bus_module.k_lock(issue)) is not None
        assert verify.get(bus_module.k_signoff(issue)) is not None
        assert verify.get(bus_module.k_pen(issue)) == "agent-b", "the close moved the pen"
        # The gate must never have run: the pen check precedes it, so the tip that
        # could have been closed at was never even read.
        assert tips["n"] == 0, "the gate read the tip before checking the pen"
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        if presence:
            verify.delete(*presence)


def _assert_close_refuses_when_the_pen_moves_mid_gate(
    bus_module, ns, redis_url, monkeypatch, move_the_pen,
):
    """agent-a holds the pen and passes the pen check; `move_the_pen(issue)` then sends
    it to agent-b from inside the gate window, and the close must refuse.

    `move_the_pen` fires from the stubbed `_shared_tip`, which `donegate` calls INSIDE
    the watched window and AFTER the pen check — precisely the gate->EXEC gap where a
    pen move must not go unnoticed. It runs on its own connection, so it is a genuine
    concurrent writer rather than a re-entrant call on the closing client.

    Callers differ only in HOW the pen moves, which is the whole point: each way it can
    move exercises a different key in the WATCH set.
    """
    issue = _issue_id()
    tip = "e" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    presence = []

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration", ("agent-a", "agent-b"))

        fired = {"n": 0}

        def tip_then_move_the_pen(_issue):
            if not fired["n"]:
                fired["n"] = 1
                move_the_pen(issue)
            return tip

        monkeypatch.setattr(bus_module, "_shared_tip", tip_then_move_the_pen)

        rc = bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False))

        assert fired["n"], "the pen never moved — the race was not exercised"
        assert rc == 1, "the close committed a gate verdict it no longer held the pen for"
        assert verify.get(bus_module.k_huddle(issue)) is not None, "the huddle was destroyed"
        assert verify.get(bus_module.k_pen(issue)) == "agent-b"
    finally:
        _delete_issue_keys(verify, bus_module, issue)
        if presence:
            verify.delete(*presence)


def test_real_redis_close_regates_when_the_pen_is_taken_inside_the_gate_window(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """The closer holds the pen at the gate, then loses it before EXEC.

    This is the residual seam the precondition alone does not cover: agent-a passes the
    pen check, and the pen then moves to agent-b mid-gate (an absent-driver takeover, a
    pass, a deny). agent-b can now push, so agent-a's gate verdict is no longer pinned.

    `_set_driver` writes the pen and the metadata blob in ONE transaction, so the move
    aborts our EXEC through the `key` WATCH we already hold. The retry re-reads the pen,
    finds it is no longer ours, and refuses. Without the re-check inside the loop the
    retry would sail through on a verdict it no longer owns.
    """
    taker = bus_module.connect(redis_url)

    def take_the_pen(issue):
        assert bus_module._set_driver(
            taker, issue, "agent-b", pen_to="agent-b", pen_expect="agent-a",
            expected_session=f"huddle:issue-{issue}:session")

    _assert_close_refuses_when_the_pen_moves_mid_gate(
        bus_module, ns, redis_url, monkeypatch, take_the_pen)


def test_real_redis_close_aborts_on_a_pen_write_that_does_not_touch_the_metadata(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """Watching `k_pen` is what keeps the close honest if a future pen mover forgets
    the metadata blob.

    Today every pen mover — `_set_driver`, `_release_pen_driver`,
    `compare_set_huddle_meta` — writes `k_pen` and the metadata blob in the SAME
    transaction, so the `key` WATCH already aborts the close on any pen move and the
    `pen_key` WATCH catches nothing the close does not already catch. That was verified,
    not assumed: dropping `pen_key` from the WATCH set leaves every other close test
    green.

    Which is exactly why this test exists. That redundancy is not a property of the
    close — it is a property of three OTHER functions, and nothing makes them keep it. A
    new pen mover that writes only `k_pen` would silently unpin the tip: the close would
    hold a gate verdict for a pen it no longer owns, and no existing test would notice.
    So the bare write below is deliberate — it is not modelling a command the bus has
    today, it is modelling the one it must not be allowed to grow.
    """
    poisoner = bus_module.connect(redis_url)

    def move_the_pen_and_nothing_else(issue):
        # No metadata write: the abort can only come from the WATCH on k_pen itself.
        poisoner.set(bus_module.k_pen(issue), "agent-b", ex=60)

    _assert_close_refuses_when_the_pen_moves_mid_gate(
        bus_module, ns, redis_url, monkeypatch, move_the_pen_and_nothing_else)


def test_real_redis_force_close_still_needs_no_pen(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """`--force` skips the gate, and with it the pen check — the escape hatch survives.

    The pen exists to pin the tip the GATE reads. With no gate there is no tip to
    protect, so forcing a stuck huddle down must not first require prising the pen out
    of a vanished driver. Without this test the fix could quietly strand every huddle
    whose driver is gone — the exact failure the override exists to prevent.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)

    try:
        # agent-b holds the pen and has signed nothing: gate would refuse, pen would too.
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-b")
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: "f" * 40)

        assert bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=True)) == 0

        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_lock(issue)) is None
        assert verify.get(bus_module.k_pen(issue)) is None
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_close_leaves_the_label_to_a_session_that_claims_the_issue_first(
    bus_module, ns, redis_url, monkeypatch, no_github,
):
    """#99: the atomic teardown frees the issue before the close reaches GitHub.

    The close's EXEC releases the lock and destroys the huddle together (#92's fix). For
    the length of the gh round-trip the issue is therefore UNLOCKED, and a `huddle open`
    racing into that window claims it legitimately: fresh lock, fresh meta,
    status:claimed. The close then posts status:pr-open over the top — last write wins —
    and comments "Huddle closed" on a huddle that just opened. Redis stays perfectly
    consistent; only GitHub ends up lying.

    The fix re-reads the lock after EXEC and stands down if anyone holds it. The racing
    claim is injected at that very read, which is the window's real shape: the point is
    that the close must LOOK before it writes, not that it can win a footrace.
    """
    issue = _issue_id()
    tip = "7" * 40
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    opener = bus_module.connect(redis_url)
    presence = []
    labels = []

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: tip)
        r.set(bus_module.k_signoff(issue), json.dumps({"agent-a": tip, "agent-b": tip}))
        presence = _seed_presence(r, bus_module, "integration", ("agent-a", "agent-b"))

        monkeypatch.setattr(bus_module, "set_status_label",
                            lambda *a, **kw: labels.append(a))

        lock_key = bus_module.k_lock(issue)
        new_session = f"huddle:issue-{issue}:session-2"
        real_get, raced = r.get, {"n": 0}

        def claim_the_issue_as_we_look(key, *a, **kw):
            # Fire on the post-EXEC lock read: a new huddle has just taken the issue.
            if key == lock_key and not raced["n"]:
                raced["n"] = 1
                opener.set(lock_key, new_session, ex=60)
            return real_get(key, *a, **kw)

        monkeypatch.setattr(r, "get", claim_the_issue_as_we_look)

        assert bus_module.cmd_huddle_close(
            r, ns(issue=issue, as_agent="agent-a", room="integration", force=False)) == 0

        assert raced["n"], "no session claimed the issue — the race was not exercised"
        assert labels == [], "the close set status:pr-open over a freshly-claimed huddle"
        # The new session's claim must survive untouched — the close may not reap it.
        assert verify.get(lock_key) == new_session, "the close clobbered the new claim"
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


# ---- #115: a block/sign-off that outlives the close it raced ------------------
#
# `_mutate_huddle_json` ends in an unconditional SET that starts from `default` when the
# key is absent, so it RESURRECTS a key a concurrent close just deleted. Watching
# only the value key cannot see that — the close deletes the HUDDLE, not this key,
# so nothing the WATCH covers ever changes and EXEC commits. The leaked key has no
# TTL, close is its only deleter, and `huddle open` does not clear it, so the NEXT
# huddle on the issue is born blocked by an objection against code that no longer
# exists.
#
# These need a real server: the guarantee under test is Redis's own WATCH/MULTI
# abort semantics, and each one also FAILS against the pre-fix bus, which is what
# makes them evidence rather than decoration.


def test_real_redis_a_block_racing_the_close_it_lands_behind_leaves_no_block(
    bus_module, ns, redis_url, monkeypatch, capsys,
):
    """The window is between `cmd_signoff`'s meta read (huddle still live, so the
    participant check passes) and its `_mutate_huddle_json` write. A close committing in
    there used to leave a fully-formed block behind on a dead huddle."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: _delete_issue_keys(closer, bus_module, issue))

        rc = bus_module.cmd_signoff(r, ns(
            issue=issue, as_agent="agent-b", room="integration",
            block="found a data-loss bug, do NOT merge"))

        assert state["fired"], "the concurrent close never landed — race not exercised"
        assert rc == 1
        assert verify.get(bus_module.k_huddle(issue)) is None
        assert verify.get(bus_module.k_block(issue)) is None, \
            "a block was resurrected onto a huddle that had already closed"
        assert "closed while you were blocking" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_a_signoff_racing_the_close_it_lands_behind_leaves_no_signoff(
    bus_module, ns, redis_url, monkeypatch, capsys,
):
    """Same window, the sign-off half. A leaked k_signoff is the symmetric poison:
    the next huddle starts with sign-offs from a session nobody in it took part in."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        monkeypatch.setattr(bus_module, "_shared_tip", lambda _issue: "e" * 40)
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue,
            lambda: _delete_issue_keys(closer, bus_module, issue))

        rc = bus_module.cmd_signoff(r, ns(
            issue=issue, as_agent="agent-b", room="integration", block=None))

        assert state["fired"], "the concurrent close never landed — race not exercised"
        assert rc == 1
        assert verify.get(bus_module.k_signoff(issue)) is None, \
            "a sign-off was resurrected onto a huddle that had already closed"
        assert "closed while you were signing off" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_a_block_in_flight_across_a_reopen_does_not_poison_the_new_huddle(
    bus_module, ns, redis_url, monkeypatch, capsys,
):
    """The half that clearing the keys at `huddle open` could NOT have fixed.

    Here the write lands after the next huddle is already OPEN, so there is no
    later open left to mop it up: the block would sit in k_block, `donegate` would
    read it (it reads k_block unconditionally), and the new huddle could not close
    until someone unblocked an objection raised against a session that is gone.
    Only binding the write to the session it was raised in refuses this.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    other = bus_module.connect(redis_url)

    def close_then_reopen():
        _delete_issue_keys(other, bus_module, issue)
        # a brand-new huddle on the same issue: _seed_huddle mints its own session
        _seed_huddle(other, bus_module, issue, ["agent-c"], "agent-c")
        other.set(bus_module.k_huddle(issue), json.dumps({
            **json.loads(other.get(bus_module.k_huddle(issue))),
            "session": f"huddle:issue-{issue}:SECOND"}), ex=60)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        state = _fire_once_on_huddle_read(
            monkeypatch, r, bus_module, issue, close_then_reopen)

        rc = bus_module.cmd_signoff(r, ns(
            issue=issue, as_agent="agent-b", room="integration",
            block="stale objection from the PREVIOUS huddle"))

        assert state["fired"], "the reopen never landed — race not exercised"
        assert rc == 1
        # the new huddle survives, and is NOT born blocked
        assert json.loads(verify.get(bus_module.k_huddle(issue)))["session"] \
            == f"huddle:issue-{issue}:SECOND"
        assert verify.get(bus_module.k_block(issue)) is None, \
            "the new huddle was born blocked by the previous session's objection"
        assert "closed while you were blocking" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_unblock_with_no_huddle_leaves_no_key_behind(
    bus_module, ns, redis_url, capsys,
):
    """`cmd_unblock` never read the huddle at all, so its mutate started from the
    `[]` default and SET it back — reporting "you have no block" while leaving a
    live, TTL-less k_block on an issue with no huddle."""
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)

    try:
        assert verify.get(bus_module.k_huddle(issue)) is None   # no huddle, by construction

        rc = bus_module.cmd_unblock(
            r, ns(issue=issue, as_agent="agent-a", room="integration"))

        assert rc == 1
        assert verify.get(bus_module.k_block(issue)) is None, \
            "unblock created a block key on an issue that has no huddle"
        assert "no huddle" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)


def test_real_redis_a_close_inside_the_watched_window_aborts_the_block_write(
    bus_module, ns, redis_url, monkeypatch, capsys,
):
    """The close lands INSIDE the guard's own WATCH window — after its MGET read
    the huddle as live, before EXEC. The other tests fire the close at the
    command's first meta read, so the guard re-reads a huddle that is ALREADY
    gone; none of them exercise the WATCH-abort-and-retry path, which is where a
    concurrency bug would actually hide.

    This is also the case that proves WATCHing the huddle key is load-bearing
    rather than belt-and-suspenders. It is tempting to think WATCHing `k_block`
    alone suffices, since the close deletes k_block too — but here k_block DOES
    NOT EXIST YET (this is the first block on the huddle), and Redis only signals
    a WATCH when a key is really modified. DEL of an absent key is a no-op, so a
    k_block-only WATCH would NOT trip, EXEC would commit, and the block would be
    resurrected onto the closed huddle — exactly the #115 bug. Only the WATCH on
    the huddle key (which the close really does delete) aborts this.
    """
    issue = _issue_id()
    r = bus_module.connect(redis_url)
    verify = bus_module.connect(redis_url)
    closer = bus_module.connect(redis_url)

    try:
        _seed_huddle(r, bus_module, issue, ["agent-a", "agent-b"], "agent-a")
        assert verify.get(bus_module.k_block(issue)) is None, "k_block must start ABSENT"

        # Fire the close on the guard's own read: `_mutate_huddle_json` reads both
        # keys with ONE mget inside the watched window, so hook mget (not get).
        fired = {"n": 0}
        orig_pipeline = r.pipeline

        def pipeline(*a, **kw):
            pipe = orig_pipeline(*a, **kw)
            orig_mget = pipe.mget

            def mget(*keys, **kw2):
                val = orig_mget(*keys, **kw2)   # reads the huddle as still LIVE
                if not fired["n"]:
                    fired["n"] = 1
                    _delete_issue_keys(closer, bus_module, issue)   # close commits
                return val

            pipe.mget = mget
            return pipe

        monkeypatch.setattr(r, "pipeline", pipeline)

        rc = bus_module.cmd_signoff(r, ns(
            issue=issue, as_agent="agent-b", room="integration",
            block="raised just as the close committed"))

        assert fired["n"], "the close never landed inside the window — race not exercised"
        assert rc == 1
        assert verify.get(bus_module.k_block(issue)) is None, \
            "the WATCH did not abort: a block was resurrected onto a closed huddle"
        assert "closed while you were blocking" in capsys.readouterr().err
    finally:
        _delete_issue_keys(verify, bus_module, issue)
