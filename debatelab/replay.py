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


def _run_config(st, e):
    st["roster"] = list(e["roster"])
    st["max_rounds"] = e["max_rounds"]
    st["quorum"] = e["quorum"]
    st["status"] = "running"          # mirrors orchestrator.py:50


def _phase_started(st, e):
    st["round"] = e["round"]          # mirrors orchestrator.py:67
    st["abstained"] = []              # mirrors orchestrator.py:68


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
    st["candidate"] = {"agent": e["agent"], "text": e["content"]}


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


_FOLD = {
    "debate_created": _debate_created,
    "run_config": _run_config,
    "phase_started": _phase_started,
    "phase_completed": _phase_completed,
    "proposal": _proposal,
    "revision": _proposal,
    "critique": _critique,
    "candidate": _candidate,
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
    # Critique and vote replace their dictionaries only after their fanouts
    # complete. Reset lazily on the first emitted result, so a later halted
    # phase preserves the prior round's dictionary just like Orchestrator.
    reset_results = set()
    for e in events:
        kind = e.get("type")
        if kind in AUDIT_ONLY:
            continue
        fold = _FOLD.get(kind)
        if fold is None:
            raise UnknownEvent(f"no fold rule for event type {kind!r}")
        if kind in ("critique", "vote"):
            result_key = (kind, e["round"], e["phase"])
            if result_key not in reset_results:
                st[f"{kind}s"] = {}
                reset_results.add(result_key)
        fold(st, e)
    return st
