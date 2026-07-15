"""Pure debate-protocol logic: phase sequencing, candidate selection, consensus.

This module is deliberately pure: no files, no clock, no network, no
knowledge of debate ids. Callers pass a `seed` string and a `roster_size`.
"""

import math
import random
from collections import Counter
from fractions import Fraction

PHASES = ("propose", "critique", "revise", "nominate", "vote")
DEFAULT_QUORUM = Fraction(2, 3)


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


def required_accepts(roster_size: int, quorum: Fraction) -> int:
    """Accepts needed for consensus. Fraction arithmetic is required: a float
    0.667 gives ceil(0.667 * 3) == 3, which would demand unanimity on a
    3-agent roster instead of the intended 2."""
    return math.ceil(quorum * roster_size)


def tally(votes: dict, roster_size: int, quorum: Fraction) -> dict:
    """Vote breakdown against the roster the debate started with.

    `abstains` is derived as roster_size - accepts - rejects rather than read
    from state["abstained"], which resets per round and accumulates both
    nominate- and vote-phase abstentions.
    """
    accepts = sum(1 for v in votes.values() if v["vote"] == "accept")
    rejects = sum(1 for v in votes.values() if v["vote"] == "reject")
    return {
        "accepts": accepts,
        "rejects": rejects,
        "abstains": roster_size - accepts - rejects,
        "roster_size": roster_size,
        "required": required_accepts(roster_size, quorum),
        "quorum": str(quorum),
    }


def check_consensus(votes: dict, roster_size: int, quorum: Fraction) -> bool:
    """Consensus = zero rejects AND accepts >= ceil(quorum * roster_size).

    The denominator is the roster the run started with, not the agents that
    happened to reply: unanimity among responders let 2 accepts out of 5
    configured agents report as unanimous consensus.
    """
    counts = tally(votes, roster_size, quorum)
    return counts["rejects"] == 0 and counts["accepts"] >= counts["required"]
