"""Guard: the pinned dev environment must actually have EVAL.

fakeredis imports its scripting mixin under a try/except ImportError, so without
the optional lupa package it registers no EVAL command at all and an eval raises
"unknown command". The fakeredis[lua] extra in requirements-dev.in pulls lupa in.

Every CAS script the bus runs goes through EVAL, and since #116 the suite runs
those scripts for real rather than mirroring them in Python -- so losing lupa
would already take the CAS tests down with it. This is the canary that says WHY:
one fast, obviously-named failure instead of a scatter of confusing ones.
"""

import fakeredis


def test_fakeredis_evaluates_lua():
    r = fakeredis.FakeRedis()

    assert r.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "k", "v")
    assert r.get("k") == b"v"
