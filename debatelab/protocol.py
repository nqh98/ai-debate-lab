"""Pure debate-protocol logic: phase sequencing, candidate selection, consensus.

This module is deliberately pure: no files, no clock, no network, no
knowledge of debate ids. Callers pass a `seed` string and a `roster_size`.
"""

import random
from collections import Counter

PHASES = ("propose", "critique", "revise", "vote")


def next_phase(round_num: int, last_completed: str | None) -> tuple[int, str]:
    """Return the round and phase to run next."""
    if round_num == 0:
        return 1, "propose"
    if last_completed == "vote":
        return round_num + 1, "critique"
    return round_num, PHASES[PHASES.index(last_completed) + 1]


def select_candidate(
    nominations: dict[str, str], agent_order: list[str], seed: str
) -> tuple[str, bool]:
    """Return (winner, was_fallback) for the plurality nominee.

    Ties and the zero-nomination fallback resolve with an RNG seeded from
    `seed`, so selection is unbiased across agents yet reproducible from the
    transcript alone. Config order deliberately does NOT decide ties: it made
    the first agent in agents.yaml win structurally.
    """
    rng = random.Random(seed)
    if not nominations:
        return rng.choice(sorted(agent_order)), True
    counts = Counter(nominations.values())
    best = max(counts.values())
    tied = sorted(name for name, count in counts.items() if count == best)
    return rng.choice(tied), False


def check_consensus(votes: dict[str, dict]) -> bool:
    """Return whether every agent that voted accepted the candidate."""
    return bool(votes) and all(vote["vote"] == "accept" for vote in votes.values())
