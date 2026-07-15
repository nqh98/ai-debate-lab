import ast
from pathlib import Path

import pytest

from debatelab.result import build_result, render_final


def event(type_, **overrides):
    return {
        "round": 1,
        "phase": "vote",
        "agent": None,
        "type": type_,
        "content": "",
        **overrides,
    }


def created(**overrides):
    return event("debate_created", round=0, phase="create", id="debate-1",
                 title="A question", **overrides)


def consensus(text="Approved answer", **overrides):
    return event("consensus", **{
        "round": 3,
        "agent": "claude",
        "content": text,
        "tally": {"accepts": 3, "rejects": 0, "abstains": 0,
                  "roster_size": 3, "required": 2},
        **overrides,
    })


def decision(status, **overrides):
    return event("human_decision", **{
        "phase": "human",
        "agent": "human",
        "content": status,
        "ts": "2026-07-15T10:12:03+00:00",
        "note": "reviewed",
        **overrides,
    })


def test_approved_result_promotes_latest_consensus_text_to_answer():
    result = build_result([created(), consensus(), decision("approved")])

    assert result == {
        "id": "debate-1",
        "title": "A question",
        "status": "approved",
        "answer": "Approved answer",
        "candidate": {"agent": "claude", "round": 3},
        "tally": {"accepts": 3, "rejects": 0, "abstains": 0, "roster_size": 3,
                  "required": 2},
        "decided_at": "2026-07-15T10:12:03+00:00",
        "note": "reviewed",
        "reason": None,
        "round": 3,
        "failed_phase": None,
    }
    assert render_final(result) == (
        "# Answer\n\n"
        "Approved answer\n\n"
        "---\n"
        "Approved 2026-07-15T10:12:03+00:00 · from **claude**, round 3 · "
        "3 accept / 0 reject / 0 abstain\n"
    )


@pytest.mark.parametrize(
    ("events", "status", "reason"),
    [
        ([created(), consensus()], "awaiting_human", "awaiting human review"),
        ([created(), consensus(), decision("rejected")], "rejected", "reviewed"),
        ([created(), event("no_consensus", round=5,
                           content="no quorum")], "no_consensus", "no quorum"),
        ([created(), event("error", round=2,
                           content="agent failure")], "error", "agent failure"),
        ([created()], "created", "debate has not produced a candidate yet"),
        ([created(), event("run_config", phase="run")], "running",
         "debate has not produced a candidate yet"),
    ],
)
def test_non_approved_results_never_expose_candidate_text(events, status, reason):
    result = build_result(events)
    rendered = render_final(result)

    assert result["status"] == status
    assert result["answer"] is None
    assert result["reason"] == reason
    assert "text" not in result["candidate"] if result["candidate"] else True
    assert "Approved answer" not in str(result)
    assert "Approved answer" not in rendered
    assert rendered.startswith("# No answer\n")
    assert rendered.endswith("The full debate is in `summary.md`.\n")


def test_rejected_without_a_note_uses_the_required_fallback():
    result = build_result([created(), consensus(), decision("rejected", note="")])

    assert result["reason"] == "rejected without a note"
    assert render_final(result) == (
        "# No answer\n\n"
        "Candidate from **claude** (round 3) was **rejected** on "
        "2026-07-15T10:12:03+00:00:\n\n"
        "> rejected without a note\n\n"
        "The full debate is in `summary.md`.\n"
    )


def test_last_consensus_and_human_decision_win():
    result = build_result([
        created(),
        consensus("first", round=1, agent="first"),
        decision("rejected", note="first decision"),
        consensus("second", round=4, agent="second"),
        decision("approved", note="second decision", ts="later"),
    ])

    assert result["status"] == "approved"
    assert result["answer"] == "second"
    assert result["candidate"] == {"agent": "second", "round": 4}
    assert result["decided_at"] == "later"
    assert result["note"] == "second decision"


def test_approval_after_no_consensus_promotes_current_run_candidate_text():
    result = build_result([
        created(),
        event("run_config", phase="run"),
        event("candidate", round=2, agent="claude", content="fallback plan"),
        event("no_consensus", round=2, content="no quorum"),
        decision("approved"),
    ])

    assert result["status"] == "approved"
    assert result["answer"] == "fallback plan"
    assert result["candidate"] == {"agent": "claude", "round": 2}


def test_resume_running_preserves_candidate_for_later_approval():
    result = build_result([
        created(),
        event("run_config", phase="run", loaded_status="created"),
        event("candidate", round=2, agent="claude", content="fallback plan"),
        event("run_config", phase="run", loaded_status="running"),
        event("no_consensus", round=2, content="no quorum"),
        decision("approved"),
    ])

    assert result["status"] == "approved"
    assert result["answer"] == "fallback plan"
    assert result["candidate"] == {"agent": "claude", "round": 2}


def test_new_run_and_terminal_outcomes_clear_stale_result_fields():
    result = build_result([
        created(),
        consensus("first answer", round=1, agent="first"),
        event("run_config", phase="run"),
        event("error", round=2, content="propose failed", failed_phase="propose"),
        event("run_config", phase="run"),
        event("no_consensus", round=3, content="no quorum",
              tally={"accepts": 1, "rejects": 1, "abstains": 0,
                     "roster_size": 2, "required": 2}),
    ])

    assert result["status"] == "no_consensus"
    assert result["candidate"] is None
    assert result["tally"] == {"accepts": 1, "rejects": 1, "abstains": 0,
                                "roster_size": 2, "required": 2}
    assert result["failed_phase"] is None
    assert result["decided_at"] is None
    assert result["note"] is None


def test_legacy_transcript_uses_caller_identity_fallbacks():
    result = build_result(
        [consensus(), decision("approved")],
        id_fallback="legacy-id",
        title_fallback="Legacy title",
    )

    assert result["id"] == "legacy-id"
    assert result["title"] == "Legacy title"


def test_legacy_no_consensus_without_tally_renders_without_inventing_one():
    result = build_result([event("no_consensus", round=5, content="no quorum")])

    assert result["tally"] is None
    assert render_final(result) == (
        "# No answer\n\n"
        "no quorum\n\n"
        "The full debate is in `summary.md`.\n"
    )


def test_legacy_error_without_failed_phase_renders_without_inventing_one():
    result = build_result([event("error", round=2, content="agent failure")])

    assert result["failed_phase"] is None
    assert render_final(result) == (
        "# No answer\n\n"
        "Halted in round 2: agent failure.\n\n"
        "The full debate is in `summary.md`.\n"
    )


def test_no_consensus_and_error_render_their_enriched_event_details():
    no_consensus = build_result([
        event("no_consensus", round=5, content="no quorum",
              tally={"accepts": 1, "rejects": 1, "abstains": 1,
                     "roster_size": 3, "required": 2}),
    ])
    error = build_result([
        event("error", round=2, content="agent failure", failed_phase="critique"),
    ])

    assert "Round cap 5 reached without a quorum: 1 accept / 1 reject / 1 abstain of 3 (2 required)." in render_final(no_consensus)
    assert "Halted in round 2 during **critique**: agent failure." in render_final(error)


def test_unknown_event_types_are_ignored():
    result = build_result([
        event("telemetry", content="must not matter", answer="wrong"),
        created(),
        consensus(),
        decision("approved"),
    ])

    assert result["answer"] == "Approved answer"


def test_result_module_does_not_import_impure_or_fold_modules():
    source = Path("debatelab/result.py").read_text()
    tree = ast.parse(source)
    banned = {"store", "orchestrator", "prompts", "cli", "replay"}

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[-1])

    assert imports.isdisjoint(banned)
