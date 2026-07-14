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


def test_agents_command_shows_resolved_auto_backend(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: dual\n    backend: auto\n"
        "    command: [\"echo\", \"{prompt}\"]\n"
        "    provider: openai\n    model: m\n    api_key_env: MISSING_KEY\n"
    )
    cli.main(["agents"])
    out = capsys.readouterr().out
    assert "ready (cli)" in out


def test_run_skips_agents_that_cannot_respond(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "  - name: b\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "  - name: keyless\n    backend: api\n    provider: openai\n"
        "    model: m\n    api_key_env: MISSING_KEY\n"
    )
    cli.main(["run", debate_id, "--max-rounds", "1"])
    out = capsys.readouterr().out
    assert "skipping agent 'keyless'" in out
    assert "MISSING_KEY" in out
    assert "final status:" in out


def test_run_exits_when_skipping_leaves_too_few_agents(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "  - name: keyless\n    backend: api\n    provider: openai\n"
        "    model: m\n    api_key_env: MISSING_KEY\n"
    )
    with pytest.raises(SystemExit, match="at least 2"):
        cli.main(["run", debate_id])


def test_run_needs_two_enabled_agents(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: only\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
    )
    with pytest.raises(SystemExit, match="at least 2"):
        cli.main(["run", debate_id])


def test_status_rejects_traversal_id_without_reading_outside_root(workdir):
    outside = workdir / "outside"
    outside.mkdir()
    (outside / "state.json").write_text('{"id": "stolen"}')
    with pytest.raises(SystemExit, match="invalid debate id"):
        cli.main(["status", "../outside"])


def test_approve_rejects_traversal_id_without_writing_outside_root(workdir):
    outside = workdir / "outside"
    outside.mkdir()
    state_file = outside / "state.json"
    original = '{"status": "awaiting_human", "round": 0}'
    state_file.write_text(original)
    with pytest.raises(SystemExit, match="invalid debate id"):
        cli.main(["approve", "../outside"])
    assert state_file.read_text() == original
    assert not (outside / "transcript.jsonl").exists()


def test_status_rejects_symlinked_debate_without_reading_outside_root(workdir):
    root = workdir / "debates"
    outside = workdir / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "state.json").write_text(
        '{"id":"example","status":"approved","round":99,"max_rounds":99}'
    )
    (root / "example").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="outside debate root|symlink"):
        cli.main(["status", "example"])


def test_approve_rejects_symlinked_debate_without_writing_outside(workdir):
    root = workdir / "debates"
    outside = workdir / "outside"
    root.mkdir()
    outside.mkdir()
    state_file = outside / "state.json"
    transcript_file = outside / "transcript.jsonl"
    summary_file = outside / "summary.md"
    state_text = '{"status":"awaiting_human","round":0}'
    state_file.write_text(state_text)
    transcript_file.write_text("")
    summary_file.write_text("outside summary\n")
    (root / "example").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SystemExit, match="outside debate root|symlink"):
        cli.main(["approve", "example", "-m", "do not write"])
    assert state_file.read_text() == state_text
    assert transcript_file.read_text() == ""
    assert summary_file.read_text() == "outside summary\n"


def test_max_rounds_must_be_positive(workdir):
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", "any-id", "--max-rounds", "0"])
    assert exc.value.code == 2


def test_decision_recovers_after_event_append_interruption(workdir, capsys, monkeypatch):
    store, debate_id = _make_awaiting(workdir, capsys)
    real_write_state = DebateStore.write_state
    interrupted = False

    def interrupt_once(self, did, state):
        nonlocal interrupted
        if did == debate_id and state["status"] == "approved" and not interrupted:
            interrupted = True
            raise RuntimeError("interrupted after transcript append")
        real_write_state(self, did, state)

    monkeypatch.setattr(DebateStore, "write_state", interrupt_once)
    with pytest.raises(RuntimeError, match="interrupted"):
        cli.main(["approve", debate_id, "-m", "ship it"])
    assert store.read_state(debate_id)["status"] == "awaiting_human"

    monkeypatch.setattr(DebateStore, "write_state", real_write_state)
    cli.main(["approve", debate_id, "-m", "ship it"])
    state = store.read_state(debate_id)
    assert state["status"] == "approved"
    assert state["human_decision"] == {"decision": "approved", "note": "ship it"}
    events = [e for e in store.read_events(debate_id) if e["type"] == "human_decision"]
    assert len(events) == 1
    assert set(events[0]) == {
        "ts", "round", "phase", "agent", "type", "content", "note"
    }
    assert events[0]["content"] == "approved"
    assert events[0]["note"] == "ship it"
    assert events[0]["round"] == 0
    assert events[0]["phase"] == "human"
    assert events[0]["agent"] == "human"
    assert events[0]["type"] == "human_decision"
    assert "APPROVED" in store.read_summary(debate_id)
    index = json.loads((workdir / "debates" / "index.json").read_text())
    assert index[0]["status"] == "approved"

    cli.main(["approve", debate_id, "-m", "ship it"])
    assert len([e for e in store.read_events(debate_id) if e["type"] == "human_decision"]) == 1
    with pytest.raises(SystemExit, match="already approved.*conflicts"):
        cli.main(["reject", debate_id, "-m", "changed mind"])


def test_no_consensus_can_be_decided(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    state = store.read_state(debate_id)
    state["status"] = "no_consensus"
    store.write_state(debate_id, state)
    cli.main(["reject", debate_id, "-m", "insufficient agreement"])
    assert store.read_state(debate_id)["human_decision"] == {
        "decision": "rejected", "note": "insufficient agreement"
    }
