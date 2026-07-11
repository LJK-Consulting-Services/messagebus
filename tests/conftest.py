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
        return super().eval(_script, numkeys, *args)


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
def ns():
    def make(**kwargs):
        defaults = {"room": "main", "json": False}
        defaults.update(kwargs)
        return argparse.Namespace(**defaults)

    return make


@pytest.fixture
def no_github(bus_module, monkeypatch):
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: [])
    monkeypatch.setattr(bus_module, "set_status_label", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bus_module, "gh", lambda *_args, **_kwargs: (0, "", ""))
