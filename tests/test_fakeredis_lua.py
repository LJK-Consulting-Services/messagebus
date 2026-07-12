"""Guard: the pinned dev environment must actually have EVAL.

fakeredis imports its scripting mixin under a try/except ImportError, so without
the optional lupa package it registers no EVAL command at all and an eval raises
"unknown command". The unit suite relies on fakeredis running the bus's real Lua,
including scripts queued inside pipelines.

The fakeredis[lua] extra in requirements-dev.in pulls lupa in; drop the extra, or
regenerate the lockfile without it, and this guard goes red before the broader
unit suite can fall back to a machine-dependent fake Redis.
"""

import fakeredis


def test_fakeredis_evaluates_lua():
    r = fakeredis.FakeRedis()

    assert r.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "k", "v")
    assert r.get("k") == b"v"


def test_fakeredis_evaluates_lua_queued_in_pipeline():
    r = fakeredis.FakeRedis()

    pipe = r.pipeline()
    pipe.multi()
    pipe.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "k", "v")

    assert pipe.execute()
    assert r.get("k") == b"v"
