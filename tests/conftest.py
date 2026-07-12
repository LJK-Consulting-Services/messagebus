import argparse
import importlib.machinery
import importlib.util
from pathlib import Path

import fakeredis
import pytest
import redis


ROOT = Path(__file__).resolve().parents[1]
_REAL_REDIS_MODULE = redis


class BusFakeRedis(fakeredis.FakeRedis):
    def eval(self, _script, numkeys, *args):
        keys = args[:numkeys]
        argv = args[numkeys:]
        if numkeys == 1:
            key = keys[0]
            holder = argv[0]
            if self.get(key) == holder:
                return self.delete(key)
            return 0
        if numkeys == 2:
            lock_key, meta_key = keys
            holder, meta_json = argv
            if self.get(lock_key) == holder:
                self.set(meta_key, meta_json)
                return 1
            return 0
        if numkeys == 4:
            lock_key, meta_key, pen_key, chal_key = keys
            holder, meta_json, pen_holder = argv
            if self.get(lock_key) == holder:
                self.set(meta_key, meta_json)
                self.set(pen_key, pen_holder)
                self.delete(chal_key)
                return 1
            return 0
        return super().eval(_script, numkeys, *args)

    def pipeline(self, transaction=True, shard_hint=None):
        pipe = super().pipeline(transaction=transaction, shard_hint=shard_hint)
        pipe.__class__ = type(
            "BusFakePipeline", (_EvalMirrorPipeline, pipe.__class__), {})
        pipe._bus_client = self
        return pipe


class _EvalMirrorPipeline:
    """Mirrors CAS_DELETE_LUA when the script is QUEUED inside a MULTI.

    `BusFakeRedis.eval` only overrides the CLIENT. `r.pipeline()` hands back a
    separate pipeline object that does not inherit it, so a queued `pipe.eval(...)`
    reaches fakeredis raw — and fakeredis only speaks EVAL when `lupa` is installed,
    which CI deliberately does not install. `cmd_huddle_close` commits its done-gate
    and its teardown in one transaction (#92), which put an EVAL inside a MULTI for
    the first time and turned that gap into "unknown command 'eval'".

    Real Redis runs the script atomically AT EXEC. The fake has no concurrency between
    `multi()` and `execute()`, so resolving the compare here and substituting the
    equivalent concrete command yields the same result vector. The substituted command
    goes through `self`, so the pipeline's own dispatch still decides whether it runs
    immediately (while watching, pre-`multi()`) or queues (inside the MULTI) — the
    mirror does not have to know which mode it is in. The compare itself reads through
    the CLIENT: a `get` on a pipeline in MULTI mode would queue another command rather
    than answer.

    What this cannot model is the interleaving the CAS exists to defend against. That
    is what the real-Redis integration tests cover; they are the backstop for this
    double, not an optional extra.

    Only the 1-key CAS-delete is pipelined today. Anything else raises rather than
    silently mis-mirroring a script this double does not actually understand.
    """

    _CAS_MISS = "bus:__cas_miss__"  # never written, so EXISTS yields a literal 0

    def eval(self, _script, numkeys, *args):
        if numkeys != 1:
            raise NotImplementedError(
                f"pipelined EVAL with numkeys={numkeys} is not mirrored by this test "
                f"double; extend _EvalMirrorPipeline before pipelining that script")
        key, holder = args[0], args[1]
        if self._bus_client.get(key) == holder:
            return self.delete(key)         # 1 at EXEC — what the script returns on a hit
        return self.exists(self._CAS_MISS)  # 0 — what the script returns on a miss


@pytest.fixture(scope="session")
def bus_module():
    loader = importlib.machinery.SourceFileLoader("bus_under_test", str(ROOT / "bus"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


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
