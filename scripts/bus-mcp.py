#!/usr/bin/env python3
"""
bus-mcp — a thin MCP (stdio) server that wraps the `bus` CLI as typed tools.

Lets a Claude Code coordinator session drive the message bus with structured tool
calls (bus_send, bus_board, bus_huddle_status, …) instead of constructing shell
strings and parsing text. The bus already emits `--json`, so this just forwards
argv to `./bus` (no shell — same injection-safe discipline as the CLI) and returns
the result.

Protocol: newline-delimited JSON-RPC 2.0 over stdin/stdout (MCP stdio transport).
No external dependencies. Config via env passed through to `bus`:
BUS_REDIS_URL, BUS_ROOM, BUS_GH_REPO, BUS_REPO_DIR, BUS_WORKTREE_ROOT.
"""

import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BUS = os.environ.get("BUS_BIN", os.path.join(HERE, "..", "bus"))
PROTOCOL_VERSION = "2024-11-05"

# ---- tool definitions: name -> (bus argv builder, json?, schema) ------------
# Each builder takes the validated args dict and returns the argv AFTER `bus`.
# `json_out` requests `--json` (read commands support it; writes don't).

def _room(a):
    return ["--room", a["room"]] if a.get("room") else []


def _redact_secrets(text):
    text = re.sub(r"\b(rediss?://)([^/\s@]*:[^@\s/]*@)", r"\1***@", text)
    return re.sub(
        r"([?&](?:password|pass|auth|token|access_token|secret)=)[^&\s)\"']*",
        r"\1***",
        text,
        flags=re.IGNORECASE,
    )


TOOLS = {
    "bus_send": {
        "desc": "Send a message on the bus (from the coordinator to an agent or 'all').",
        "schema": {
            "type": "object",
            "properties": {
                "frm": {"type": "string", "description": "sender id (usually 'coordinator')"},
                "to": {"type": "string", "description": "recipient agent id or 'all'"},
                "body": {"type": "string", "description": "message text"},
                "topic": {"type": "string", "description": "optional topic slug, e.g. issue-42"},
                "kind": {"type": "string", "description": "msg|question|answer|system (default msg)"},
                "room": {"type": "string"},
            },
            "required": ["frm", "to", "body"],
        },
        "json_out": False,
        "argv": lambda a: _room(a) + ["send", "--from", a["frm"], "--to", a["to"]]
                          + (["--topic", a["topic"]] if a.get("topic") else [])
                          + (["--kind", a["kind"]] if a.get("kind") else [])
                          + [a["body"]],
    },
    "bus_board": {
        "desc": "Table of gh issues + status label + Redis lock holder + presence.",
        "schema": {"type": "object", "properties": {"room": {"type": "string"}}},
        "json_out": True,
        "argv": lambda a: _room(a) + ["board"],
    },
    "bus_agents": {
        "desc": "List agents currently present in a room.",
        "schema": {"type": "object", "properties": {"room": {"type": "string"}}},
        "json_out": True,
        "argv": lambda a: _room(a) + ["agents"],
    },
    "bus_tail": {
        "desc": "Recent messages in a room (does not move any cursor).",
        "schema": {"type": "object", "properties": {
            "n": {"type": "integer", "description": "how many (default 20)"},
            "room": {"type": "string"}}},
        "json_out": True,
        "argv": lambda a: _room(a) + ["tail", "-n", str(a.get("n", 20))],
    },
    "bus_thread": {
        "desc": "Print the full reply_to thread containing a message id.",
        "schema": {"type": "object", "properties": {
            "id": {"type": "string"}, "room": {"type": "string"}}, "required": ["id"]},
        "json_out": True,
        "argv": lambda a: _room(a) + ["thread", a["id"]],
    },
    "bus_ws_list": {
        "desc": "All agent worktrees with dirty / unpushed / present status.",
        "schema": {"type": "object", "properties": {}},
        "json_out": True,
        "argv": lambda a: ["ws", "list"],
    },
    "bus_reap": {
        "desc": "List stale issue locks (holder absent). Advisory; does not release.",
        "schema": {"type": "object", "properties": {}},
        "json_out": True,
        "argv": lambda a: ["reap"],
    },
    "bus_huddle_status": {
        "desc": "Huddle state for an issue: opener, driver, participants, branch, status.",
        "schema": {"type": "object", "properties": {
            "issue": {"type": "integer"}}, "required": ["issue"]},
        "json_out": True,
        "argv": lambda a: ["huddle", "status", "--issue", str(a["issue"])],
    },
    "bus_pen_status": {
        "desc": "Current write-pen holder + shared branch tip for a huddle issue.",
        "schema": {"type": "object", "properties": {
            "issue": {"type": "integer"}}, "required": ["issue"]},
        "json_out": True,
        "argv": lambda a: ["pen", "status", "--issue", str(a["issue"])],
    },
    "bus_status_set": {
        "desc": "Transition a gh issue's status:* label (coordinator moving the state machine).",
        "schema": {"type": "object", "properties": {
            "as_agent": {"type": "string"}, "issue": {"type": "integer"},
            "set": {"type": "string", "description": "status:open|claimed|pr-open|merged|deployed|verified"}},
            "required": ["as_agent", "issue", "set"]},
        "json_out": False,
        "argv": lambda a: ["status", "--as", a["as_agent"], "--issue", str(a["issue"]), "--set", a["set"]],
    },
    "bus_doctor": {
        "desc": "Check Redis + gh connectivity.",
        "schema": {"type": "object", "properties": {}},
        "json_out": False,
        "argv": lambda a: ["doctor"],
    },
}


def run_bus(tool, args):
    spec = TOOLS[tool]
    argv = [BUS]
    if spec["json_out"]:
        argv.append("--json")
    argv += spec["argv"](args)
    p = subprocess.run(argv, capture_output=True, text=True)  # argv, never shell
    out = _redact_secrets((p.stdout or "").strip())
    err = _redact_secrets((p.stderr or "").strip())
    if p.returncode != 0 and not out:
        return f"bus error (rc={p.returncode}): {err}", True
    text = out if out else (err or "(ok)")
    if err and out:
        text = f"{out}\n[stderr] {err}"
    return text, p.returncode != 0


# ---- JSON-RPC / MCP plumbing ------------------------------------------------

def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def result(rid, res):
    send({"jsonrpc": "2.0", "id": rid, "result": res})


def error(rid, code, message):
    send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(req):
    method = req.get("method")
    rid = req.get("id")
    if method == "initialize":
        result(rid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "messagebus", "version": "0.1.0"},
        })
    elif method == "notifications/initialized":
        pass  # notification, no response
    elif method == "ping":
        result(rid, {})
    elif method == "tools/list":
        result(rid, {"tools": [
            {"name": name, "description": t["desc"], "inputSchema": t["schema"]}
            for name, t in TOOLS.items()
        ]})
    elif method == "tools/call":
        params = req.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {}) or {}
        if name not in TOOLS:
            error(rid, -32601, f"unknown tool: {name}")
            return
        try:
            text, is_err = run_bus(name, args)
        except Exception as e:  # noqa: BLE001 - surface as a tool error, never crash the server
            text, is_err = f"tool failed: {e}", True
        result(rid, {"content": [{"type": "text", "text": text}], "isError": is_err})
    elif rid is not None:
        error(rid, -32601, f"method not found: {method}")


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            handle(req)
        except Exception as e:  # noqa: BLE001 - keep the server alive on any single-request error
            if isinstance(req, dict) and req.get("id") is not None:
                error(req["id"], -32603, f"internal error: {e}")


if __name__ == "__main__":
    main()
