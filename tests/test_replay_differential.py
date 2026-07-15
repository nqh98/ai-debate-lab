"""replay must agree with Orchestrator on every debate shape.

Tasks 1-3 can each pass while the two implementations still disagree. This
file is what proves they don't, and it is the reason the deliberate
duplication in replay.py is affordable. It is `debate fsck` run in process.
"""
import pytest

from debatelab import cli
from debatelab.agents.base import AgentError, ErrorKind
from debatelab.orchestrator import Orchestrator
from debatelab.replay import replay

from .conftest import MockAgent, happy_agent, make_store


def assert_agrees(store, did):
    __tracebackhide__ = True
    expected = replay(store.read_events(did))
    actual = store.read_state(did)
    assert actual == expected, "replay disagrees with state.json"


def test_agrees_after_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "awaiting_human"
    assert_agrees(store, did)


def test_agrees_after_no_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        MockAgent("b", [
            "proposal from b", "critique from b", "revised proposal from b",
            "NOMINATE: a\nbest one", "VOTE: reject\nnot yet",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "no_consensus"
    assert_agrees(store, did)


def test_agrees_after_a_halt(tmp_path):
    """The shape that produced the one probe divergence: propose halts, so no
    phase ever completes and last_completed_phase must stay None."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    dead = [MockAgent("b", []), MockAgent("c", [])]
    Orchestrator(store, [happy_agent("a"), *dead]).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["status"] == "error"
    assert state["last_completed_phase"] is None
    assert state["round"] == 1
    assert_agrees(store, did)


def test_agrees_after_approve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    cli.main(["approve", did, "-m", "ship it"])
    assert store.read_state(did)["status"] == "approved"
    assert_agrees(store, did)


def test_agrees_after_reject(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    cli.main(["reject", did, "-m", "not good enough"])
    assert store.read_state(did)["status"] == "rejected"
    assert_agrees(store, did)


def test_agrees_with_retries_and_abstentions(tmp_path):
    """agent_call events from the reliability cycle are audit-only and must
    not perturb the fold; an exhausted agent still abstains."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 429", kind=ErrorKind.RATE_LIMIT),
            "proposal from c", "critique from c", "revised proposal from c",
            "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert any(
        e["type"] == "agent_call" for e in store.read_events(did)
    ), "expected telemetry in the transcript"
    assert_agrees(store, did)


def test_agrees_across_rounds_when_an_agent_drops_out(tmp_path):
    """The reset rule: an agent that critiques in round 1 and abstains in
    round 2 must vanish from critiques. A merging fold would keep it, and
    would diverge only here."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2", "rev a2", "NOMINATE: b", "VOTE: accept\nyes",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: accept\nyes",
            "crit b2", "rev b2", "NOMINATE: a", "VOTE: accept\nyes",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: accept\nyes",
            # nothing left: c abstains from round 2 onward
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)
    assert store.read_state(did)["round"] == 2
    assert_agrees(store, did)


def test_agrees_after_a_later_round_critique_halt(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)
    state = store.read_state(did)
    assert state["status"] == "error"
    assert state["critiques"] == {
        "a": "crit a", "b": "crit b", "c": "crit c",
    }
    assert_agrees(store, did)


def test_agrees_after_a_later_round_vote_halt(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2", "rev a2", "NOMINATE: b",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
            "crit b2", "rev b2",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
            "crit c2", "rev c2",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)
    state = store.read_state(did)
    assert state["status"] == "error"
    assert state["votes"] == {
        "a": {"vote": "reject", "reason": "VOTE: reject\nno"},
        "b": {"vote": "reject", "reason": "VOTE: reject\nno"},
        "c": {"vote": "reject", "reason": "VOTE: reject\nno"},
    }
    assert_agrees(store, did)


def _crash_before_critique_checkpoint(store, did):
    agents = [
        MockAgent("a", ["prop a", "abandoned crit a"]),
        MockAgent("b", ["prop b", "abandoned crit b"]),
        MockAgent("c", ["prop c", "abandoned crit c"]),
    ]
    orch = Orchestrator(store, agents)
    real_checkpoint = orch._checkpoint

    def checkpoint(debate_id, state):
        if state["last_completed_phase"] == "critique":
            raise RuntimeError("crash before critique checkpoint")
        real_checkpoint(debate_id, state)

    orch._checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="before critique checkpoint"):
        orch.run(did, max_rounds=1)


def test_agrees_after_completed_phase_crash_then_resume_into_halt(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    _crash_before_critique_checkpoint(store, did)

    Orchestrator(store, [
        MockAgent("a", ["resumed crit a"]),
        MockAgent("b", []),
        MockAgent("c", []),
    ]).run(did, max_rounds=1)

    state = store.read_state(did)
    assert state["last_completed_phase"] == "propose"
    assert state["critiques"] == {}
    assert state["status"] == "error"
    assert_agrees(store, did)


def test_agrees_after_later_critique_crash_then_lower_round_cap(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "abandoned crit a",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
            "abandoned crit b",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
            "abandoned crit c",
        ]),
    ]
    orch = Orchestrator(store, agents)
    real_checkpoint = orch._checkpoint

    def checkpoint(debate_id, state):
        if state["round"] == 2 and state["last_completed_phase"] == "critique":
            raise RuntimeError("crash before later critique checkpoint")
        real_checkpoint(debate_id, state)

    orch._checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="later critique checkpoint"):
        orch.run(did, max_rounds=2)

    Orchestrator(store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )

    state = store.read_state(did)
    assert state["status"] == "no_consensus"
    assert state["round"] == 1
    assert state["last_completed_phase"] == "vote"
    assert state["critiques"] == {
        "a": "crit a", "b": "crit b", "c": "crit c",
    }
    assert_agrees(store, did)


def test_agrees_after_later_critique_write_then_lower_round_cap(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "durable crit a",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
            "durable crit b",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
            "durable crit c",
        ]),
    ]
    orch = Orchestrator(store, agents)
    real_checkpoint = orch._checkpoint

    def checkpoint(debate_id, state):
        real_checkpoint(debate_id, state)
        if state["round"] == 2 and state["last_completed_phase"] == "critique":
            raise RuntimeError("interrupted after later critique write")

    orch._checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="after later critique write"):
        orch.run(did, max_rounds=2)

    Orchestrator(store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )

    state = store.read_state(did)
    assert state["status"] == "no_consensus"
    assert state["round"] == 2
    assert state["last_completed_phase"] == "critique"
    assert state["critiques"] == {
        "a": "durable crit a",
        "b": "durable crit b",
        "c": "durable crit c",
    }
    assert_agrees(store, did)


def test_agrees_after_later_critique_halt_then_lower_round_cap(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "partial crit a",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)
    halted = store.read_state(did)
    assert halted["status"] == "error"
    assert halted["round"] == 2
    assert halted["abstained"] == ["b", "c"]

    Orchestrator(store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )

    state = store.read_state(did)
    assert state["status"] == "no_consensus"
    assert state["round"] == 2
    assert state["last_completed_phase"] == "vote"
    assert state["abstained"] == ["b", "c"]
    assert_agrees(store, did)


def test_agrees_after_halted_vote_candidate_then_lower_round_cap(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2", "rev a2", "NOMINATE: b", "VOTE: reject\nstill no",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
            "crit b2", "rev b2", "NOMINATE: a",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
            "crit c2", "rev c2", "NOMINATE: b",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)
    halted = store.read_state(did)
    assert halted["status"] == "error"
    assert halted["candidate"] == {
        "agent": "b", "text": "synthesis from b", "synthesized": True,
    }

    Orchestrator(store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )

    state = store.read_state(did)
    assert state["status"] == "no_consensus"
    assert state["round"] == 2
    assert state["last_completed_phase"] == "synthesize"
    assert state["candidate"] == {
        "agent": "b", "text": "synthesis from b", "synthesized": True,
    }
    assert_agrees(store, did)


def _repeat_halted_vote(store, did, *, checkpoint_second):
    Orchestrator(store, [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2", "rev a2", "NOMINATE: b", "VOTE: reject\nfirst",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
            "crit b2", "rev b2", "NOMINATE: a",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: b", "VOTE: reject\nno",
            "crit c2", "rev c2", "NOMINATE: b",
        ]),
    ]).run(did, max_rounds=2)
    first = store.read_state(did)
    assert first["status"] == "error"
    assert first["candidate"] == {
        "agent": "b", "text": "synthesis from b", "synthesized": True,
    }
    assert first["abstained"] == ["b", "c"]

    orch = Orchestrator(store, [
        MockAgent("a", ["NOMINATE: c"]),
        MockAgent("b", ["NOMINATE: c"]),
        MockAgent("c", ["NOMINATE: a", "VOTE: reject\nsecond"]),
    ])
    real_checkpoint = orch._checkpoint

    def checkpoint(debate_id, state):
        if checkpoint_second:
            real_checkpoint(debate_id, state)
        raise RuntimeError("interrupt second halt checkpoint")

    orch._checkpoint = checkpoint
    with pytest.raises(RuntimeError, match="second halt checkpoint"):
        orch.run(did, max_rounds=2)

    Orchestrator(store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )
    return store.read_state(did)


def test_repeated_halt_discards_second_vote_attempt_if_checkpoint_crashes(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")

    state = _repeat_halted_vote(store, did, checkpoint_second=False)

    assert state["status"] == "no_consensus"
    assert state["candidate"] == {
        "agent": "b", "text": "synthesis from b", "synthesized": True,
    }
    assert state["abstained"] == ["b", "c"]
    assert_agrees(store, did)


def test_repeated_halt_promotes_second_vote_attempt_if_checkpoint_is_durable(
        tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")

    state = _repeat_halted_vote(store, did, checkpoint_second=True)

    assert state["status"] == "no_consensus"
    assert state["candidate"] == {
        "agent": "b", "text": "synthesis from b", "synthesized": True,
    }
    assert state["abstained"] == ["a", "b"]
    assert_agrees(store, did)


def test_agrees_when_resumed_nominate_halts_before_selecting_a_candidate(tmp_path):
    """A halt inside the nominate fanout itself -- before a candidate is
    chosen -- must leave the candidate None both before and after resume."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b"]),
        MockAgent("b", ["prop b", "crit b", "rev b"]),
        MockAgent("c", ["prop c", "crit c", "rev c"]),
    ]).run(did, max_rounds=1)
    halted = store.read_state(did)
    assert halted["status"] == "error"
    assert halted["candidate"] is None
    assert_agrees(store, did)

    Orchestrator(store, [
        MockAgent("a", ["NOMINATE: b"]),
        MockAgent("b", []),
        MockAgent("c", []),
    ]).run(did, max_rounds=1)

    state = store.read_state(did)
    assert state["status"] == "error"
    assert state["candidate"] is None
    assert_agrees(store, did)


def test_agrees_when_same_phase_retry_has_changed_responders(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    _crash_before_critique_checkpoint(store, did)

    agents = [
        MockAgent("a", [
            "replacement crit a", "rev a", "NOMINATE: b",
            "VOTE: accept\nyes",
        ]),
        MockAgent("b", [
            "replacement crit b", "rev b", "NOMINATE: a",
            "VOTE: accept\nyes",
        ]),
        MockAgent("c", []),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)

    state = store.read_state(did)
    assert state["critiques"] == {
        "a": "replacement crit a",
        "b": "replacement crit b",
    }
    assert_agrees(store, did)


def test_agrees_when_a_completed_vote_has_no_parseable_votes(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2", "rev a2", "NOMINATE: b", "invalid", "still invalid",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: reject\nno",
            "crit b2", "rev b2", "NOMINATE: a", "invalid", "still invalid",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: reject\nno",
            "crit c2", "rev c2", "NOMINATE: a", "invalid", "still invalid",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)

    state = store.read_state(did)
    assert state["votes"] == {}
    assert state["last_completed_phase"] == "vote"
    assert state["status"] == "no_consensus"
    assert_agrees(store, did)


def test_agrees_when_a_self_nomination_is_dropped(tmp_path):
    """nomination_dropped is audit-only; the fold must not react to it."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a", nominee="a"), happy_agent("b", nominee="a")]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert any(
        e["type"] == "nomination_dropped" for e in store.read_events(did)
    ), "expected a self-nomination to be dropped"
    assert_agrees(store, did)


def test_agrees_after_a_synthesised_debate(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    assert store.read_state(did)["candidate"]["synthesized"] is True
    assert_agrees(store, did)


def test_agrees_after_a_synthesis_fallback(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [
        happy_agent("a", synthesis=AgentError("boom")),
        happy_agent("b"),
        happy_agent("c"),
    ]).run(did, max_rounds=1)
    assert store.read_state(did)["candidate"]["synthesized"] is False
    assert_agrees(store, did)


def test_agrees_across_a_carried_forward_synthesis(tmp_path):
    """Two rounds: round 1's synthesis becomes a's proposal, which round 2
    critiques and revises. The carry-forward is the state update most likely
    to drift between the two implementations."""
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
    assert_agrees(store, did)
