import os
import uuid

import pytest


pytestmark = pytest.mark.integration


@pytest.fixture
def redis_url():
    url = os.environ.get("BUS_INTEGRATION_REDIS_URL")
    if not url:
        pytest.skip("set BUS_INTEGRATION_REDIS_URL to run Redis integration tests")
    return url


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
