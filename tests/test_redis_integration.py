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
