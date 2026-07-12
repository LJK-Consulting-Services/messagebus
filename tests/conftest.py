import argparse
import functools
import importlib.machinery
import importlib.util
import types
from pathlib import Path

import fakeredis
import pytest
import redis


ROOT = Path(__file__).resolve().parents[1]
_REAL_REDIS_MODULE = redis
_CAS_MISS = "bus:__cas_miss__"  # never written, so EXISTS on it yields a literal 0


def _load_bus():
    loader = importlib.machinery.SourceFileLoader("bus_under_test", str(ROOT / "bus"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_BUS = _load_bus()

# fakeredis only speaks EVAL when `lupa` is installed, which CI deliberately does not
# install, so the bus's Lua has to be mirrored in Python here. The mirrors dispatch on
# the SCRIPT, never on its arity: a future one-key script (a CAS-expire, a CAS-set)
# would otherwise be silently executed as a compare-and-DELETE and the unit suite would
# still pass. An unrecognised script raises instead. Compared by text, not identity —
# tests/test_huddle_gate.py loads its own `bus` module instance, whose constants are
# equal strings but distinct objects.
_CAS_DELETE = _BUS.CAS_DELETE_LUA
_CAS_SET_META = _BUS.CAS_SET_META_LUA
_CAS_SET_META_PEN = _BUS.CAS_SET_META_PEN_LUA


def _unknown_script(script, numkeys):
    return NotImplementedError(
        f"this test double does not mirror the Lua script it was handed "
        f"(numkeys={numkeys}). Every script the bus runs must be mirrored here, or "
        f"exercised against real Redis in tests/test_redis_integration.py, or the fake "
        f"silently diverges from production. Script:\n{script}")


class BusFakeRedis(fakeredis.FakeRedis):
    def eval(self, script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if script == _CAS_DELETE:
            key, holder = keys[0], argv[0]
            if self.get(key) == holder:
                return self.delete(key)
            return 0
        if script == _CAS_SET_META:
            lock_key, meta_key = keys
            holder, meta_json = argv
            if self.get(lock_key) == holder:
                self.set(meta_key, meta_json)
                return 1
            return 0
        if script == _CAS_SET_META_PEN:
            lock_key, meta_key, pen_key, chal_key = keys
            holder, meta_json, pen_holder = argv
            if self.get(lock_key) == holder:
                self.set(meta_key, meta_json)
                self.set(pen_key, pen_holder)
                self.delete(chal_key)
                return 1
            return 0
        raise _unknown_script(script, numkeys)

    def pipeline(self, transaction=True, shard_hint=None):
        pipe = super().pipeline(transaction=transaction, shard_hint=shard_hint)
        pipe.eval = types.MethodType(functools.partial(_mirror_cas_eval, client=self), pipe)
        return pipe


def _mirror_cas_eval(pipe, script, numkeys, *args, client):
    """Mirror CAS_DELETE_LUA when the script is QUEUED inside a MULTI.

    `BusFakeRedis.eval` only overrides the CLIENT. `r.pipeline()` hands back a separate
    pipeline object that does not inherit it, so a queued `pipe.eval(...)` reaches
    fakeredis raw — and fakeredis only speaks EVAL when `lupa` is installed, which CI
    deliberately does not install. `cmd_huddle_close` commits its done-gate and its
    teardown in one transaction (#92), which put an EVAL inside a MULTI for the first
    time and turned that gap into "unknown command 'eval'".

    Real Redis runs the script atomically AT EXEC. The fake has no concurrency between
    `multi()` and `execute()`, so resolving the compare here and substituting the
    equivalent concrete command yields the same result vector. The substituted command
    goes through `pipe`, so the pipeline's own dispatch still decides whether it runs
    immediately (while watching, pre-`multi()`) or queues (inside the MULTI) — the
    mirror does not have to know which mode it is in. The compare reads through the
    CLIENT: a `get` on a pipeline in MULTI mode would queue rather than answer.

    What this cannot model is the interleaving the CAS exists to defend against. That is
    what the real-Redis integration tests cover; they are the backstop for this double,
    not an optional extra.
    """
    if script != _CAS_DELETE:  # the only script pipelined today
        raise _unknown_script(script, numkeys)
    key, holder = args[0], args[1]
    if client.get(key) == holder:
        return pipe.delete(key)       # 1 at EXEC — what the script returns on a hit
    return pipe.exists(_CAS_MISS)     # 0 — what the script returns on a miss


@pytest.fixture(scope="session")
def bus_module():
    return _BUS


@pytest.fixture
def fake_redis():
    return BusFakeRedis(decode_responses=True)


@pytest.fixture
def ns(bus_module):
    def make(**kwargs):
        defaults = {"room": "main", "json": False, "url": bus_module.DEFAULT_URL}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    return make


@pytest.fixture
def events_of(bus_module):
    """Every structured event on a room's stream, optionally one type only.

    Takes the client so it works against both fakeredis and the real server the
    durability tests spawn. `kind=None` returns all events — which is how a test
    asserts that something (e.g. `bus watch`) emitted NONE.
    """
    def get(r, kind=None, room="main"):
        decoded = (bus_module.parse_event(bus_module.fields_to_msg(mid, f))
                   for mid, f in r.xrange(bus_module.k_stream(room)))
        return [e for e in decoded if e and (kind is None or e["event"] == kind)]

    return get


@pytest.fixture
def no_github(bus_module, monkeypatch):
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: [])
    monkeypatch.setattr(bus_module, "set_status_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bus_module, "gh", lambda *_args, **_kwargs: (0, "", ""))
