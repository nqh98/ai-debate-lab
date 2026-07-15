"""Full pipeline: new -> run (2 rounds to consensus) -> approve, all via cli.main."""
import json

import pytest

from debatelab import cli, prompts
from debatelab.agents import registry
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent, happy_agent, make_store


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
    ], synthesis="use postgres with read replicas and pgbouncer")
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
        ], synthesis="use postgres with replicas"),
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


def test_lower_cap_resume_keeps_checkpointed_candidate_for_approval(workdir, capsys):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")
    first_agents = [
        MockAgent("alpha", [
            "checkpointed answer", "critique", "checkpointed answer",
            "NOMINATE: alpha", "VOTE: accept",
        ], synthesis="checkpointed answer"),
        MockAgent("bravo", [
            "other", "critique", "other",
            "NOMINATE: alpha", "VOTE: reject",
        ]),
    ]

    assert Orchestrator(store, first_agents).run(debate_id, max_rounds=1) == "no_consensus"
    checkpointed = store.read_state(debate_id)["candidate"]
    assert checkpointed["text"] == "checkpointed answer"

    assert Orchestrator(
        store, [MockAgent("alpha", []), MockAgent("bravo", [])]
    ).run(debate_id, max_rounds=1) == "no_consensus"
    cli.main(["approve", debate_id])
    capsys.readouterr()

    result = store.read_result(debate_id)
    assert result["answer"] == checkpointed["text"]
    assert result["candidate"] == {
        "agent": checkpointed["agent"], "round": 1, "synthesized": True,
    }
    assert checkpointed["text"] in store.read_final(debate_id)


def test_lower_cap_resume_ignores_candidate_after_interrupted_vote_checkpoint(
    workdir, capsys, monkeypatch
):
    store = DebateStore(workdir / "debates")
    debate_id = store.create("Topic", "problem")
    first_agents = [
        MockAgent("alpha", [
            "checkpointed answer", "critique", "checkpointed answer",
            "NOMINATE: alpha", "VOTE: accept",
        ]),
        MockAgent("bravo", [
            "other", "critique", "other",
            "NOMINATE: alpha", "VOTE: reject",
        ]),
    ]
    assert Orchestrator(store, first_agents).run(debate_id, max_rounds=1) == "no_consensus"
    checkpointed = store.read_state(debate_id)["candidate"]

    real_append_event = store.append_event

    def interrupt_after_candidate(did, event):
        real_append_event(did, event)
        if event["type"] == "candidate":
            raise RuntimeError("interrupted after candidate event")

    monkeypatch.setattr(store, "append_event", interrupt_after_candidate)
    interrupted_agents = [
        MockAgent("alpha", [
            "critique", "uncommitted answer", "NOMINATE: alpha", "VOTE: accept",
        ]),
        MockAgent("bravo", [
            "critique", "other", "NOMINATE: alpha", "VOTE: reject",
        ]),
    ]
    with pytest.raises(RuntimeError, match="interrupted after candidate event"):
        Orchestrator(store, interrupted_agents).run(debate_id, max_rounds=2)
    assert store.read_state(debate_id)["candidate"] == checkpointed

    monkeypatch.setattr(store, "append_event", real_append_event)
    assert Orchestrator(
        store, [MockAgent("alpha", []), MockAgent("bravo", [])]
    ).run(debate_id, max_rounds=1) == "no_consensus"
    cli.main(["approve", debate_id])
    capsys.readouterr()

    result = store.read_result(debate_id)
    final = store.read_final(debate_id)
    assert store.read_state(debate_id)["candidate"] == checkpointed
    assert result["answer"] == checkpointed["text"]
    assert checkpointed["text"] in final
    assert "uncommitted answer" not in result["answer"]
    assert "uncommitted answer" not in final


def test_reject_reasons_reach_round_two_prompts(workdir, capsys, monkeypatch):
    cli.main(["new", "Which database should we use?"])
    debate_id = capsys.readouterr().out.strip()
    # Patch build_agents to return this exact list so we can inspect prompts.
    agents = scripted_agents()
    monkeypatch.setattr(registry, "build_agents", lambda specs: agents)
    cli.main(["run", debate_id])
    capsys.readouterr()
    alpha = agents[0]
    # Round 1 makes six calls: propose, critique, revise, nominate,
    # synthesize (alpha wins), vote -- indices 0-5.
    round2_critique_prompt = alpha.prompts[6]
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


def test_the_answer_is_a_merge_and_carries_no_changelog(tmp_path, monkeypatch):
    """The end-to-end shape of the cycle: the approved answer is the merged
    document, not the winning agent's revision with its diff notes on top."""
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    a = MockAgent("a", [
        "proposal from a",
        "critique from a",
        "Changes: tightened the intro\nrevised proposal from a",
        "NOMINATE: a\nbest one",
        "VOTE: accept\nagreed",
    ], synthesis="the merged answer")
    Orchestrator(store, [a, happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )
    cli.main(["approve", did, "-m", "ship it"])

    final = (store.root / did / "final.md").read_text()
    assert "the merged answer" in final
    assert "Changes:" not in final
    assert "synthesized by **a**" in final

    result = json.loads((store.root / did / "result.json").read_text())
    assert result["answer"] == "the merged answer"
    assert result["candidate"]["synthesized"] is True


def test_a_resumed_debate_does_not_synthesize_twice(tmp_path):
    """The phase split's whole justification: a halt after synthesize must
    not re-run the DEEP call on resume."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    dead_vote = [
        "proposal from {n}", "critique from {n}", "revised proposal from {n}",
        "NOMINATE: a\nbest one",
    ]

    def stops_before_voting(name):
        return MockAgent(name, [r.format(n=name) for r in dead_vote])

    a = MockAgent("a", [r.format(n="a") for r in dead_vote])
    Orchestrator(
        store, [a, stops_before_voting("b"), stops_before_voting("c")]
    ).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "error"
    assert store.read_state(did)["last_completed_phase"] == "synthesize"
    first = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["phase"] == "synthesize"
    ]
    assert len(first) == 1

    a2 = MockAgent("a", ["VOTE: accept\nagreed"])
    Orchestrator(store, [
        a2,
        MockAgent("b", ["VOTE: accept\nagreed"]),
        MockAgent("c", ["VOTE: accept\nagreed"]),
    ]).run(did)
    after = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["phase"] == "synthesize"
    ]
    assert len(after) == 1, "resume re-ran the synthesis"
    assert not [p for p in a2.prompts if prompts.SYNTHESIS_HEADER in p]
    assert store.read_state(did)["status"] == "awaiting_human"
