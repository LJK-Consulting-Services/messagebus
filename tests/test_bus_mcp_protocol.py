import importlib.util
import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_bus_mcp():
    spec = importlib.util.spec_from_file_location("bus_mcp_protocol", ROOT / "scripts" / "bus-mcp.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_builders_and_json_rpc_handlers(monkeypatch, capsys):
    bus_mcp = load_bus_mcp()

    assert bus_mcp.TOOLS["bus_send"]["argv"]({
        "room": "dev",
        "frm": "coordinator",
        "to": "agent",
        "topic": "issue-79",
        "kind": "question",
        "body": "ping",
    }) == [
        "--room",
        "dev",
        "send",
        "--from",
        "coordinator",
        "--to",
        "agent",
        "--topic",
        "issue-79",
        "--kind",
        "question",
        "ping",
    ]

    monkeypatch.setattr(
        bus_mcp.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, '[{"ok": true}]', ""),
    )
    text, is_error = bus_mcp.run_bus("bus_board", {"room": "dev"})
    assert text == '[{"ok": true}]'
    assert not is_error

    bus_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    bus_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    bus_mcp.handle({
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "bus_board", "arguments": {}},
    })
    bus_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "missing"})

    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert responses[0]["result"]["serverInfo"]["name"] == "messagebus"
    assert responses[1]["result"]["tools"]
    assert responses[2]["result"]["content"][0]["text"] == '[{"ok": true}]'
    assert responses[3]["error"]["code"] == -32601
