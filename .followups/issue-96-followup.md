# Follow-up: room-scope every wildcard presence read as ONE change (Fleet-S1)

Surfaced by the `/simplify high` + adversarial gates on **#96** (donegate presence-snapshot).
Deliberately kept OUT of #96 so its reviewed SHA (`30927dd`, claude-3 non-author APPROVE)
and its zero-conflict merge surface stay intact while #100/#113 are in flight.

All line numbers below are on **`origin/dev`** (`git show origin/dev:bus`), re-derived
against the blob rather than a local checkout — a stale working branch shifts these by ~70
lines and has already caused three separate agents to cite the wrong line.

---

## 1. The wildcard presence reads must be scoped all-or-nothing

`scan_iter` sites on `origin/dev` that read presence keys:

| line | caller | glob | room-scoped? |
|------|--------|------|--------------|
| 954  | `_holder_present` | `bus:presence:*:{holder}` | requires a room segment |
| 1024 | `cmd_reap`    | `bus:presence:*` | **bare** |
| 1162 | `cmd_drain`   | `bus:presence:*:{agent}` | requires a room segment |
| 1206 | `cmd_board`   | `bus:presence:*` | **bare** |
| 1700 | `cmd_ws_list` | `bus:presence:*` | **bare** |

#96 adds a sixth: `_present_set`, which copies the **bare** form.

Two of these — `_holder_present` (954) and `cmd_drain` (1162) — read across rooms
**deliberately**, and say so in the source. Scoping them blind BREAKS them: `cmd_drain`
exists to clear an agent out of *every* room on exit, so a room filter makes it silently
fail to drain, which strands that agent's presence key and leaves stale pen/huddle state
behind. So this is **not** a mechanical prefix sweep — each site needs its own decision,
and a half-applied sweep is worse than none.

## 2. Malformed-key parity (codex-1's finding, empirically confirmed on real Redis)

The **bare** glob `bus:presence:*` + `rsplit(":", 1)[-1]` treats a key that is missing its
room segment — `bus:presence:bob` — as "agent `bob` is present". `_holder_present`'s
room-segment glob does not match that key. So the two disagree.

Status: **real, but non-blocking**, which is why it did not gate #96:
- **Fail-closed** in donegate — a spurious present agent only ADDS block reasons; it cannot
  excuse an unsigned participant.
- **CLI-unreachable** — `--as` and `--room` are both `type=ident` (`^[A-Za-z0-9._-]+$`), so
  no CLI path can write a key without a room segment.
- **Zero new capability** — anyone who can write Redis directly can forge the *canonical*
  key for identical effect.
- **Pre-existing** — `origin/dev` already ships the identical bare glob at 1024 / 1206 /
  1700. #96 imports the weakness; it does not invent it.

### Do NOT "harden" this with an exact-segment-count filter

Rejected patch: `if len(key.split(":")) != 4: continue`.

It **drops** a 5-segment key `bus:presence:main:x:bob`, which `_holder_present`'s glob
matches as **present** (verified on real Redis, both branches, with negative controls).
That makes the presence set a strict *subset* of what the pen/gate path sees — i.e. it
**fails OPEN**: a participant who is genuinely present gets dropped from the set, is
silently excused from signing off, and `close` then tears down `k_signoff` / `k_block` /
`k_pen` — **destroying that participant's unpushed work.** It hand-installs the exact
gate-bypass the reviewer proved was impossible, under the banner of a security fix.

The invariant to preserve: **the presence set must be a superset of what `_holder_present`
matches.** A stricter filter is a data-loss bug, not hardening.

If you want behavior-identical all-agents semantics *before* full room-prefixing, the
correct one-liner is the glob, not a filter:

```python
scan_iter(match="bus:presence:*:*", count=_SCAN_COUNT)   # rsplit(":", 1)[-1] as before
```

`*:*` requires a room segment, so it matches **exactly** the key set `_holder_present`
matches — parity on all three shapes (canonical, bare-malformed, 5-segment). Not a
superset, not a subset.

## 3. `count=` batch hint missing on the remaining `scan_iter` sites

`_SCAN_COUNT` was introduced in #96 but only applied there. Remaining un-hinted sites on
`origin/dev`: **884** (`k_cursor(room, "*")` — a retention/cursor scan, *not* a presence
read; it gets mis-cited as one), **1026** and **1203** (`bus:lock:issue:*`), **1072**,
**1314** (already correctly room-scoped via the `k_presence` helper), **1603**
(`bus:worktree:issue:*`).

Pure batching hint, no semantic change. Verified against fakeredis + real Redis in #96.

## 4. Tests owed

- Assert `count=_SCAN_COUNT` is actually passed at the sites whose perf claim depends on it.
- Parity coverage across all three key shapes — canonical, bare-malformed, 5-segment —
  pinning `present_set(p) <=> _holder_present(p)` once the globs change. A guard test that
  also passes *before* the fix is coverage, not a guard: the 5-segment row is the one that
  catches the fail-open regression above.

---

**Sizing:** low priority — sub-millisecond on current keyspaces, and the malformed-key path
is fail-closed and CLI-unreachable. The durable win is consolidation: six presence reads,
one decision each, landed together.
