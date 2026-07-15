"""replay must agree with Orchestrator on every debate shape.

Tasks 1-3 can each pass while the two implementations still disagree. This
file is what proves they don't, and it is the reason the deliberate
duplication in replay.py is affordable. It is `debate fsck` run in process.
"""
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
