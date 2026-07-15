import pytest

from debatelab.agents import models
from debatelab.agents.base import AgentError
from debatelab import prompts, protocol
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


def test_phases_request_matching_model_tiers(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    agents = [happy_agent("a"), happy_agent("b")]
    Orchestrator(store, agents).run(did)
    # propose/critique/revise are deep work; nominate and vote are fast.
    assert agents[0].tasks == [
        models.DEEP, models.DEEP, models.DEEP, models.FAST, models.FAST
    ]


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


def test_unparseable_vote_abstains_instead_of_counting_as_accept(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["proposal a", "crit a", "rev a", "NOMINATE: b",
                        "I cannot accept this"]),
        MockAgent("b", ["proposal b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    types = [e["type"] for e in store.read_events(did)]
    assert "abstained" in types
    state = store.read_state(did)
    assert "a" not in state["votes"]
    assert state["abstained"] == ["a"]


def test_unparseable_vote_is_reasked_once_and_then_counted(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "I cannot accept this", "VOTE: reject"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])
    Orchestrator(store, [a, b]).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["votes"]["a"]["vote"] == "reject"
    assert state["abstained"] == []
    assert "could not be parsed" in a.prompts[-1]
    assert "Candidate final answer" in a.prompts[-1]


def test_vote_unparseable_twice_abstains(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "I cannot accept this", "still refusing to comply"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])
    Orchestrator(store, [a, b]).run(did, max_rounds=1)
    state = store.read_state(did)
    assert "a" not in state["votes"]
    assert state["abstained"] == ["a"]


def test_agent_error_during_reask_abstains(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "garbage", AgentError("a: down")])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])
    Orchestrator(store, [a, b]).run(did, max_rounds=1)
    assert store.read_state(did)["abstained"] == ["a"]


def test_self_nomination_is_dropped_and_recorded(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["proposal a", "crit a", "rev a", "NOMINATE: a",
                        "VOTE: accept"]),
        MockAgent("b", ["proposal b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    dropped = [e for e in store.read_events(did)
               if e["type"] == "nomination_dropped"]
    assert [e["agent"] for e in dropped] == ["a"]


def test_zero_valid_nominations_emits_fallback_candidate(tmp_path):
    """Regression: this used to silently crown agent_order[0]."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["proposal a", "crit a", "rev a", "no idea",
                        "still no idea", "VOTE: accept"]),
        MockAgent("b", ["proposal b", "crit b", "rev b", "dunno",
                        "still dunno", "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    fallbacks = [e for e in store.read_events(did)
                 if e["type"] == "fallback_candidate"]
    assert len(fallbacks) == 1


def test_unparseable_nomination_is_reasked_once_and_then_counted(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "not a nomination",
                        "NOMINATE: b", "VOTE: accept"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: b",
                        "VOTE: accept"])

    Orchestrator(store, [a, b]).run(did, max_rounds=1)

    state = store.read_state(did)
    assert state["candidate"]["agent"] == "b"
    assert not any(e["type"] == "fallback_candidate"
                   for e in store.read_events(did))
    assert state["abstained"] == []
    assert "could not be parsed" in a.prompts[-2]
    assert "NOMINATE: <agent-name>" in a.prompts[-2]


def test_nomination_retry_events_replay_the_recorded_candidate(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "not a nomination",
                        "NOMINATE: b", "VOTE: accept"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: b",
                        "VOTE: accept"])

    Orchestrator(store, [a, b]).run(did, max_rounds=1)

    events = store.read_events(did)
    names = ["a", "b"]
    nominations = {}
    for event in events:
        if event["type"] == "nomination":
            nominee = prompts.parse_nomination(event["content"], names)
        elif event["type"] == "nomination_retry":
            nominee = event["nominee"]
        else:
            continue
        if nominee and nominee != event["agent"]:
            nominations[event["agent"]] = nominee

    candidate, was_fallback = protocol.select_candidate(
        nominations, names, f"{did}:1"
    )
    assert nominations == {"a": "b"}
    assert not was_fallback
    assert candidate == store.read_state(did)["candidate"]["agent"] == "b"


def test_unparseable_nomination_twice_uses_fallback_candidate(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "no nomination",
                        "still no nomination", "VOTE: accept"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "also no nomination",
                        "still also no nomination", "VOTE: accept"])

    status = Orchestrator(store, [a, b]).run(did, max_rounds=1)

    assert status == "awaiting_human"
    assert any(e["type"] == "fallback_candidate"
               for e in store.read_events(did))
    assert store.read_state(did)["abstained"] == []


def test_agent_error_during_nomination_reask_does_not_halt(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "not a nomination",
                        AgentError("a: down"), "VOTE: accept"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])

    status = Orchestrator(store, [a, b]).run(did, max_rounds=1)

    assert status == "awaiting_human"
    assert store.read_state(did)["abstained"] == []
    assert not any(e["type"] == "error" for e in store.read_events(did))


def test_two_accepts_of_a_five_agent_roster_is_not_consensus(tmp_path):
    """Regression, verified to reproduce against the pre-fix code: consensus
    was unanimity among agents that VOTED, and a phase needed only 2
    responders, so 5 configured agents with 3 network failures and 2 accepts
    returned 'awaiting_human'. It must now be no_consensus (2 < ceil(2/3*5)=4).

    A MockAgent with an empty script raises AgentError on every ask, which is
    how c/d/e abstain in every phase.
    """
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: accept"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
        MockAgent("c", []), MockAgent("d", []), MockAgent("e", []),
    ]
    status = Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["roster"] == ["a", "b", "c", "d", "e"]
    assert sorted(state["votes"]) == ["a", "b"]
    assert status == "no_consensus"
    no_consensus = [e for e in store.read_events(did)
                    if e["type"] == "no_consensus"]
    assert no_consensus[0]["content"] == (
        "no consensus reached within the configured round limit"
    )


def test_full_roster_accept_reaches_consensus_with_a_tally(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: accept"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
        MockAgent("c", ["prop c", "crit c", "rev c", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "awaiting_human"
    consensus = [e for e in store.read_events(did) if e["type"] == "consensus"]
    assert consensus[0]["tally"] == {
        "accepts": 3, "rejects": 0, "abstains": 0,
        "roster_size": 3, "required": 2, "quorum": "2/3",
    }


def test_roster_change_on_resume_is_recorded(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    state = store.read_state(did)
    state["roster"] = ["a", "b", "zz"]
    store.write_state(did, state)
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: reject"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: reject"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    changed = [e for e in store.read_events(did) if e["type"] == "roster_changed"]
    assert len(changed) == 1
    assert store.read_state(did)["roster"] == ["a", "b"]


def test_state_predating_roster_and_quorum_still_runs(tmp_path):
    """Compatibility: old debates have no roster/quorum keys. They must
    default rather than raise, and must not emit a spurious roster_changed."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    state = store.read_state(did)
    del state["roster"]
    del state["quorum"]
    store.write_state(did, state)
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: accept"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["quorum"] == "2/3"
    assert state["roster"] == ["a", "b"]
    assert not [e for e in store.read_events(did) if e["type"] == "roster_changed"]
