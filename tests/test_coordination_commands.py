import json
import subprocess
from unittest import mock


def test_set_status_label_removes_only_current_status_labels(bus_module, monkeypatch):
    calls = []
    monkeypatch.setattr(
        bus_module,
        "issue_labels",
        lambda _issue: ["status:open", "bug", "status:claimed"],
    )
    monkeypatch.setattr(
        bus_module,
        "gh",
        lambda args, check=True: calls.append(args) or (0, "", ""),
    )

    bus_module.set_status_label(79, "status:pr-open")

    assert calls == [[
        "issue",
        "edit",
        "79",
        "--add-label",
        "status:pr-open",
        "--remove-label",
        "status:open",
        "--remove-label",
        "status:claimed",
    ]]


def test_set_status_label_read_failure_adds_only_new_label(bus_module, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: None)
    monkeypatch.setattr(bus_module, "gh", lambda args, check=True: calls.append(args) or (0, "", ""))

    bus_module.set_status_label(79, "status:verified")

    assert calls == [["issue", "edit", "79", "--add-label", "status:verified"]]
    assert "could not read labels" in capsys.readouterr().err


def test_status_renews_holder_and_announces(bus_module, fake_redis, ns, monkeypatch, capsys):
    status = mock.Mock()
    gh = mock.Mock(return_value=(0, "", ""))
    # #71: cmd_status reads the live label to enforce transition legality; seed a
    # current status so claimed -> pr-open is a legal forward move.
    monkeypatch.setattr(bus_module, "issue_labels", lambda _issue: ["status:claimed"])
    monkeypatch.setattr(bus_module, "set_status_label", status)
    monkeypatch.setattr(bus_module, "gh", gh)
    fake_redis.set(bus_module.k_lock(79), "alice", ex=10)

    assert bus_module.cmd_status(
        fake_redis,
        ns(as_agent="alice", issue=79, set="status:pr-open", ttl=120, force=False),
    ) == 0

    assert fake_redis.ttl(bus_module.k_lock(79)) > 10
    assert fake_redis.xlen(bus_module.k_stream("main")) == 1
    assert "status:pr-open" in capsys.readouterr().out
    status.assert_called_once_with(79, "status:pr-open", current_labels=["status:claimed"])
    gh.assert_called_once()


def test_board_json_combines_github_locks_and_presence(bus_module, fake_redis, ns, monkeypatch, capsys):
    monkeypatch.setattr(bus_module, "GH_REPO", "owner/repo")
    issues = [
        {"number": 2, "title": "Second", "labels": [{"name": "status:open"}]},
        {"number": 10, "title": "Tenth", "labels": [{"name": "status:claimed"}]},
    ]
    monkeypatch.setattr(
        bus_module,
        "gh",
        lambda *_args, **_kwargs: (0, json.dumps(issues), ""),
    )
    fake_redis.set(bus_module.k_lock(10), "alice")
    fake_redis.set(bus_module.k_lock(12), "huddle:issue-12:session")
    fake_redis.set(bus_module.k_presence("main", "alice"), "now")

    assert bus_module.cmd_board(fake_redis, ns(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)

    assert [row["issue"] for row in rows] == ["2", "10", "12"]
    assert rows[1]["present"] == "yes"
    assert rows[2]["present"] == "sesh"


def test_reap_lists_and_releases_only_absent_non_huddle_locks(
    bus_module, fake_redis, ns, capsys
):
    fake_redis.set(bus_module.k_lock(1), "alice")
    fake_redis.set(bus_module.k_lock(2), "bob")
    fake_redis.set(bus_module.k_lock(3), "huddle:issue-3:session")
    fake_redis.set(bus_module.k_presence("main", "bob"), "now")

    assert bus_module.cmd_reap(fake_redis, ns(json=True, release=None)) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"issue": "1", "holder": "alice", "ttl": -1}
    ]

    assert bus_module.cmd_reap(fake_redis, ns(json=False, release=1)) == 0
    assert fake_redis.get(bus_module.k_lock(1)) is None
    assert "released stale lock" in capsys.readouterr().out

    assert bus_module.cmd_reap(fake_redis, ns(json=False, release=2)) == 1
    assert fake_redis.get(bus_module.k_lock(2)) == "bob"


def test_init_and_doctor_paths(bus_module, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(bus_module, "GH_REPO", "")
    assert bus_module.cmd_init(mock.Mock()) == 2
    assert "BUS_GH_REPO" in capsys.readouterr().err

    monkeypatch.setattr(bus_module, "GH_REPO", "owner/repo")
    monkeypatch.setattr(bus_module, "gh", lambda args, check=False: calls.append(args) or (0, "", ""))
    assert bus_module.cmd_init(mock.Mock()) == 0
    assert len(calls) == len(bus_module.STATUS_LABELS)

    monkeypatch.setattr(bus_module, "connect", lambda _url: object())
    monkeypatch.setattr(bus_module, "main_repo_dir", lambda: "/repo")
    monkeypatch.setattr(bus_module, "worktree_root", lambda main: f"{main}-worktrees")
    monkeypatch.setattr(
        bus_module.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "git version 2.0\n", ""),
    )
    assert bus_module.cmd_doctor("redis://local") == 0
    out = capsys.readouterr().out
    assert "redis: OK" in out
    assert "gh: OK" in out


def test_parser_validation_and_main_dispatch(bus_module, fake_redis, monkeypatch, capsys):
    parser = bus_module.build_parser()
    parsed = parser.parse_args(["--room", "dev", "send", "--from", "alice", "--to", "bob", "hi"])
    assert parsed.room == "dev"
    assert parsed.cmd == "send"
    assert bus_module.positive_int("2") == 2

    for bad in ["bad room", "bad/slash"]:
        try:
            bus_module.ident(bad)
        except Exception as exc:
            assert "must match" in str(exc)
        else:
            raise AssertionError("ident accepted invalid value")

    monkeypatch.setattr(bus_module, "connect", lambda _url: fake_redis)
    assert bus_module.main(["join", "--as", "alice"]) == 0
    assert "alice joined" in capsys.readouterr().out

    monkeypatch.setattr(bus_module, "connect", lambda _url: (_ for _ in ()).throw(RuntimeError("down")))
    assert bus_module.main(["agents"]) == 3
    assert "cannot reach Redis" in capsys.readouterr().err
