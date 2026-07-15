from fractions import Fraction

import pytest

from debatelab.agents import models
from debatelab.agents.base import Agent, AgentError, ErrorKind
from debatelab import orchestrator, prompts, protocol
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent, happy_agent, make_store


class DeadAgent(Agent):
    def __init__(self, name):
        super().__init__(name)
        self.calls = 0

    def ask(self, prompt, task=models.DEEP):
        self.calls += 1
        raise AgentError(
            f"{self.name}: command not found: agy", kind=ErrorKind.NOT_FOUND
        )


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
    assert state["candidate"]["text"] == "synthesis from a"
    assert state["candidate"]["synthesized"] is True
    assert all(v["vote"] == "accept" for v in state["votes"].values())
    types = [e["type"] for e in store.read_events(did)]
    for expected in ("proposal", "critique", "revision", "nomination",
                     "candidate", "synthesis", "vote", "consensus"):
        assert expected in types
    assert "pending human decision" in store.read_summary(did)


def test_phases_request_matching_model_tiers(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    agents = [happy_agent("a"), happy_agent("b")]
    Orchestrator(store, agents).run(did)
    # propose/critique/revise are deep work; nominate and vote are fast.
    # Only the nomination winner is asked to synthesize (deep); this test
    # uses "a" as the nominee, so agents[0] picks up the extra deep call.
    assert agents[0].tasks == [
        models.DEEP, models.DEEP, models.DEEP, models.FAST, models.DEEP,
        models.FAST,
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
    assert state["proposals"]["a"] == "synthesis from a"


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
                        AgentError("a: command not found: agy",
                                   kind=ErrorKind.NOT_FOUND),
                        "VOTE: accept"])
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
    assert no_consensus[0]["tally"] == protocol.tally(
        state["votes"], len(state["roster"]), Fraction(state["quorum"])
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


def test_a_transient_failure_is_retried_and_costs_no_vote(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 429: slow down", kind=ErrorKind.RATE_LIMIT),
            "proposal from c",
            "critique from c",
            "revised proposal from c",
            "NOMINATE: a\nbest one",
            "VOTE: accept\nagreed",
        ]),
    ]
    status = Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["abstained"] == []
    assert sorted(state["votes"]) == ["a", "b", "c"]
    assert status == "awaiting_human"


def test_a_permanent_failure_is_never_retried(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    dead = DeadAgent("c")
    slept = []
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), dead], sleep=slept.append
    ).run(did, max_rounds=1)
    assert dead.calls == 5
    assert slept == []


def test_a_permanent_failure_still_abstains(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b"), DeadAgent("c")]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert "c" in state["abstained"]
    assert "c" not in state["votes"]


def test_exhausted_retries_still_abstain(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b"), MockAgent("c", [])]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert "c" in state["abstained"]
    assert state["roster"] == ["a", "b", "c"]


def test_reask_retries_a_transient_failure_instead_of_abstaining(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b",
            "I cannot accept this",
            AgentError("a: HTTP 503", kind=ErrorKind.SERVER_ERROR),
            "VOTE: reject\nnow parseable",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: accept",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["votes"]["a"]["vote"] == "reject"
    assert state["abstained"] == []


def test_backoff_delays_are_drawn_from_the_injected_rng(tmp_path):
    import random

    store = make_store(tmp_path)
    did = store.create("T", "problem")
    slept = []
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 503", kind=ErrorKind.SERVER_ERROR),
            "proposal from c", "critique from c", "revised proposal from c",
            "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        ]),
    ]
    Orchestrator(
        store, agents, sleep=slept.append, rng=random.Random(0)
    ).run(did, max_rounds=1)
    assert len(slept) == 1
    assert slept == [random.Random(0).uniform(0, 1.0)]


def test_every_attempt_is_recorded_with_its_outcome(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 429: slow down", kind=ErrorKind.RATE_LIMIT),
            "proposal from c", "critique from c", "revised proposal from c",
            "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["agent"] == "c"
        and e["phase"] == "propose"
    ]
    assert [e["attempt"] for e in calls] == [1, 2]
    assert [e["ok"] for e in calls] == [False, True]
    assert calls[0]["kind"] == "rate_limit"
    assert "HTTP 429" in calls[0]["content"]
    assert "kind" not in calls[1]


def test_agent_call_events_carry_phase_task_and_duration(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    calls = [e for e in store.read_events(did) if e["type"] == "agent_call"]
    assert calls, "expected an agent_call per attempt"
    assert all(isinstance(e["duration_ms"], int) for e in calls)
    assert all(e["duration_ms"] >= 0 for e in calls)
    assert {e["phase"] for e in calls} == {
        "propose", "critique", "revise", "nominate", "synthesize", "vote"
    }
    assert {e["task"] for e in calls} == {models.DEEP, models.FAST}


def test_agent_call_events_never_claim_a_token_count(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    calls = [e for e in store.read_events(did) if e["type"] == "agent_call"]
    assert all("tokens" not in e for e in calls)


def test_every_transcript_event_carries_the_base_schema(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    required = {"ts", "round", "phase", "agent", "type", "content"}
    assert all(required <= set(e) for e in store.read_events(did))


def test_a_permanent_failure_records_exactly_one_attempt(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b"), DeadAgent("c")]
    Orchestrator(store, agents).run(did, max_rounds=1)
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["agent"] == "c"
        and e["phase"] == "propose"
    ]
    assert [e["attempt"] for e in calls] == [1]
    assert calls[0]["kind"] == "not_found"


def _types(store, did, phase=None):
    return [
        e["type"] for e in store.read_events(did)
        if phase is None or e["phase"] == phase
    ]


def test_run_emits_run_config_on_every_run(tmp_path):
    """Regression: roster_changed fires only when the roster DIFFERS from a
    previous run, so a first run recorded the roster nowhere — and the roster
    is the denominator check_consensus divides by."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    configs = [e for e in store.read_events(did) if e["type"] == "run_config"]
    assert len(configs) == 1
    assert configs[0]["roster"] == ["a", "b"]
    assert configs[0]["max_rounds"] == 1
    assert configs[0]["quorum"] == "2/3"
    assert configs[0]["last_completed_phase"] is None
    assert configs[0]["loaded_status"] == "created"
    assert len(configs[0]["loaded_state_sha256"]) == 64


def test_run_config_records_the_effective_overrides(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=3, quorum=Fraction(1, 2)
    )
    config = next(
        e for e in store.read_events(did) if e["type"] == "run_config"
    )
    assert config["max_rounds"] == 3
    assert config["quorum"] == "1/2"


def test_each_phase_is_bracketed_by_started_and_completed(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    for phase in (
        "propose", "critique", "revise", "nominate", "synthesize", "vote"
    ):
        types = _types(store, did, phase)
        assert types[0] == "phase_started", phase
        assert "phase_completed" in types, phase


def test_a_halted_phase_records_started_without_completed(tmp_path):
    """The divergence this whole cycle exists for: a phase that raised
    DebateHalted must be distinguishable from one that finished."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), DeadAgent("b"), DeadAgent("c")]
    ).run(did, max_rounds=1)
    types = _types(store, did, "propose")
    assert "phase_started" in types
    assert "phase_completed" not in types
    assert store.read_state(did)["status"] == "error"
    assert store.read_state(did)["last_completed_phase"] is None
    error = next(e for e in store.read_events(did) if e["type"] == "error")
    assert error["phase"] == "end"
    assert error["failed_phase"] == "propose"


def test_nominations_are_recorded_under_the_nominate_phase(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    events = store.read_events(did)
    for kind in ("nomination", "candidate"):
        phases = {e["phase"] for e in events if e["type"] == kind}
        assert phases == {"nominate"}, f"{kind} should be a nominate event"
    votes = {e["phase"] for e in events if e["type"] == "vote"}
    assert votes == {"vote"}


def test_nominate_and_vote_are_separately_bracketed(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    started = [
        e["phase"] for e in store.read_events(did) if e["type"] == "phase_started"
    ]
    assert started == [
        "propose", "critique", "revise", "nominate", "synthesize", "vote"
    ]


def _run_one_round(tmp_path, agents):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, agents).run(did, max_rounds=1)
    return store, did


def test_the_candidate_is_the_synthesis_not_a_proposal(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a"), happy_agent("b"), happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["candidate"] == {
        "agent": "a", "text": "synthesis from a", "synthesized": True,
    }
    synth = [e for e in store.read_events(did) if e["type"] == "synthesis"]
    assert len(synth) == 1
    assert synth[0]["phase"] == "synthesize"
    assert synth[0]["agent"] == "a"
    assert synth[0]["content"] == "synthesis from a"


def test_the_synthesis_carries_forward_as_the_winners_proposal(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a"), happy_agent("b"), happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["proposals"]["a"] == "synthesis from a"
    assert state["proposals"]["b"] == "revised proposal from b"


def test_only_the_winner_is_asked_to_synthesize(tmp_path):
    agents = [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    _run_one_round(tmp_path, agents)
    asked = {
        ag.name: sum(1 for p in ag.prompts if prompts.SYNTHESIS_HEADER in p)
        for ag in agents
    }
    assert asked == {"a": 1, "b": 0, "c": 0}


def test_the_vote_is_cast_on_the_synthesis(tmp_path):
    agents = [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    _run_one_round(tmp_path, agents)
    vote_prompts = [
        p for ag in agents for p in ag.prompts if "VOTE: accept" in p
    ]
    assert vote_prompts
    for p in vote_prompts:
        assert "synthesis from a" in p
        assert "revised proposal from a" not in p


def test_synthesis_error_falls_back_to_the_verbatim_proposal(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a", synthesis=AgentError("boom")),
        happy_agent("b"),
        happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["candidate"] == {
        "agent": "a", "text": "revised proposal from a", "synthesized": False,
    }
    assert state["proposals"]["a"] == "revised proposal from a"
    failed = [e for e in store.read_events(did) if e["type"] == "synthesis_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "agent_error"
    assert failed[0]["agent"] == "a"
    assert state["status"] == "awaiting_human", "the vote must still happen"


def test_synthesis_empty_reply_falls_back(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a", synthesis="   \n  "),
        happy_agent("b"),
        happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["candidate"]["text"] == "revised proposal from a"
    assert state["candidate"]["synthesized"] is False
    failed = [e for e in store.read_events(did) if e["type"] == "synthesis_failed"]
    assert [e["reason"] for e in failed] == ["empty"]


def test_synthesis_failure_never_halts_the_debate(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a", synthesis=AgentError("boom")),
        happy_agent("b"),
        happy_agent("c"),
    ])
    events = store.read_events(did)
    assert not [e for e in events if e["type"] == "error"]
    assert store.read_state(did)["status"] != "error"


def test_a_pre_synthesis_checkpoint_resumes_into_nominate(tmp_path):
    """spec §9: a state.json written before this cycle stops at revise. The
    new protocol runs the superset nominate -> synthesize -> vote rather than
    jumping straight to vote. No migration, no special case."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    state = store.read_state(did)
    state.update({
        "status": "running",
        "round": 1,
        "last_completed_phase": "revise",
        "roster": ["a", "b", "c"],
        "proposals": {"a": "a1", "b": "b1", "c": "c1"},
        "critiques": {"a": "ca", "b": "cb", "c": "cc"},
        "candidate": None,
    })
    store.write_state(did, state)
    agents = [
        MockAgent(n, ["NOMINATE: a\nbest one", "VOTE: accept\nagreed"])
        for n in ("a", "b", "c")
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    started = [
        e["phase"] for e in store.read_events(did) if e["type"] == "phase_started"
    ]
    assert started == ["nominate", "synthesize", "vote"]
    assert store.read_state(did)["candidate"]["synthesized"] is True


def test_synthesize_prompt_gets_reject_reasons_from_the_last_round(tmp_path):
    """Round 2's synthesis must see why round 1's answer was rejected."""
    def rejecting(name):
        return MockAgent(name, [
            f"proposal from {name}", f"critique from {name}",
            f"revised proposal from {name}", "NOMINATE: a\nbest one",
            "VOTE: reject\ntoo vague",
            f"critique from {name}", f"revised proposal from {name}",
            "NOMINATE: a\nbest one", "VOTE: accept\nfine now",
        ])
    a = MockAgent("a", [
        "proposal from a", "critique from a", "revised proposal from a",
        "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        "critique from a", "revised proposal from a",
        "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
    ])
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [a, rejecting("b"), rejecting("c")]).run(did, max_rounds=2)
    synth_prompts = [p for p in a.prompts if prompts.SYNTHESIS_HEADER in p]
    assert len(synth_prompts) == 2
    assert "too vague" in synth_prompts[1]
    assert "too vague" not in synth_prompts[0]


def test_agent_call_events_carry_the_resolved_model(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b")]
    for agent in agents:
        agent.model = "test-model-1"
    Orchestrator(store, agents).run(did, max_rounds=1)
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["ok"]
    ]
    assert calls
    assert all(e["model"] == "test-model-1" for e in calls)


def test_agent_call_records_a_null_model_rather_than_dropping_the_key(tmp_path):
    """null is the claim 'the backend routed itself'. An absent key would make
    that indistinguishable from 'this adapter never reported', which is the
    ambiguity the field is built to avoid."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["ok"]
    ]
    assert calls
    assert all("model" in e for e in calls)
    assert all(e["model"] is None for e in calls)


def test_failed_agent_call_omits_the_model_key(tmp_path):
    """AgentError does not know the model, and guessing one would be worse
    than the absence. This is the only case where absence is correct."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), MockAgent("c", [])]
    ).run(did, max_rounds=1)
    failed = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and not e["ok"]
    ]
    assert failed
    assert all("model" not in e for e in failed)


def test_grounded_debate_prefaces_prompts_per_agent(tmp_path):
    store = make_store(tmp_path)
    ws = {"source": "/repo", "commit": "c0ffee"}
    did = store.create("t", "the problem", workspace=ws)
    a, b = happy_agent("a", nominee="b"), happy_agent("b", nominee="a")
    a.workspace_attached = True
    b.workspace_attached = False   # e.g. an api-backed voter
    orchestrator.Orchestrator(store, [a, b]).run(did)
    assert all("c0ffee" in p for p in a.prompts)
    assert all("you cannot" in p for p in b.prompts)


def test_plain_debate_prompts_are_unprefaced(tmp_path):
    store = make_store(tmp_path)
    did = store.create("t", "the problem")
    a, b = happy_agent("a", nominee="b"), happy_agent("b", nominee="a")
    orchestrator.Orchestrator(store, [a, b]).run(did)
    for agent in (a, b):
        assert all("you cannot" not in p for p in agent.prompts)
        assert all("working directory" not in p for p in agent.prompts)
