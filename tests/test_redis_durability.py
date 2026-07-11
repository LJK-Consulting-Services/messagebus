"""B3 (#83) — the durability + restart-survival acceptance, against a REAL Redis.

These tests cannot use the CI service container: it has no AOF and we are not
allowed to SIGKILL it. Each test therefore spawns its own throwaway
`redis-server` on a private port with its own data dir, kills it outright, and
restarts it against the SAME append log.

Why `--appendfsync always` here when production ships `everysec`: everysec bounds
loss to ~1s, which is the right trade for a coordination log but makes a
kill-immediately-after-write test race the fsync. `always` removes the timing
flake while exercising the identical AOF load path on restart.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import redis


pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]

# How long the server stays dead in the end-to-end restart test. Must exceed
# redis-py's OWN retry budget so the outage genuinely reaches our reconnect
# wrapper, and must sit inside the wrapper's backoff schedule so it still
# reconnects. That budget: Retry(ExponentialBackoff(), 3) = 4 attempts (the
# initial one + 3 retries), sleeping compute(1..3) = 16+32+64ms ≈ 112ms nominal
# (measured ~129ms). 1.5s clears it comfortably.
OUTAGE_SECONDS = 1.5


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_up(url, bus_module, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return bus_module.connect(url)
        except Exception:  # noqa: BLE001 - still booting
            time.sleep(0.05)
    raise AssertionError(f"redis at {url} never came up")


class RedisUnderTest:
    """A private redis-server we are allowed to kill."""

    def __init__(self, data_dir, port, bus_module):
        self.data_dir, self.port, self._bus = data_dir, port, bus_module
        self.url = f"redis://127.0.0.1:{port}/0"
        self.proc = None

    def start(self):
        self.proc = subprocess.Popen(
            ["redis-server", "--port", str(self.port), "--bind", "127.0.0.1",
             "--dir", str(self.data_dir), "--appendonly", "yes",
             "--appendfsync", "always", "--save", ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return _wait_until_up(self.url, self._bus)

    def kill(self):
        self.proc.kill()   # SIGKILL: a crash, not a clean shutdown flush
        self.proc.wait(timeout=10)


@pytest.fixture
def redis_under_test(bus_module, tmp_path):
    if shutil.which("redis-server") is None:
        if os.environ.get("GITHUB_ACTIONS"):
            pytest.fail("redis-server must be installed in CI for the durability tests")
        pytest.skip("redis-server not on PATH; skipping real kill+restart tests")
    server = RedisUnderTest(tmp_path, _free_port(), bus_module)
    server.start()
    try:
        yield server
    finally:
        if server.proc.poll() is None:
            server.kill()


def test_aof_carries_coordination_state_across_a_kill(bus_module, redis_under_test):
    """The state that actually matters is not the message log — it's the live
    coordination tokens. A volatile Redis silently hands out a claim someone
    already holds and drops the write pen on the floor."""
    r = bus_module.connect(redis_under_test.url)
    assert bus_module.redis_persistence(r) == "aof"

    r.set(bus_module.k_lock(83), "claude-2", ex=3600)
    r.set(bus_module.k_pen(83), "claude-2")
    r.set(bus_module.k_huddle(83), '{"issue": 83, "status": "open"}')
    r.set(bus_module.k_cursor("main", "claude-2"), "5-0")
    msg_id = r.xadd(bus_module.k_stream("main"),
                    bus_module.make_fields("alice", "all", "survive me"))

    redis_under_test.kill()
    r2 = redis_under_test.start()

    assert r2.get(bus_module.k_lock(83)) == "claude-2"     # claim not handed out twice
    assert r2.get(bus_module.k_pen(83)) == "claude-2"      # single-writer invariant holds
    assert r2.get(bus_module.k_huddle(83)) == '{"issue": 83, "status": "open"}'
    assert r2.get(bus_module.k_cursor("main", "claude-2")) == "5-0"
    assert r2.ttl(bus_module.k_lock(83)) > 0               # TTL survives, not resurrected forever
    entries = r2.xrange(bus_module.k_stream("main"))
    assert [mid for mid, _ in entries] == [msg_id]
    assert entries[0][1]["body"] == "survive me"


def test_wait_rides_out_a_redis_restart_end_to_end(bus_module, redis_under_test, events_of):
    """The headline B3 acceptance, run as a real process: an agent blocked in
    `bus wait` must reconnect across a server restart and still deliver the
    message published afterwards — instead of dying with a ConnectionError.

    The outage is held open for OUTAGE_SECONDS on purpose. redis-py's own retry
    budget is ~112ms (see the constant), so a fast restart is absorbed entirely
    inside the client and never reaches our reconnect wrapper — measured: it
    still delivers the message, but emits no event. Staying down past that budget
    is what forces the wrapper — and therefore EV_RECONNECT — to be the thing
    under test, rather than a race we'd sometimes win for free.
    """
    r = bus_module.connect(redis_under_test.url)
    r.set(bus_module.k_cursor("main", "bob"), "0-0")

    waiter = subprocess.Popen(
        [sys.executable, str(ROOT / "bus"), "--url", redis_under_test.url,
         "wait", "--as", "bob", "--timeout", "60"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        # let it get as far as registering presence, i.e. it is really blocking
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if r.get(bus_module.k_presence("main", "bob")):
                break
            assert waiter.poll() is None, "waiter exited before it blocked"
            time.sleep(0.05)
        else:
            raise AssertionError("waiter never registered presence")

        redis_under_test.kill()
        time.sleep(OUTAGE_SECONDS)  # outlast redis-py's own ~56ms retry budget
        assert waiter.poll() is None, "waiter died during the outage instead of reconnecting"
        r2 = redis_under_test.start()
        r2.xadd(bus_module.k_stream("main"),
                bus_module.make_fields("alice", "bob", "hello after the restart"))

        out, err = waiter.communicate(timeout=90)
    finally:
        if waiter.poll() is None:
            waiter.kill()
            waiter.communicate(timeout=10)

    assert waiter.returncode == bus_module.RC_DELIVERED, f"stderr: {err}"
    assert "hello after the restart" in out  # the message was NOT lost

    # and the reconnect is observable, not silent
    reconnects = events_of(r2, bus_module.EV_RECONNECT)
    assert len(reconnects) == 1
    assert reconnects[0]["fields"] == {"agent": "bob", "room": "main", "cmd": "wait"}


def test_connect_retries_an_injected_connection_error(bus_module, redis_under_test):
    """AC: a ConnectionError on a command retries and succeeds instead of crashing.

    It drives the very Retry object the live client carries — this is what
    redis-py runs every command through — so it fails the moment connect() stops
    configuring retries (verified by mutation: with the default `retries=0`, the
    first injected error propagates and this test goes red).
    """
    r = bus_module.connect(redis_under_test.url)
    conn = r.connection_pool.make_connection()
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise redis.exceptions.ConnectionError("blip")
        return "delivered"

    assert conn.retry.call_with_retry(flaky, lambda _e: None) == "delivered"
    assert len(attempts) == 3  # rode out two failures rather than raising on the first


def test_send_survives_the_server_killing_our_connection(bus_module, ns,
                                                         redis_under_test, capsys):
    """Regression guard for the blip case end-to-end. Note this passes on redis-py's
    baseline too — the pool transparently reopens a socket the server closed, so it
    is NOT evidence about our retry config; test_connect_retries_an_injected_
    connection_error is. Kept because a send that dies here would still be a bug."""
    r = bus_module.connect(redis_under_test.url)
    room = "blip"
    r.xadd(bus_module.k_stream(room), bus_module.make_fields("alice", "all", "warm"))
    r.execute_command("CLIENT", "KILL", "TYPE", "normal", "SKIPME", "no")

    rc = bus_module.cmd_send(r, ns(room=room, frm="alice", to="bob", topic=None,
                                   reply_to="", kind="msg", body="through the blip"))
    capsys.readouterr()

    assert rc == 0
    bodies = [f["body"] for _, f in r.xrange(bus_module.k_stream(room))]
    assert "through the blip" in bodies
