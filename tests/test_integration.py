"""Full pipeline: new -> run (2 rounds to consensus) -> approve, all via cli.main."""
import json

import pytest

from debatelab import cli
from debatelab.agents import registry
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent


def scripted_agents():
    # Round 1: propose, critique, revise, nominate, vote (charlie rejects).
    # Round 2: critique, revise, nominate, vote (all accept).
    alpha = MockAgent("alpha", [
        "use postgres",
        "bravo's idea lacks indexes; charlie ignores cost",
        "use postgres with read replicas",
        "NOMINATE: alpha\nmost complete",
        "VOTE: accept\nfine",
        "charlie's concern is addressed now",
        "use postgres with read replicas and pgbouncer",
        "NOMINATE: alpha\nstill best",
        "VOTE: accept\ngood",
    ])
    bravo = MockAgent("bravo", [
        "use mysql",
        "alpha is solid; charlie's is vague",
        "postgres is fine actually",
        "NOMINATE: alpha\nconvinced",
        "VOTE: accept\nworks",
        "agree with the pooling addition",
        "postgres with replicas works",
        "NOMINATE: alpha\nyes",
        "VOTE: accept\nship it",
    ])
    charlie = MockAgent("charlie", [
        "use sqlite",
        "both overkill for our scale",
        "fine, postgres, but keep it simple",
        "NOMINATE: alpha\nok",
        "VOTE: reject\nno connection pooling story",
        "pooling is in now",
        "postgres with pgbouncer is acceptable",
        "NOMINATE: alpha\nagreed",
        "VOTE: accept\nsatisfied",
    ])
    return [alpha, bravo, charlie]


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.yaml").write_text("agents: []\n")
    monkeypatch.setattr(registry, "load_agent_specs", lambda path: [])
    monkeypatch.setattr(registry, "build_agents", lambda specs: scripted_agents())
    return tmp_path


def test_full_debate_to_approval(workdir, capsys):
    cli.main(["new", "Which database should we use?"])
    debate_id = capsys.readouterr().out.strip()

    cli.main(["run", debate_id])
    out = capsys.readouterr().out
    assert "final status: awaiting_human" in out

    store = DebateStore(workdir / "debates")
    state = store.read_state(debate_id)
    assert state["round"] == 2
    assert state["candidate"]["agent"] == "alpha"
    assert "pgbouncer" in state["candidate"]["text"]

    events = store.read_events(debate_id)
    rejects = [e for e in events if e["type"] == "vote" and e["verdict"] == "reject"]
    assert len(rejects) == 1 and rejects[0]["agent"] == "charlie"
    assert sum(1 for e in events if e["type"] == "proposal") == 3  # round 1 only
    assert any(e["type"] == "consensus" for e in events)
    assert any(e["round"] == 2 and e["type"] == "critique" for e in events)

    # Human gate: summary is pending until approval.
    assert "pending human decision" in store.read_summary(debate_id)
    cli.main(["approve", debate_id, "-m", "ship it"])
    capsys.readouterr()
    state = store.read_state(debate_id)
    assert state["status"] == "approved"
    assert state["human_decision"]["note"] == "ship it"
    assert "APPROVED" in store.read_summary(debate_id)
    assert any(e["type"] == "human_decision" for e in store.read_events(debate_id))

    index = json.loads((workdir / "debates" / "index.json").read_text())
    assert index[0]["status"] == "approved"


def test_no_consensus_candidate_can_be_approved_and_returned(workdir, capsys, monkeypatch):
    agents = [
        MockAgent("alpha", [
            "use postgres", "critique", "use postgres with replicas",
            "NOMINATE: alpha", "VOTE: accept\\nyes",
        ]),
        MockAgent("bravo", [
            "use mysql", "critique", "use mysql",
            "NOMINATE: alpha", "VOTE: reject\\nno",
        ]),
    ]
    monkeypatch.setattr(registry, "build_agents", lambda specs: agents)
    cli.main(["new", "Which database should we use?"])
    debate_id = capsys.readouterr().out.strip()

    with pytest.raises(SystemExit) as exc:
        cli.main(["run", debate_id, "--max-rounds", "1"])
    assert exc.value.code == 1
    capsys.readouterr()

    cli.main(["approve", debate_id])
    capsys.readouterr()
    cli.main(["result", debate_id])

    out = capsys.readouterr().out
    assert "# Answer" in out
    assert "use postgres with replicas" in out


def test_reject_reasons_reach_round_two_prompts(workdir, capsys, monkeypatch):
    cli.main(["new", "Which database should we use?"])
    debate_id = capsys.readouterr().out.strip()
    # Patch build_agents to return this exact list so we can inspect prompts.
    agents = scripted_agents()
    monkeypatch.setattr(registry, "build_agents", lambda specs: agents)
    cli.main(["run", debate_id])
    capsys.readouterr()
    alpha = agents[0]
    round2_critique_prompt = alpha.prompts[5]  # calls 0-4 are round 1
    assert "Rejection reasons" in round2_critique_prompt
    assert "no connection pooling story" in round2_critique_prompt


def test_checkpoint_writes_all_derived_views_atomically(workdir):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")

    Orchestrator(store, scripted_agents()).run(debate_id)

    debate_path = store.path(debate_id)
    assert (debate_path / "summary.md").exists()
    assert json.loads((debate_path / "result.json").read_text())["status"] == "awaiting_human"
    assert (debate_path / "final.md").read_text().startswith("# No answer")
    assert not list(debate_path.glob("*.tmp"))
