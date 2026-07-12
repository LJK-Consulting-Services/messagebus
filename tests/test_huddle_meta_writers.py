"""Static guard on who may touch the huddle metadata key.

The metadata is one JSON blob holding both an append-only `participants` list and
a mutable `driver`. Anyone who read-modify-writes the whole blob outside a WATCH
silently erases a concurrent `huddle join` — and the done-gate then closes the
huddle with an unsigned participant. The scenario tests in
test_redis_integration.py prove the CURRENT writers are safe against a real
concurrent join; this file is what stops a NEW one from landing quietly, by
failing on any function that reaches the key outside the known-safe set.

What this guard does and does NOT catch, stated honestly so nobody over-trusts
it:
  * It catches a new function that references `k_huddle` OR builds the key by its
    literal `bus:huddle:` prefix (the two ways to reach the key from scratch).
  * It does NOT catch a blind write added INSIDE an already-allowlisted function
    — an allowlist is function-granular by construction. The scenario tests, not
    this one, are the guard against a regression inside a known writer.

Widening the allowlist is a deliberate act: route the write through `_set_driver`
(WATCH/MULTI) rather than a bare `r.set(k_huddle(...))`, then add the function
here.
"""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HUDDLE_KEY_PREFIX = "bus:huddle:"  # k_huddle builds f"bus:huddle:issue:{issue}"

# Every function permitted to reach the huddle meta key, and why it is safe.
ALLOWED = {
    "k_huddle",                  # the key builder itself (owns the literal prefix)
    "compare_set_huddle_meta",   # Lua CAS on the lock; creates the blob at open
    "_huddle_meta",              # read-only
    "_set_driver",               # WATCH/MULTI read-modify-write
    "_release_pen_driver",       # WATCH/MULTI pen delete + driver clear
    "_record_pen_challenge",     # WATCH/MULTI challenge write bound to session
    "_delete_pen_challenge",     # WATCH/MULTI challenge delete bound to session
    "_mutate_json",              # WATCH/MULTI; READS meta to bind the block/signoff
                                 # write to the session, never writes the blob (#115)
    "cmd_huddle_close",          # WATCH/MULTI: done-gate + teardown in one txn (#92)
    "cmd_huddle_join",           # WATCH/MULTI append to participants
    "cmd_huddle_open",           # creates the blob (nothing to lose yet)
    "cleanup_lost_huddle_branch",
}


def _functions_reaching_the_key(source):
    """Functions that either call `k_huddle` or embed its literal key prefix —
    the two ways to address the metadata key. Catching the literal closes the
    obvious bypass of naming the key directly instead of via the builder."""
    tree = ast.parse(source)
    reaching = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for n in ast.walk(fn):
            if isinstance(n, ast.Name) and n.id == "k_huddle":
                reaching.add(fn.name)
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and HUDDLE_KEY_PREFIX in n.value:
                reaching.add(fn.name)
    return reaching


def test_only_allowlisted_functions_touch_the_huddle_meta_key():
    unexpected = _functions_reaching_the_key((ROOT / "bus").read_text()) - ALLOWED
    assert not unexpected, (
        f"{sorted(unexpected)} reach the huddle metadata key. A read-modify-write "
        f"of that blob outside a WATCH erases a concurrent `huddle join`; use "
        f"`_set_driver` (or another WATCH/MULTI path) and then add the function to "
        f"ALLOWED in this test."
    )


def test_allowlist_has_no_dead_entries():
    """Keeps the allowlist honest. Every entry must both be a real function AND
    actually reach the key (except `k_huddle`, which DEFINES the prefix rather
    than reaching it, and `cmd_huddle_open`, which writes via
    `compare_set_huddle_meta` without naming the key). A name that reaches
    neither is dead weight that silently licenses a future reuse of it."""
    source = (ROOT / "bus").read_text()
    defined = {
        fn.name
        for fn in ast.walk(ast.parse(source))
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not (ALLOWED - defined), (
        f"allowlist names functions that no longer exist: {sorted(ALLOWED - defined)}")
    reaches = _functions_reaching_the_key(source)
    indirect = {"k_huddle", "cmd_huddle_open"}
    dead = ALLOWED - reaches - indirect
    assert not dead, (
        f"allowlist entries that no longer reach the key (remove them): {sorted(dead)}")
