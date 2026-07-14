"""Pure debate-protocol logic: phase sequencing, candidate selection, consensus."""

from collections import Counter

PHASES = ("propose", "critique", "revise", "vote")


def next_phase(round_num: int, last_completed: str | None) -> tuple[int, str]:
    """Return the round and phase to run next."""
    if round_num == 0:
        return 1, "propose"
    if last_completed == "vote":
        return round_num + 1, "critique"
    return round_num, PHASES[PHASES.index(last_completed) + 1]


def select_candidate(nominations: dict[str, str], agent_order: list[str]) -> str:
    """Select the plurality winner, breaking ties by configured agent order."""
    if not nominations:
        return agent_order[0]
    counts = Counter(nominations.values())
    best = max(counts.values())
    tied = [name for name, count in counts.items() if count == best]
    return min(tied, key=agent_order.index)


def check_consensus(votes: dict[str, dict]) -> bool:
    """Return whether every agent that voted accepted the candidate."""
    return bool(votes) and all(vote["vote"] == "accept" for vote in votes.values())
