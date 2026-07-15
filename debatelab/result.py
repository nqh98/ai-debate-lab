"""Pure projections of terminal debate transcript events."""


def build_result(
    events: list, *, id_fallback: str | None = None, title_fallback: str | None = None
) -> dict:
    """Build a result document from the terminal events in a transcript."""
    debate_id = id_fallback
    title = title_fallback
    status = "created"
    candidate = None
    candidate_text = None
    tally = None
    decided_at = None
    note = None
    reason = "debate has not produced a candidate yet"
    round_ = None
    failed_phase = None

    for event in events:
        event_type = event.get("type")
        if event_type == "debate_created":
            debate_id = event.get("id", debate_id)
            title = event.get("title", title)
            status = "created"
            reason = "debate has not produced a candidate yet"
        elif event_type == "run_config":
            status = "running"
            reason = "debate has not produced a candidate yet"
        elif event_type == "consensus":
            candidate = {"agent": event.get("agent"), "round": event.get("round")}
            candidate_text = event.get("content")
            tally = event.get("tally")
            round_ = event.get("round")
            status = "awaiting_human"
            reason = "awaiting human review"
        elif event_type == "no_consensus":
            status = "no_consensus"
            reason = event.get("content")
            tally = event.get("tally")
            round_ = event.get("round")
            failed_phase = None
        elif event_type == "error":
            status = "error"
            reason = event.get("content")
            failed_phase = event.get("failed_phase")
            round_ = event.get("round")
        elif event_type == "human_decision":
            status = event.get("content")
            decided_at = event.get("ts")
            note = event.get("note", "")
            if status == "approved":
                reason = None
            elif status == "rejected":
                reason = note or "rejected without a note"

    answer = candidate_text if status == "approved" else None
    return {
        "id": debate_id,
        "title": title,
        "status": status,
        "answer": answer,
        "candidate": candidate,
        "tally": tally,
        "decided_at": decided_at,
        "note": note,
        "reason": reason,
        "round": round_,
        "failed_phase": failed_phase,
    }


def _tally_text(tally: dict) -> str:
    return (
        f"{tally['accepts']} accept / {tally['rejects']} reject / "
        f"{tally['abstains']} abstain"
    )


def render_final(result: dict) -> str:
    """Render the final human-facing markdown view of a result document."""
    if result["answer"] is not None:
        provenance = (
            f"Approved {result['decided_at']} · from **{result['candidate']['agent']}**, "
            f"round {result['candidate']['round']}"
        )
        if result["tally"] is not None:
            provenance += f" · {_tally_text(result['tally'])}"
        return f"# Answer\n\n{result['answer']}\n\n---\n{provenance}\n"

    status = result["status"]
    reason = result["reason"]
    if status == "rejected":
        candidate = result["candidate"]
        if candidate is not None:
            message = (
                f"Candidate from **{candidate['agent']}** (round {candidate['round']}) "
                "was **rejected**"
            )
        else:
            message = "The candidate was **rejected**"
        if result["decided_at"] is not None:
            message += f" on {result['decided_at']}"
        body = f"{message}:\n\n> {reason}"
    elif status == "no_consensus" and result["tally"] is not None and result["round"] is not None:
        tally = result["tally"]
        body = (
            f"Round cap {result['round']} reached without a quorum: "
            f"{_tally_text(tally)} of {tally['roster_size']} "
            f"({tally['required']} required)."
        )
    elif status == "error" and result["round"] is not None:
        if result["failed_phase"] is not None:
            body = (
                f"Halted in round {result['round']} during "
                f"**{result['failed_phase']}**: {reason}."
            )
        else:
            body = f"Halted in round {result['round']}: {reason}."
    else:
        body = reason

    return f"# No answer\n\n{body}\n\nThe full debate is in `summary.md`.\n"
