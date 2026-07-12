"""Guard: the pinned dev environment must actually have EVAL.

fakeredis imports its scripting mixin under a try/except ImportError, so without
the optional lupa package it registers no EVAL command at all and an eval raises
"unknown command". That is why tests/conftest.py hand-mirrors the bus's Lua
scripts in Python.

The fakeredis[lua] extra in requirements-dev.in pulls lupa in so those mirrors
can be deleted (#116). While they are still there they shadow every script the
bus runs, so nothing else in the suite would notice lupa going missing: drop the
extra, or regenerate the lockfile without it, and CI would stay green until the
mirrors came out. This asserts the dependency instead of assuming it.

Uses a bare FakeRedis on purpose -- not the conftest fixture, whose eval shim is
the very thing being made redundant.
"""

import fakeredis


def test_fakeredis_evaluates_lua():
    r = fakeredis.FakeRedis()

    assert r.eval("return redis.call('SET', KEYS[1], ARGV[1])", 1, "k", "v")
    assert r.get("k") == b"v"
