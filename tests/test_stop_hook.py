import json
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
AGENT = "agent-a"


def make_hook_runner(
    tmp_path,
    sequence,
    *,
    max_continue=3,
    max_turns=10,
    env_extra=None,
    bus_dir_name="bus-state",
):
    bus_dir = tmp_path / bus_dir_name
    bus_dir.mkdir()
    sequence_file = tmp_path / "bus-sequence.txt"
    state_file = tmp_path / "bus-sequence.idx"
    sequence_file.write_text("\n".join(sequence) + "\n")

    fake_bus = tmp_path / "fake-bus"
    fake_bus.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
idx=0
if [[ -f "$FAKE_BUS_STATE" ]]; then
  idx="$(cat "$FAKE_BUS_STATE")"
fi
line="$(sed -n "$((idx + 1))p" "$FAKE_BUS_SEQUENCE")"
if [[ -z "$line" ]]; then
  echo "fake bus exhausted" >&2
  exit 99
fi
echo "$((idx + 1))" > "$FAKE_BUS_STATE"
rc="${line%%|*}"
payload=""
if [[ "$line" == *"|"* ]]; then
  payload="${line#*|}"
fi
if [[ -n "$payload" ]]; then
  printf '%s\\n' "$payload"
fi
exit "$rc"
"""
    )
    fake_bus.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "BUS_AGENT": AGENT,
            "BUS_BIN": str(fake_bus),
            "BUS_DIR": str(bus_dir),
            "BUS_ROOM": "test-room",
            "BUS_WAIT_SECS": "0",
            "BUS_MAX_TURNS": str(max_turns),
            "BUS_MAX_CONTINUE": str(max_continue),
            "BUS_ERROR_BACKOFF_SECS": "0",
            "FAKE_BUS_SEQUENCE": str(sequence_file),
            "FAKE_BUS_STATE": str(state_file),
        }
    )
    if env_extra:
        env.update(env_extra)

    def run_hook():
        return subprocess.run(
            [str(ROOT / "hooks/stop-hook.sh")],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    return run_hook, bus_dir


def parse_block(result):
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["decision"] == "block"
    return payload["reason"]


def touch_command_from(reason):
    match = re.search(r"run:  (touch .+?)  —", reason)
    assert match, reason
    return match.group(1)


def test_stop_hook_self_continues_until_done_marker(tmp_path):
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        [
            '0|[{"id":"1","body":"claim issue"}]',
            "10|",
            "10|",
            "10|",
            "10|",
        ],
    )
    active_file = bus_dir / f"active-{AGENT}"
    done_marker = bus_dir / f"turn-done-{AGENT}"
    turns_file = bus_dir / f"turns-{AGENT}"
    continue_file = bus_dir / f"continue-{AGENT}"

    delivered_reason = parse_block(run_hook())

    assert active_file.exists()
    assert not done_marker.exists()
    assert "BEGIN UNTRUSTED BUS MESSAGES" in delivered_reason
    assert f"touch {done_marker}" in delivered_reason
    assert turns_file.read_text().strip() == "1"

    continue_reason = parse_block(run_hook())

    assert continue_reason.startswith("CONTINUE your current task")
    assert f"touch {done_marker}" in continue_reason
    assert continue_file.read_text().strip() == "1"
    assert turns_file.read_text().strip() == "1"

    done_marker.touch()
    stopped = run_hook()

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == ""
    assert not active_file.exists()
    assert not done_marker.exists()


def test_stop_hook_no_active_turn_allows_idle_stop(tmp_path):
    run_hook, bus_dir = make_hook_runner(tmp_path, ["10|", "10|"])

    stopped = run_hook()

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == ""
    assert not (bus_dir / f"active-{AGENT}").exists()


def test_stop_hook_continue_budget_caps_self_continue(tmp_path):
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        ["10|", "10|", "10|", "10|", "10|", "10|"],
        max_continue=2,
    )
    active_file = bus_dir / f"active-{AGENT}"
    active_file.touch()

    assert parse_block(run_hook()).startswith("CONTINUE your current task")
    assert parse_block(run_hook()).startswith("CONTINUE your current task")
    stopped = run_hook()

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == ""
    assert "hit BUS_MAX_CONTINUE=2" in stopped.stderr
    assert not active_file.exists()


def test_stop_hook_unexpected_bus_rc_blocks_retry(tmp_path):
    run_hook, bus_dir = make_hook_runner(tmp_path, ["2|"], max_continue=2)

    reason = parse_block(run_hook())

    assert "bus poll/wait return rc=2" in reason
    assert f"touch {bus_dir / f'turn-done-{AGENT}'}" in reason
    assert (bus_dir / f"continue-{AGENT}").read_text().strip() == "1"


def test_stop_hook_max_turns_does_not_consume_delivered_message(tmp_path):
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        ['0|[{"id":"1","body":"still queued"}]'],
        max_turns=1,
    )
    (bus_dir / f"turns-{AGENT}").write_text("1")

    stopped = run_hook()

    assert stopped.returncode == 0, stopped.stderr
    assert stopped.stdout == ""
    assert "hit BUS_MAX_TURNS=1" in stopped.stderr
    assert not (tmp_path / "bus-sequence.idx").exists()


def test_stop_hook_max_turns_still_self_continues_active_turn(tmp_path):
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        [
            '0|[{"id":"1","body":"claim issue"}]',
            '0|[{"id":"2","body":"should remain queued"}]',
        ],
        max_turns=1,
    )
    active_file = bus_dir / f"active-{AGENT}"
    done_marker = bus_dir / f"turn-done-{AGENT}"

    parse_block(run_hook())
    reason = parse_block(run_hook())

    assert active_file.exists()
    assert reason.startswith("CONTINUE your current task")
    assert f"touch {done_marker}" in reason
    assert (tmp_path / "bus-sequence.idx").read_text().strip() == "1"


def test_stop_hook_ignores_non_numeric_state_without_command_substitution(tmp_path):
    pwned = tmp_path / "pwned"
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        ["10|", "10|"],
        env_extra={"BUS_MAX_CONTINUE": f"MAX_CONTINUE[$(touch {pwned})]"},
    )
    (bus_dir / f"active-{AGENT}").touch()
    (bus_dir / f"continue-{AGENT}").write_text(f"MAX_CONTINUE[$(touch {pwned})]")

    reason = parse_block(run_hook())

    assert reason.startswith("CONTINUE your current task")
    assert not pwned.exists()
    assert (bus_dir / f"continue-{AGENT}").read_text().strip() == "1"


def test_stop_hook_canonicalizes_leading_zero_counts(tmp_path):
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        ["10|", "10|"],
        max_continue="08",
    )
    (bus_dir / f"active-{AGENT}").touch()
    (bus_dir / f"continue-{AGENT}").write_text("07")

    result = run_hook()
    reason = parse_block(result)

    assert reason.startswith("CONTINUE your current task")
    assert "value too great for base" not in result.stderr
    assert (bus_dir / f"continue-{AGENT}").read_text().strip() == "8"


def test_stop_hook_shell_quotes_marker_path_in_model_command(tmp_path):
    run_hook, bus_dir = make_hook_runner(
        tmp_path,
        ['0|[{"id":"1","body":"claim issue"}]'],
        bus_dir_name='bus"; touch pwned #',
    )
    done_marker = bus_dir / f"turn-done-{AGENT}"

    reason = parse_block(run_hook())
    command = touch_command_from(reason)

    subprocess.run(["bash", "-lc", command], cwd=tmp_path, check=True)

    assert done_marker.exists()
    assert not (tmp_path / "pwned").exists()
