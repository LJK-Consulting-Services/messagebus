"""Seat config for swarm-spawn and swarm-worker: $SWARM_CONFIG or ~/.config/messagebus/swarm.yaml.

See examples/swarm.yaml for the format. Without a file, the built-in seats below apply.
"""
import os
import shlex

DEFAULT_CLAUDE_FLAGS = ("--permission-mode acceptEdits --allowedTools Edit Write Read Glob Grep "
                        "Bash(git:*) Bash(gh:*) Bash(bus:*) Bash(python:*) Bash(pytest:*) Bash(ls:*) "
                        "Bash(cat:*) Bash(mkdir:*)")
KINDS = ("claude", "codex", "omp", "agy", "cmd")
DEFAULT_SEATS = {
    "claude": {"kind": "claude", "launcher": ["claude"]},
    "codex": {"kind": "codex"},
    "omp": {"kind": "omp"},
    "agy": {"kind": "agy", "command": ["agy-review.sh"]},
    "cmd": {"kind": "cmd"},
}


def config_path():
    return os.environ.get("SWARM_CONFIG") or os.path.expanduser("~/.config/messagebus/swarm.yaml")


def read_file(path):
    if not os.path.exists(path):
        return {}
    import yaml
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load():
    raw = read_file(config_path())
    seats = dict(raw.get("seats") or DEFAULT_SEATS)
    seats.setdefault("cmd", DEFAULT_SEATS["cmd"])
    for name, seat in seats.items():
        if seat.get("kind") not in KINDS:
            raise SystemExit(f"swarm config: seat '{name}' has kind {seat.get('kind')!r}; use one of {KINDS}")
    return {"seats": seats, "picker": raw.get("picker"), "models": raw.get("models") or [],
            "roles": raw.get("roles") or {}, "unknown_score": float(raw.get("unknown_score", 0)),
            "claude_flags": shlex.split(raw.get("claude_flags") or DEFAULT_CLAUDE_FLAGS)}


def writes(seat):
    return seat.get("writes", seat["kind"] != "agy")


def seat_score(cfg, scores, name):
    value = scores.get(name)
    if value == "throttled":
        return None
    return cfg["unknown_score"] if value is None else float(value)


def candidates(cfg, band, scores):
    """(headroom score, model index, seat name, model entry) for every seat that can serve a model in the band."""
    out = []
    for model in cfg["models"]:
        if not band["min"] <= model["index"] <= band["max"]:
            continue
        for name in model["seats"]:
            seat = cfg["seats"].get(name)
            score = seat_score(cfg, scores, name) if seat else None
            if score is None or (band.get("writes", True) and not writes(seat)):
                continue
            out.append((score, model["index"], name, model))
    return out


def choose(cfg, role, scores):
    """The seat with the most headroom among models whose intelligence index is in the role's band."""
    band = cfg["roles"].get(role)
    if not band:
        raise SystemExit(f"swarm config: no band for role '{role}'; add roles.{role}: {{min, max}}")
    if not all(isinstance(band.get(k), (int, float)) for k in ("min", "max")):
        raise SystemExit(f"swarm config: roles.{role} needs numeric min and max, got {band!r}")
    found = candidates(cfg, band, scores)
    if not found:
        raise SystemExit(f"swarm config: no unthrottled seat serves a model with index {band['min']}-{band['max']}")
    _, _, name, model = max(found, key=lambda c: (c[0], c[1]))
    return {"harness": name, "model": model["model"], "effort": model.get("effort")}


def seat(cfg, name):
    if name not in cfg["seats"]:
        raise SystemExit(f"swarm config: no seat '{name}' (have: {', '.join(sorted(cfg['seats']))}); "
                         f"edit {config_path()}")
    return cfg["seats"][name]
