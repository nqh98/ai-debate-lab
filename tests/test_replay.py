import hashlib
import json

import pytest

from debatelab import replay
from debatelab.replay import MissingGenesis, UnknownEvent


def genesis(**over):
    e = {"round": 0, "phase": "create", "agent": None,
         "type": "debate_created", "content": "T", "id": "d1", "title": "T",
         "max_rounds": 5, "quorum": "2/3"}
    return {**e, **over}


def ev(type_, **kw):
    return {"round": 1, "phase": "propose", "agent": None, "type": type_,
            "content": "", **kw}


def state_sha256(state):
    payload = json.dumps(
        state, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def test_a_transcript_without_genesis_cannot_be_replayed():
    """Pre-genesis debates are refused, not guessed at."""
    with pytest.raises(MissingGenesis):
        replay.replay([ev("proposal", agent="a", content="x")])


def test_an_empty_transcript_cannot_be_replayed():
    with pytest.raises(MissingGenesis):
        replay.replay([])


def test_genesis_alone_reproduces_the_state_create_writes():
    state = replay.replay([genesis()])
    assert state["id"] == "d1"
    assert state["title"] == "T"
    assert state["max_rounds"] == 5
    assert state["quorum"] == "2/3"
    assert state["status"] == "created"
    assert state["round"] == 0
    assert state["roster"] is None
    assert state["last_completed_phase"] is None


def test_defaults_come_off_the_event_not_from_an_import():
    """A debate created when the default was 5 must still replay as 5 after
    someone changes the default to 10."""
    state = replay.replay([genesis(max_rounds=99, quorum="1/2")])
    assert state["max_rounds"] == 99
    assert state["quorum"] == "1/2"


def test_run_config_sets_the_roster_and_marks_the_debate_running():
    state = replay.replay([
        genesis(),
        ev("run_config", phase="run", roster=["a", "b"], max_rounds=5,
           quorum="2/3"),
    ])
    assert state["roster"] == ["a", "b"]
    assert state["status"] == "running"


def test_run_config_overrides_the_creation_defaults():
    state = replay.replay([
        genesis(max_rounds=5, quorum="2/3"),
        ev("run_config", phase="run", roster=["a"], max_rounds=3,
           quorum="1/2"),
    ])
    assert state["max_rounds"] == 3
    assert state["quorum"] == "1/2"


def test_phase_started_advances_the_round():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=2, phase="critique"),
    ])
    assert state["round"] == 2


def test_phase_completed_sets_last_completed_phase():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("phase_completed", phase="propose"),
    ])
    assert state["last_completed_phase"] == "propose"


def test_a_started_but_uncompleted_phase_leaves_last_completed_alone():
    """The divergence that motivated the marker events: a halted propose
    phase must not read as a completed one."""
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("abstained", agent="a", content="boom"),
        ev("error", phase="end", content="only 1 agent responded"),
    ])
    assert state["last_completed_phase"] is None
    assert state["round"] == 1
    assert state["status"] == "error"


def test_proposal_and_revision_both_write_proposals_last_wins():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="first"),
        ev("phase_started", phase="revise"),
        ev("revision", agent="a", content="revised"),
    ])
    assert state["proposals"] == {"a": "revised"}


def test_an_agent_that_skips_revise_keeps_its_proposal():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="a1"),
        ev("proposal", agent="b", content="b1"),
        ev("phase_started", phase="revise"),
        ev("revision", agent="a", content="a2"),
    ])
    assert state["proposals"] == {"a": "a2", "b": "b1"}


def test_critiques_reset_at_each_critique_phase():
    """orchestrator.py:212 assigns state["critiques"] = results — replace, not
    merge. An agent that critiqued in round 1 and abstained in round 2 must
    vanish; merging would keep it."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="c1"),
        ev("critique", round=1, phase="critique", agent="b", content="c2"),
        ev("phase_completed", round=1, phase="critique"),
        ev("phase_started", round=2, phase="critique"),
        ev("critique", round=2, phase="critique", agent="a", content="c3"),
        ev("phase_completed", round=2, phase="critique"),
    ])
    assert state["critiques"] == {"a": "c3"}


def test_a_halted_later_critique_keeps_the_prior_round_critiques():
    """The orchestrator replaces critiques only after its fanout succeeds."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="c1"),
        ev("phase_completed", round=1, phase="critique"),
        ev("phase_started", round=2, phase="critique"),
        ev("abstained", round=2, phase="critique", agent="b", content="boom"),
    ])
    assert state["critiques"] == {"a": "c1"}


def test_a_completed_critique_replaces_prior_results_even_when_empty():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="c1"),
        ev("phase_completed", round=1, phase="critique"),
        ev("phase_started", round=2, phase="critique"),
        ev("phase_completed", round=2, phase="critique"),
    ])
    assert state["critiques"] == {}


def test_votes_reset_at_each_vote_phase():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("vote", round=1, phase="vote", agent="a", verdict="accept",
           content="yes"),
        ev("phase_completed", round=1, phase="vote"),
        ev("phase_started", round=2, phase="vote"),
        ev("vote", round=2, phase="vote", agent="b", verdict="reject",
           content="no"),
        ev("phase_completed", round=2, phase="vote"),
    ])
    assert state["votes"] == {"b": {"vote": "reject", "reason": "no"}}


def test_a_halted_later_vote_keeps_the_prior_round_votes():
    """The orchestrator replaces votes only after its vote fanout succeeds."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("vote", round=1, phase="vote", agent="a", verdict="accept",
           content="yes"),
        ev("phase_completed", round=1, phase="vote"),
        ev("phase_started", round=2, phase="vote"),
        ev("abstained", round=2, phase="vote", agent="b", content="boom"),
    ])
    assert state["votes"] == {"a": {"vote": "accept", "reason": "yes"}}


def test_a_completed_vote_replaces_prior_votes_even_when_empty():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("vote", round=1, phase="vote", agent="a", verdict="accept",
           content="yes"),
        ev("phase_completed", round=1, phase="vote"),
        ev("phase_started", round=2, phase="vote"),
        ev("candidate", round=2, phase="vote", agent="b", content="answer"),
        ev("abstained", round=2, phase="vote", agent="a",
           content="not a vote"),
        ev("phase_completed", round=2, phase="vote"),
    ])
    assert state["votes"] == {}
    assert state["candidate"] == {
        "agent": "b", "text": "answer", "synthesized": False,
    }


def test_a_resumed_same_phase_replaces_the_abandoned_attempt_results():
    state = replay.replay([
        genesis(),
        ev("run_config", phase="run", roster=["a", "b"], max_rounds=2,
           quorum="2/3"),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="old"),
        ev("phase_completed", round=1, phase="critique"),
        ev("run_config", phase="run", roster=["a", "b"], max_rounds=2,
           quorum="2/3"),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="b", content="new"),
        ev("phase_completed", round=1, phase="critique"),
    ])
    assert state["critiques"] == {"b": "new"}


def test_no_consensus_after_a_lower_cap_discards_an_unproven_checkpoint():
    state = replay.replay([
        genesis(),
        ev("run_config", phase="run", roster=["a", "b"], max_rounds=2,
           quorum="2/3"),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="kept"),
        ev("phase_completed", round=1, phase="critique"),
        ev("phase_started", round=1, phase="revise"),
        ev("phase_completed", round=1, phase="revise"),
        ev("phase_started", round=1, phase="vote"),
        ev("phase_completed", round=1, phase="vote"),
        ev("phase_started", round=2, phase="critique"),
        ev("critique", round=2, phase="critique", agent="a",
           content="abandoned"),
        ev("phase_completed", round=2, phase="critique"),
        ev("run_config", round=1, phase="run", roster=["a", "b"],
           max_rounds=1, quorum="2/3", last_completed_phase="vote",
           loaded_status="running"),
        ev("no_consensus", round=1, phase="end"),
    ])
    assert state["round"] == 1
    assert state["last_completed_phase"] == "vote"
    assert state["critiques"] == {"a": "kept"}
    assert state["status"] == "no_consensus"


def test_no_consensus_after_a_lower_cap_preserves_a_loaded_checkpoint():
    state = replay.replay([
        genesis(),
        ev("run_config", phase="run", roster=["a", "b"], max_rounds=2,
           quorum="2/3"),
        ev("phase_started", round=1, phase="vote"),
        ev("phase_completed", round=1, phase="vote"),
        ev("phase_started", round=2, phase="critique"),
        ev("critique", round=2, phase="critique", agent="a",
           content="durable"),
        ev("phase_completed", round=2, phase="critique"),
        ev("run_config", round=2, phase="run", roster=["a", "b"],
           max_rounds=1, quorum="2/3", last_completed_phase="critique",
           loaded_status="running"),
        ev("no_consensus", round=2, phase="end"),
    ])
    assert state["round"] == 2
    assert state["last_completed_phase"] == "critique"
    assert state["critiques"] == {"a": "durable"}
    assert state["status"] == "no_consensus"


def test_loaded_state_identity_distinguishes_same_boundary_candidates():
    first = replay._initial()
    first.update({
        "id": "d1",
        "title": "T",
        "status": "error",
        "round": 2,
        "max_rounds": 2,
        "quorum": "2/3",
        "roster": ["a", "b", "c"],
        "last_completed_phase": "revise",
        "candidate": {"agent": "b", "text": "first"},
        "abstained": ["b", "c"],
    })
    second = {
        **first,
        "candidate": {"agent": "c", "text": "second"},
        "abstained": ["a", "b"],
    }
    event = ev(
        "run_config",
        round=2,
        phase="run",
        last_completed_phase="revise",
        loaded_status="error",
        loaded_state_sha256=state_sha256(first),
    )

    assert replay._matches_loaded_state(first, event) is True
    assert replay._matches_loaded_state(second, event) is False


def test_abstained_resets_every_phase_not_every_round():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="propose"),
        ev("abstained", agent="a", content="boom"),
        ev("phase_started", round=1, phase="critique"),
    ])
    assert state["abstained"] == []


def test_abstained_unions_across_multiple_abstentions_in_one_phase():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("abstained", phase="vote", agent="b", content="x"),
        ev("abstained", phase="vote", agent="a", content="y"),
    ])
    assert state["abstained"] == ["a", "b"]


def test_candidate_and_terminal_statuses():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="nominate"),
        ev("candidate", phase="nominate", agent="a", content="the answer"),
        ev("consensus", phase="vote", agent="a", content="the answer"),
    ])
    assert state["candidate"] == {
        "agent": "a", "text": "the answer", "synthesized": False,
    }
    assert state["status"] == "awaiting_human"


def test_a_new_nominate_attempt_clears_the_prior_candidate():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a", content="old"),
        ev("phase_started", round=1, phase="nominate"),
    ])
    assert state["candidate"] is None


def test_no_consensus_sets_its_status():
    state = replay.replay([genesis(), ev("no_consensus", phase="end")])
    assert state["status"] == "no_consensus"


def test_terminal_event_enrichments_do_not_change_replay_state():
    plain = [
        genesis(),
        ev("no_consensus", phase="end"),
        ev("error", phase="end", content="only one response"),
    ]
    enriched = [
        genesis(),
        ev(
            "no_consensus",
            phase="end",
            tally={
                "accepts": 1,
                "rejects": 1,
                "abstains": 0,
                "roster_size": 2,
                "required": 2,
                "quorum": "2/3",
            },
        ),
        ev(
            "error",
            phase="end",
            content="only one response",
            failed_phase="propose",
        ),
    ]

    assert replay.replay(enriched) == replay.replay(plain)


def test_human_decision_sets_the_decision_and_the_status():
    state = replay.replay([
        genesis(),
        ev("human_decision", phase="human", agent="human",
           content="approved", note="ship it"),
    ])
    assert state["human_decision"] == {"decision": "approved",
                                       "note": "ship it"}
    assert state["status"] == "approved"


def test_audit_only_events_never_change_state():
    """agent_call is the reliability cycle's telemetry; nomination_dropped
    records a self-nomination. Real events, but none of them touches
    state.json, so none may touch replay's output."""
    base = replay.replay([genesis()])
    noisy = replay.replay([genesis()] + [
        ev(t, agent="a", content="noise") for t in replay.AUDIT_ONLY
    ])
    assert noisy == base


def test_duplicate_events_last_write_wins():
    """A crash mid-phase re-runs that phase on resume and re-appends its
    events — the wart the README already documents."""
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="first try"),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="second try"),
    ])
    assert state["proposals"] == {"a": "second try"}


def test_an_unmodelled_event_type_raises():
    """The forcing function: the next person to add an event type must say
    whether it folds, rather than have replay drift silently out of sync."""
    with pytest.raises(UnknownEvent, match="not_a_real_event"):
        replay.replay([genesis(), ev("not_a_real_event", agent="a")])


def test_synthesis_folds_into_the_candidate_and_the_proposals():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a",
           content="a's proposal"),
        ev("phase_completed", round=1, phase="nominate"),
        ev("phase_started", round=1, phase="synthesize"),
        ev("synthesis", round=1, phase="synthesize", agent="a",
           content="merged"),
        ev("phase_completed", round=1, phase="synthesize"),
    ])
    assert state["candidate"] == {
        "agent": "a", "text": "merged", "synthesized": True,
    }
    assert state["proposals"]["a"] == "merged"


def test_synthesis_failed_changes_no_state():
    base = [
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a",
           content="a's proposal"),
        ev("phase_completed", round=1, phase="nominate"),
    ]
    before = replay.replay(base)
    after = replay.replay(base + [
        ev("phase_started", round=1, phase="synthesize"),
        ev("synthesis_failed", round=1, phase="synthesize", agent="a",
           content="boom", reason="agent_error"),
    ])
    assert after["candidate"] == before["candidate"]
    assert after["candidate"]["synthesized"] is False
    assert after["proposals"] == before["proposals"]


def test_the_candidate_event_is_never_marked_synthesized():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a",
           content="a's proposal"),
    ])
    assert state["candidate"]["synthesized"] is False


def test_the_candidate_resets_on_nominate_not_vote():
    """The reset moved with the phase split; a vote phase must not clear the
    candidate it is voting on."""
    base = [
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a", content="x"),
        ev("phase_completed", round=1, phase="nominate"),
    ]
    assert replay.replay(
        base + [ev("phase_started", round=1, phase="vote")]
    )["candidate"] is not None
    assert replay.replay(
        base + [ev("phase_started", round=2, phase="nominate")]
    )["candidate"] is None
