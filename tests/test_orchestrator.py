import pytest

from debatelab.agents.base import AgentError
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent


def make_store(tmp_path):
    return DebateStore(tmp_path / "debates")


def happy_agent(name, nominee="a"):
    return MockAgent(name, [
        f"proposal from {name}",
        f"critique from {name}",
        f"revised proposal from {name}",
        f"NOMINATE: {nominee}\nbest one",
        "VOTE: accept\nagreed",
    ])


def test_requires_two_agents(tmp_path):
    with pytest.raises(ValueError):
        Orchestrator(make_store(tmp_path), [MockAgent("solo", [])])


def test_single_round_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    agents = [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    status = Orchestrator(store, agents).run(did)
    assert status == "awaiting_human"
    state = store.read_state(did)
    assert state["status"] == "awaiting_human"
    assert state["round"] == 1
    assert state["candidate"]["agent"] == "a"
    assert state["candidate"]["text"] == "revised proposal from a"
    assert all(v["vote"] == "accept" for v in state["votes"].values())
    types = [e["type"] for e in store.read_events(did)]
    for expected in ("proposal", "critique", "revision", "nomination",
                     "candidate", "vote", "consensus"):
        assert expected in types
    assert "pending human decision" in store.read_summary(did)


def test_no_consensus_after_max_rounds(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    dissenter = MockAgent("c", [
        "proposal from c", "critique from c", "revised from c",
        "NOMINATE: a\nok", "VOTE: reject\nstill wrong",
    ])
    agents = [happy_agent("a"), happy_agent("b"), dissenter]
    status = Orchestrator(store, agents).run(did, max_rounds=1)
    assert status == "no_consensus"
    state = store.read_state(did)
    assert state["votes"]["c"]["vote"] == "reject"
    assert any(e["type"] == "no_consensus" for e in store.read_events(did))


def test_retry_once_then_succeed(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    flaky = MockAgent("b", [
        AgentError("blip"), "proposal from b",
        "critique from b", "revised from b",
        "NOMINATE: a\nok", "VOTE: accept\nok",
    ])
    agents = [happy_agent("a"), flaky, happy_agent("c")]
    status = Orchestrator(store, agents).run(did)
    assert status == "awaiting_human"
    assert store.read_state(did)["abstained"] == []


def test_double_failure_abstains_and_continues(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    broken = MockAgent("b", [
        AgentError("down"), AgentError("still down"),
        "critique from b", "revised from b",
        "NOMINATE: a\nok", "VOTE: accept\nok",
    ])
    agents = [happy_agent("a"), broken, happy_agent("c")]
    status = Orchestrator(store, agents).run(did)
    assert status == "awaiting_human"
    events = store.read_events(did)
    assert any(e["type"] == "abstained" and e["agent"] == "b" for e in events)


def test_too_few_responders_halts_with_error(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    agents = [
        happy_agent("a"),
        MockAgent("b", [AgentError("x"), AgentError("x")]),
        MockAgent("c", [AgentError("x"), AgentError("x")]),
    ]
    status = Orchestrator(store, agents).run(did)
    assert status == "error"
    state = store.read_state(did)
    assert state["status"] == "error"
    assert any(e["type"] == "error" for e in store.read_events(did))


def test_resume_after_interrupted_phase(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    first = [
        MockAgent("a", ["proposal from a"]),
        MockAgent("b", ["proposal from b"]),
    ]
    status = Orchestrator(store, first).run(did)
    assert status == "error"
    assert store.read_state(did)["last_completed_phase"] == "propose"

    second = [
        MockAgent("a", ["crit a", "rev a", "NOMINATE: a\nok", "VOTE: accept\nok"]),
        MockAgent("b", ["crit b", "rev b", "NOMINATE: a\nok", "VOTE: accept\nok"]),
    ]
    status = Orchestrator(store, second).run(did)
    assert status == "awaiting_human"
    events = store.read_events(did)
    assert sum(1 for e in events if e["type"] == "proposal") == 2
    state = store.read_state(did)
    assert state["round"] == 1
    assert state["proposals"]["a"] == "rev a"


def test_finished_debate_is_not_rerun(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "p")
    state = store.read_state(did)
    state["status"] = "approved"
    store.write_state(did, state)
    agents = [MockAgent("a", []), MockAgent("b", [])]
    assert Orchestrator(store, agents).run(did) == "approved"


def test_resume_after_vote_checkpoint_retains_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    orchestrator = Orchestrator(store, [happy_agent("a"), happy_agent("b")])
    checkpoint = orchestrator._checkpoint

    def interrupt_after_vote_write(debate_id, state):
        checkpoint(debate_id, state)
        if state["last_completed_phase"] == "vote":
            raise RuntimeError("interrupted after vote checkpoint")

    orchestrator._checkpoint = interrupt_after_vote_write
    with pytest.raises(RuntimeError, match="interrupted after vote checkpoint"):
        orchestrator.run(did)

    persisted = store.read_state(did)
    assert persisted["status"] == "awaiting_human"
    assert persisted["last_completed_phase"] == "vote"
    assert persisted["round"] == 1

    empty_agents = [MockAgent("a", []), MockAgent("b", [])]
    assert Orchestrator(store, empty_agents).run(did) == "awaiting_human"
    assert store.read_state(did)["round"] == 1
