import json

import pytest

from debatelab import cli
from debatelab.store import DebateStore


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_new_creates_debate_and_prints_id(workdir, capsys):
    cli.main(["new", "Pick a database"])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    assert store.read_state(debate_id)["status"] == "created"


def test_new_with_context_files(workdir, capsys):
    ctx = workdir / "notes.md"
    ctx.write_text("important context")
    cli.main(["new", "Pick a database", "--context", str(ctx)])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    assert "important context" in store.read_problem(debate_id)


def test_status_and_list(workdir, capsys):
    cli.main(["new", "First topic"])
    debate_id = capsys.readouterr().out.strip()
    cli.main(["status", debate_id])
    out = capsys.readouterr().out
    assert debate_id in out and "created" in out
    cli.main(["list"])
    assert debate_id in capsys.readouterr().out


def test_show_prints_summary(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    store.write_summary(debate_id, "# my summary\n")
    cli.main(["show", debate_id])
    assert "# my summary" in capsys.readouterr().out


def _make_awaiting(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    state = store.read_state(debate_id)
    state["status"] = "awaiting_human"
    state["candidate"] = {"agent": "a", "text": "the answer"}
    store.write_state(debate_id, state)
    return store, debate_id


def test_approve_records_decision(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    cli.main(["approve", debate_id, "-m", "looks right"])
    state = store.read_state(debate_id)
    assert state["status"] == "approved"
    assert state["human_decision"] == {"decision": "approved", "note": "looks right"}
    events = store.read_events(debate_id)
    assert events[-1]["type"] == "human_decision"
    assert "APPROVED" in store.read_summary(debate_id)
    index = json.loads((workdir / "debates" / "index.json").read_text())
    assert index[0]["status"] == "approved"


def test_reject_requires_message(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    with pytest.raises(SystemExit):
        cli.main(["reject", debate_id])
    cli.main(["reject", debate_id, "-m", "not convincing"])
    assert store.read_state(debate_id)["status"] == "rejected"


def test_approve_wrong_status_exits(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    with pytest.raises(SystemExit):
        cli.main(["approve", debate_id])


def test_agents_command_reports_readiness(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: echoer\n    backend: cli\n"
        "    command: [\"echo\", \"{prompt}\"]\n"
        "  - name: keyless\n    backend: api\n    provider: openai\n"
        "    model: m\n    api_key_env: MISSING_KEY\n"
        "  - name: off\n    backend: cli\n    command: [\"echo\"]\n"
        "    enabled: false\n"
    )
    cli.main(["agents"])
    out = capsys.readouterr().out
    assert "echoer" in out and "ready" in out
    assert "MISSING_KEY" in out
    assert "disabled" in out


def test_run_needs_two_enabled_agents(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: only\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
    )
    with pytest.raises(SystemExit, match="at least 2"):
        cli.main(["run", debate_id])
