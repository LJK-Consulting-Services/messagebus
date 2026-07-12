"""#80 — message-envelope schema_version stamp + backward compatibility."""


def test_make_fields_stamps_message_schema_version(bus_module):
    f = bus_module.make_fields("alice", "bob", "hi")
    assert f["schema_version"] == bus_module.MESSAGE_SCHEMA_VERSION == 1


def test_fields_to_msg_defaults_legacy_missing_version_to_zero(bus_module):
    # A message written before the stamp existed has no schema_version field.
    legacy = {"from": "alice", "to": "bob", "kind": "msg", "body": "hi"}
    msg = bus_module.fields_to_msg("1-0", legacy)
    assert msg["schema_version"] == 0


def test_schema_version_round_trips_as_int(bus_module):
    # Redis stores stream fields as strings; the read side must coerce to int
    # so callers can compare numerically (e.g. version gating).
    f = bus_module.make_fields("alice", "bob", "hi")
    stored = {k: str(v) for k, v in f.items()}  # mimic redis string storage
    msg = bus_module.fields_to_msg("1-0", stored)
    assert msg["schema_version"] == 1
    assert isinstance(msg["schema_version"], int)
