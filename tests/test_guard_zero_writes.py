import json


def redis_values(r):
    values = {}
    for key in sorted(r.scan_iter("*")):
        kind = r.type(key)
        if kind == "string":
            values[key] = (kind, r.get(key))
        elif kind == "stream":
            values[key] = (kind, r.xrange(key))
        else:
            values[key] = (kind, None)
    return values


def test_release_guard_returns_before_any_write(bus_module, fake_redis, ns):
    fake_redis.set(bus_module.k_lock(79), "alice")
    before = redis_values(fake_redis)

    assert bus_module.cmd_release(fake_redis, ns(as_agent="bob", issue=79)) == 1

    assert redis_values(fake_redis) == before


def test_pen_pass_guard_returns_before_any_write(bus_module, fake_redis, ns):
    fake_redis.set(bus_module.k_pen(79), "alice")
    before = redis_values(fake_redis)

    assert bus_module.cmd_pen_pass(
        fake_redis,
        ns(as_agent="bob", issue=79, to="alice"),
    ) == 1

    assert redis_values(fake_redis) == before


def test_huddle_close_nonparticipant_guard_returns_before_any_write(
    bus_module, fake_redis, ns
):
    meta = {
        "issue": 79,
        "opener": "alice",
        "participants": ["alice"],
        "driver": "alice",
        "branch": bus_module.huddle_branch(79),
        "session": "huddle:issue-79:session",
        "status": "open",
    }
    fake_redis.set(bus_module.k_huddle(79), json.dumps(meta))
    before = redis_values(fake_redis)

    assert bus_module.cmd_huddle_close(
        fake_redis,
        ns(as_agent="mallory", issue=79, force=False),
    ) == 1

    assert redis_values(fake_redis) == before
