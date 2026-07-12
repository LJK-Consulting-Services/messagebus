import argparse
import importlib.machinery
import importlib.util
from pathlib import Path

import fakeredis
import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_bus():
    loader = importlib.machinery.SourceFileLoader("bus_under_test", str(ROOT / "bus"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


_BUS = _load_bus()


@pytest.fixture(scope="session")
def bus_module():
    return _BUS


@pytest.fixture
def fake_redis():
    return fakeredis.FakeRedis(decode_responses=True)


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
