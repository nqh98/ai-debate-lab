import json
from contextlib import contextmanager
from fractions import Fraction

import pytest

from debatelab import cli
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import happy_agent, make_store


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


def _record_consensus(store, debate_id, answer="the answer"):
    store.append_event(debate_id, {
        "round": 1,
        "phase": "vote",
        "agent": "a",
        "type": "consensus",
        "content": answer,
        "tally": {
            "accepts": 2,
            "rejects": 0,
            "abstains": 0,
            "roster_size": 2,
            "required": 2,
        },
    })


def test_result_for_awaiting_human_prints_no_answer_and_exits_one(
    workdir, capsys
):
    store, debate_id = _make_awaiting(workdir, capsys)
    _record_consensus(store, debate_id)

    with pytest.raises(SystemExit) as exc:
        cli.main(["result", debate_id])

    assert exc.value.code == 1
    assert "# No answer" in capsys.readouterr().out


def test_result_after_approval_prints_answer_and_exits_zero(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    _record_consensus(store, debate_id, "ship postgres")
    cli.main(["approve", debate_id])
    capsys.readouterr()

    cli.main(["result", debate_id])

    out = capsys.readouterr().out
    assert "# Answer" in out
    assert "ship postgres" in out


def test_result_json_prints_parseable_answer(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    _record_consensus(store, debate_id, "ship postgres")
    cli.main(["approve", debate_id])
    capsys.readouterr()

    cli.main(["result", debate_id, "--json"])

    assert json.loads(capsys.readouterr().out)["answer"] == "ship postgres"


def test_result_requires_an_answer_even_when_the_result_is_approved(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    state = store.read_state(debate_id)
    state["status"] = "no_consensus"
    store.write_state(debate_id, state)
    store.append_event(debate_id, {
        "round": 1,
        "phase": "end",
        "agent": None,
        "type": "no_consensus",
        "content": "no quorum",
    })
    cli.main(["approve", debate_id])
    capsys.readouterr()

    with pytest.raises(SystemExit) as exc:
        cli.main(["result", debate_id, "--json"])

    assert exc.value.code == 1
    decision_event = next(
        event for event in store.read_events(debate_id)
        if event["type"] == "human_decision"
    )
    assert json.loads(capsys.readouterr().out) == {
        "id": debate_id,
        "title": "Topic",
        "status": "approved",
        "answer": None,
        "candidate": None,
        "tally": None,
        "decided_at": decision_event["ts"],
        "note": "",
        "reason": "approved without a candidate",
        "round": 1,
        "failed_phase": None,
    }

    with pytest.raises(SystemExit) as exc:
        cli.main(["result", debate_id])

    assert exc.value.code == 1
    assert capsys.readouterr().out == (
        "# No answer\n\n"
        "approved without a candidate\n\n"
        "The full debate is in `summary.md`.\n"
    )


def test_result_reads_a_pre_genesis_transcript_without_error(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    _record_consensus(store, debate_id)
    transcript = store.path(debate_id) / "transcript.jsonl"
    legacy_events = store.read_events(debate_id)[1:]
    transcript.write_text(
        "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in legacy_events)
    )

    with pytest.raises(SystemExit) as exc:
        cli.main(["result", debate_id])

    assert exc.value.code == 1
    assert "# No answer" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "exit_code"),
    [
        ("awaiting_human", None),
        ("no_consensus", 1),
        ("error", 3),
        ("approved", 1),
        ("rejected", 1),
        ("created", 1),
        ("running", 1),
    ],
)
def test_run_uses_terminal_status_exit_codes(
    workdir, capsys, monkeypatch, status, exit_code
):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return status

    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(cli.registry, "load_agent_specs", lambda config: [])
    monkeypatch.setattr(cli.registry, "build_agents", lambda specs: ["a", "b"])
    from debatelab import orchestrator
    monkeypatch.setattr(orchestrator, "Orchestrator", FakeOrchestrator)

    if exit_code is None:
        cli.main(["run", debate_id])
    else:
        with pytest.raises(SystemExit) as exc:
            cli.main(["run", debate_id])
        assert exc.value.code == exit_code


@pytest.mark.parametrize("status", ["approved", "rejected"])
def test_run_exits_one_for_a_predecided_debate(
    workdir, capsys, monkeypatch, status
):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")
    state = store.read_state(debate_id)
    state["status"] = status
    store.write_state(debate_id, state)

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return status

    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(cli.registry, "load_agent_specs", lambda config: [])
    monkeypatch.setattr(cli.registry, "build_agents", lambda specs: ["a", "b"])
    from debatelab import orchestrator
    monkeypatch.setattr(orchestrator, "Orchestrator", FakeOrchestrator)

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", debate_id])

    assert exc.value.code == 1


def test_decide_and_reconciliation_regenerate_all_derived_views(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    _record_consensus(store, debate_id)

    cli.main(["approve", debate_id])
    debate_path = store.path(debate_id)
    for name in ("summary.md", "result.json", "final.md"):
        assert (debate_path / name).exists()
    assert json.loads((debate_path / "result.json").read_text())["answer"] == "the answer"

    for name in ("summary.md", "result.json", "final.md"):
        (debate_path / name).unlink()
    cli.main(["approve", debate_id])

    for name in ("summary.md", "result.json", "final.md"):
        assert (debate_path / name).exists()


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


def test_run_forwards_force_to_store_lock(workdir, capsys, monkeypatch):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")
    calls = {}

    @contextmanager
    def debate_lock(debate_id, *, command, force=False):
        calls["debate_id"] = debate_id
        calls["command"] = command
        calls["force"] = force
        yield

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, *args, **kwargs):
            return "no_consensus"

    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(DebateStore, "debate_lock", lambda self, *args, **kwargs:
                        debate_lock(*args, **kwargs))
    monkeypatch.setattr(cli.registry, "load_agent_specs", lambda config: [])
    monkeypatch.setattr(cli.registry, "build_agents", lambda specs: ["a", "b"])
    from debatelab import orchestrator
    monkeypatch.setattr(orchestrator, "Orchestrator", FakeOrchestrator)

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", debate_id, "--force"])

    assert exc.value.code == 1
    assert calls == {"debate_id": debate_id, "command": "run", "force": True}


def test_run_forwards_quorum_to_orchestrator(workdir, capsys, monkeypatch):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")
    calls = {}

    class FakeOrchestrator:
        def __init__(self, *args, **kwargs):
            pass

        def run(self, debate_id, max_rounds=None, quorum=None):
            calls.update({
                "debate_id": debate_id,
                "max_rounds": max_rounds,
                "quorum": quorum,
            })
            return "no_consensus"

    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.setattr(cli.registry, "load_agent_specs", lambda config: [])
    monkeypatch.setattr(cli.registry, "build_agents", lambda specs: ["a", "b"])
    from debatelab import orchestrator
    monkeypatch.setattr(orchestrator, "Orchestrator", FakeOrchestrator)

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", debate_id, "--quorum", "3/4"])

    assert exc.value.code == 1
    assert calls == {
        "debate_id": debate_id,
        "max_rounds": None,
        "quorum": Fraction(3, 4),
    }


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


def test_quorum_fraction_accepts_fractions_and_rejects_junk():
    import argparse
    from fractions import Fraction

    assert cli.quorum_fraction("2/3") == Fraction(2, 3)
    assert cli.quorum_fraction("1") == Fraction(1)
    for bad in ("banana", "0", "5/4", "-1/2"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.quorum_fraction(bad)


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


def _run_a_debate(workdir):
    store = make_store(workdir)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    return store, did


def test_fsck_reports_ok_on_a_healthy_debate(workdir, capsys):
    _store, did = _run_a_debate(workdir)
    cli.main(["fsck", did])
    assert capsys.readouterr().out.strip() == f"{did}: ok"


def test_fsck_reports_ok_on_a_created_but_unrun_debate(workdir, capsys):
    """No boundary event exists, so the prefix is genesis alone — compared
    against exactly what create() wrote. A real check, not a vacuous one."""
    cli.main(["new", "Pick a database"])
    did = capsys.readouterr().out.strip()
    cli.main(["fsck", did])
    assert capsys.readouterr().out.strip() == f"{did}: ok"


def test_fsck_notes_events_in_flight_after_the_last_checkpoint(
    workdir, capsys
):
    """state.json is the last checkpoint, not the latest truth. A hard crash
    leaves events past it, and reporting that as divergence would cry wolf on
    exactly the debates fsck exists to inspect."""
    store, did = _run_a_debate(workdir)
    store.append_event(did, {
        "round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
        "task": "deep", "attempt": 1, "duration_ms": 5, "ok": True,
        "content": "",
    })
    cli.main(["fsck", did])
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"{did}: ok")
    assert "1 event in flight" in out


def test_fsck_accepts_a_phase_completed_after_the_current_checkpoint(
    workdir, capsys
):
    store, did = _run_a_debate(workdir)
    store.append_event(did, {
        "round": 2, "phase": "critique", "agent": None,
        "type": "phase_started", "content": "",
    })
    store.append_event(did, {
        "round": 2, "phase": "critique", "agent": None,
        "type": "phase_completed", "content": "",
    })
    cli.main(["fsck", did])
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"{did}: ok")
    assert "2 events in flight" in out


def test_fsck_accepts_a_human_decision_after_the_current_checkpoint(
    workdir, capsys
):
    store, did = _run_a_debate(workdir)
    store.append_event(did, {
        "round": 1, "phase": "human", "agent": "human",
        "type": "human_decision", "content": "approved", "note": "ship it",
    })
    cli.main(["fsck", did])
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"{did}: ok")
    assert "1 event in flight" in out


def test_fsck_reports_diverged_and_names_the_key(workdir, capsys):
    store, did = _run_a_debate(workdir)
    state = store.read_state(did)
    state["status"] = "approved"          # a lie the transcript does not tell
    store.write_state(did, state)
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", did])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"{did}: diverged" in out
    assert "status" in out
    assert "awaiting_human" in out


def test_fsck_reports_a_missing_nullable_key(workdir, capsys):
    store, did = _run_a_debate(workdir)
    state = store.read_state(did)
    del state["human_decision"]
    store.write_state(did, state)
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", did])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "human_decision" in out
    assert "<missing>" in out


def test_fsck_reports_unverifiable_on_a_pre_genesis_debate(workdir, capsys):
    """The four committed debates predate genesis events. They are refused,
    not guessed at, and not migrated."""
    store, did = _run_a_debate(workdir)
    events = store.read_events(did)[1:]           # strip debate_created
    path = store.path(did) / "transcript.jsonl"
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", did])
    assert exc.value.code == 3
    assert "unverifiable" in capsys.readouterr().out


def test_fsck_on_a_missing_debate_fails_cleanly(workdir):
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", "no-such-debate"])
    assert exc.value.code != 0
