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
        ev("phase_started", round=2, phase="critique"),
        ev("critique", round=2, phase="critique", agent="a", content="c3"),
    ])
    assert state["critiques"] == {"a": "c3"}


def test_a_halted_later_critique_keeps_the_prior_round_critiques():
    """The orchestrator replaces critiques only after its fanout succeeds."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="c1"),
        ev("phase_started", round=2, phase="critique"),
        ev("abstained", round=2, phase="critique", agent="b", content="boom"),
    ])
    assert state["critiques"] == {"a": "c1"}


def test_votes_reset_at_each_vote_phase():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("vote", round=1, phase="vote", agent="a", verdict="accept",
           content="yes"),
        ev("phase_started", round=2, phase="vote"),
        ev("vote", round=2, phase="vote", agent="b", verdict="reject",
           content="no"),
    ])
    assert state["votes"] == {"b": {"vote": "reject", "reason": "no"}}


def test_a_halted_later_vote_keeps_the_prior_round_votes():
    """The orchestrator replaces votes only after its vote fanout succeeds."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("vote", round=1, phase="vote", agent="a", verdict="accept",
           content="yes"),
        ev("phase_started", round=2, phase="vote"),
        ev("abstained", round=2, phase="vote", agent="b", content="boom"),
    ])
    assert state["votes"] == {"a": {"vote": "accept", "reason": "yes"}}


def test_abstained_resets_every_phase_not_every_round():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="propose"),
        ev("abstained", agent="a", content="boom"),
        ev("phase_started", round=1, phase="critique"),
    ])
    assert state["abstained"] == []


def test_abstained_unions_across_the_vote_phases_two_fanouts():
    """_phase_vote fans out twice (nominate, then vote) inside one phase, and
    both append to the same list."""
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
        ev("phase_started", phase="vote"),
        ev("candidate", phase="vote", agent="a", content="the answer"),
        ev("consensus", phase="vote", agent="a", content="the answer"),
    ])
    assert state["candidate"] == {"agent": "a", "text": "the answer"}
    assert state["status"] == "awaiting_human"


def test_no_consensus_sets_its_status():
    state = replay.replay([genesis(), ev("no_consensus", phase="end")])
    assert state["status"] == "no_consensus"


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
    with pytest.raises(UnknownEvent, match="synthesis"):
        replay.replay([genesis(), ev("synthesis", agent="a")])
