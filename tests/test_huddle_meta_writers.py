"""Static guard on who may touch the huddle metadata key.

The metadata is one JSON blob holding both an append-only `participants` list and
a mutable `driver`. Anyone who read-modify-writes the whole blob outside a WATCH
silently erases a concurrent `huddle join` — and the done-gate then closes the
huddle with an unsigned participant. The four scenario tests in
test_redis_integration.py prove the CURRENT writers are safe; this one is what
stops a FIFTH one from landing quietly, since it fails on any new function that
so much as names the key.

Widening the allowlist is a deliberate act: route the write through `_set_driver`
(WATCH/MULTI) rather than a bare `r.set(k_huddle(...))`, then add the function
here.
"""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Every function permitted to reference k_huddle, and why it is safe.
ALLOWED = {
    "k_huddle",                  # the key builder itself
    "compare_set_huddle_meta",   # Lua CAS on the lock; creates the blob at open
    "_huddle_meta",              # read-only
    "_set_driver",               # WATCH/MULTI read-modify-write
    "cmd_huddle_join",           # WATCH/MULTI append to participants
    "cmd_huddle_open",           # creates the blob (nothing to lose yet)
    "cmd_huddle_close",          # deletes the blob
    "cleanup_lost_huddle_branch",
}


def _functions_referencing(source, name):
    tree = ast.parse(source)
    return {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(fn))
    }


def test_only_allowlisted_functions_touch_the_huddle_meta_key():
    touchers = _functions_referencing((ROOT / "bus").read_text(), "k_huddle")

    unexpected = touchers - ALLOWED
    assert not unexpected, (
        f"{sorted(unexpected)} reference the huddle metadata key. A read-modify-write "
        f"of that blob outside a WATCH erases a concurrent `huddle join`; use "
        f"`_set_driver` (or another WATCH/MULTI path) and then add the function to "
        f"ALLOWED in this test."
    )


def test_allowlist_has_no_stale_entries():
    """Keeps the allowlist honest: a name left here after its function is gone or
    renamed would silently license a future function to reuse that name."""
    source = (ROOT / "bus").read_text()
    defined = {
        fn.name
        for fn in ast.walk(ast.parse(source))
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert not (ALLOWED - defined), f"allowlist names functions that no longer exist: {sorted(ALLOWED - defined)}"
