"""Fold a debate transcript into the state it implies.

Pure in the sense that matters: no files, no network, no clock, no store, no
debate knowledge beyond the event vocabulary. It imports nothing from
debatelab at all -- it is a fold over plain dicts, which is what lets the
whole suite test it without a filesystem.

This is deliberately an INDEPENDENT reimplementation of the state updates in
Orchestrator, not a refactor that shares them. If both sides ran the same
code, `debate fsck` would compare state.json against the logic that wrote it
-- able to catch a torn write, never a fold bug, which is the class of error
this module introduces. The duplication is the feature; fsck is the
differential test between the two. tests/test_replay_differential.py is what
keeps them honest.
"""
import copy
import hashlib
import json


class MissingGenesis(Exception):
    """The transcript has no debate_created event: a pre-genesis debate."""


class UnknownEvent(Exception):
    """An event type with no fold rule and no audit-only exemption."""


# Real events that carry real information and change no state.json key, so
# they must change nothing here either.
AUDIT_ONLY = frozenset({
    "agent_call",
    "nomination",
    "nomination_retry",
    "nomination_dropped",
    "fallback_candidate",
    "roster_changed",
    "synthesis_failed",
    "workspace_ready",
})


def _initial():
    """The shape store.create() writes. id/title/max_rounds/quorum are None
    only until the mandatory genesis event fills them."""
    return {
        "id": None,
        "title": None,
        "status": "created",
        "round": 0,
        "max_rounds": None,
        "quorum": None,
        "roster": None,
        "last_completed_phase": None,
        "proposals": {},
        "critiques": {},
        "candidate": None,
        "votes": {},
        "abstained": [],
        "human_decision": None,
    }


def _debate_created(st, e):
    st["id"] = e["id"]
    st["title"] = e["title"]
    # Off the event, never imported: a default that changed since this debate
    # was created must not rewrite what this debate actually ran with.
    st["max_rounds"] = e["max_rounds"]
    st["quorum"] = e["quorum"]
    if "workspace" in e:
        st["workspace"] = e["workspace"]


def _run_config(st, e):
    st["roster"] = list(e["roster"])
    st["max_rounds"] = e["max_rounds"]
    st["quorum"] = e["quorum"]
    st["status"] = "running"          # mirrors orchestrator.py:50


def _phase_started(st, e):
    st["round"] = e["round"]          # mirrors orchestrator.py:67
    st["abstained"] = []              # mirrors orchestrator.py:68
    if e["phase"] == "nominate":
        st["candidate"] = None        # mirrors orchestrator.py:113


def _phase_completed(st, e):
    st["last_completed_phase"] = e["phase"]   # mirrors orchestrator.py:71


def _proposal(st, e):
    # propose runs exactly once per debate (protocol.next_phase returns to
    # critique after vote, never to propose), so :190's replace and :225's
    # merge coincide: last write wins, and an agent that skips revise keeps
    # its proposal.
    st["proposals"][e["agent"]] = e["content"]


def _critique(st, e):
    st["critiques"][e["agent"]] = e["content"]


def _candidate(st, e):
    st["candidate"] = {
        "agent": e["agent"], "text": e["content"], "synthesized": False,
    }


def _synthesis(st, e):
    # Mirrors orchestrator._phase_synthesize: the merge is both the candidate
    # and, from now on, the winner's proposal -- which is what round N+1
    # critiques.
    st["candidate"] = {
        "agent": e["agent"], "text": e["content"], "synthesized": True,
    }
    st["proposals"][e["agent"]] = e["content"]


def _vote(st, e):
    st["votes"][e["agent"]] = {"vote": e["verdict"], "reason": e["content"]}


def _abstained(st, e):
    st["abstained"] = sorted(set(st["abstained"]) | {e["agent"]})


def _human_decision(st, e):
    st["human_decision"] = {"decision": e["content"], "note": e.get("note", "")}
    st["status"] = e["content"]


def _status_setter(value):
    def fold(st, e):
        st["status"] = value
    return fold


def _state_sha256(st):
    payload = json.dumps(
        st, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _matches_loaded_state(st, event):
    """Compare a checkpoint candidate with a new-format run_config."""
    if "loaded_state_sha256" in event:
        return _state_sha256(st) == event["loaded_state_sha256"]
    if "last_completed_phase" not in event or "loaded_status" not in event:
        return None
    return (
        st["round"] == event["round"]
        and st["last_completed_phase"] == event["last_completed_phase"]
        and st["status"] == event["loaded_status"]
    )


_FOLD = {
    "debate_created": _debate_created,
    "run_config": _run_config,
    "phase_started": _phase_started,
    "phase_completed": _phase_completed,
    "proposal": _proposal,
    "revision": _proposal,
    "critique": _critique,
    "candidate": _candidate,
    "synthesis": _synthesis,
    "vote": _vote,
    "abstained": _abstained,
    "consensus": _status_setter("awaiting_human"),
    "no_consensus": _status_setter("no_consensus"),
    "error": _status_setter("error"),
    "human_decision": _human_decision,
}


def replay(events):
    """Fold a transcript into the state it implies.

    Raises MissingGenesis if the transcript does not open with a
    debate_created event, and UnknownEvent for any type that is neither
    folded nor exempted as audit-only.
    """
    if not events or events[0].get("type") != "debate_created":
        raise MissingGenesis(
            "transcript does not open with a debate_created event: "
            "pre-genesis debate"
        )
    st = _initial()
    _debate_created(st, events[0])
    checkpointed = copy.deepcopy(st)
    attempt = None
    pending_checkpoint = None
    superseded = None
    resume_config = None

    for e in events[1:]:
        kind = e.get("type")
        if kind in AUDIT_ONLY:
            continue
        fold = _FOLD.get(kind)
        if fold is None:
            raise UnknownEvent(f"no fold rule for event type {kind!r}")

        if kind == "run_config":
            # A new run loaded state.json. A new-format event identifies which
            # unresolved boundary state it loaded; legacy events still need
            # the first phase (or no_consensus round) to resolve that choice.
            if pending_checkpoint is not None:
                superseded = (copy.deepcopy(st), pending_checkpoint)
            st = copy.deepcopy(checkpointed)
            if superseded is not None:
                candidate, _candidate_key = superseded
                matches = _matches_loaded_state(candidate, e)
                if matches is not None:
                    if matches:
                        checkpointed = copy.deepcopy(candidate)
                        st = copy.deepcopy(candidate)
                    superseded = None
            fold(st, e)
            resume_config = e
            pending_checkpoint = None
            attempt = None
            continue

        if kind == "phase_started":
            key = (e["round"], e["phase"])
            if superseded is not None:
                candidate, candidate_key = superseded
                if key != candidate_key:
                    # Resuming at a later phase proves the candidate phase was
                    # checkpointed before the previous process stopped.
                    checkpointed = copy.deepcopy(candidate)
                    st = copy.deepcopy(candidate)
                    _run_config(st, resume_config)
                superseded = None
            if pending_checkpoint is not None:
                # Orchestrator cannot start another phase until the previous
                # phase's checkpoint write has returned successfully.
                checkpointed = copy.deepcopy(st)
                pending_checkpoint = None
            fold(st, e)
            attempt = {
                "key": key,
                "critiques": {},
                "votes": {},
            }
            continue

        if kind in ("critique", "vote") and attempt is not None:
            if attempt["key"] == (e["round"], e["phase"]):
                if kind == "critique":
                    attempt["critiques"][e["agent"]] = e["content"]
                else:
                    attempt["votes"][e["agent"]] = {
                        "vote": e["verdict"],
                        "reason": e["content"],
                    }
                continue

        if kind == "phase_completed":
            key = (e["round"], e["phase"])
            if attempt is not None and attempt["key"] == key:
                if e["phase"] == "critique":
                    st["critiques"] = dict(attempt["critiques"])
                elif e["phase"] == "vote":
                    st["votes"] = dict(attempt["votes"])
            fold(st, e)
            pending_checkpoint = key
            attempt = None
            continue

        if kind == "no_consensus":
            if superseded is not None:
                candidate, _candidate_key = superseded
                # Legacy run_config events have no complete identity. Their
                # round is still enough to resolve the lower-cap cases where
                # no phase starts to prove which checkpoint was loaded.
                if candidate["round"] == resume_config["round"]:
                    checkpointed = copy.deepcopy(candidate)
                    st = copy.deepcopy(candidate)
                    _run_config(st, resume_config)
                superseded = None
            if pending_checkpoint is not None:
                # no_consensus is emitted on the loop after the last phase's
                # checkpoint, so reaching it proves that phase durable.
                checkpointed = copy.deepcopy(st)
                pending_checkpoint = None
            fold(st, e)
            continue

        if kind == "error":
            # run() checkpoints a DebateHalted state after appending error.
            # The next run_config identity proves whether that write landed.
            fold(st, e)
            pending_checkpoint = (e["round"], "error")
            attempt = None
            continue

        if kind == "human_decision":
            # The decision command reads and validates the prior checkpoint
            # before it can append this event.
            checkpointed = copy.deepcopy(st)
            pending_checkpoint = None

        fold(st, e)
    return st
