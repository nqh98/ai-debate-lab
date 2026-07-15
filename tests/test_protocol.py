from fractions import Fraction

from debatelab import protocol


Q = Fraction(2, 3)
ACCEPT = {"vote": "accept", "reason": "r"}
REJECT = {"vote": "reject", "reason": "r"}


def test_next_phase_fresh_debate_starts_with_propose():
    assert protocol.next_phase(0, None) == (1, "propose")


def test_next_phase_walks_round_one():
    assert protocol.next_phase(1, "propose") == (1, "critique")
    assert protocol.next_phase(1, "critique") == (1, "revise")
    assert protocol.next_phase(1, "revise") == (1, "vote")


def test_next_phase_after_vote_skips_propose():
    assert protocol.next_phase(1, "vote") == (2, "critique")
    assert protocol.next_phase(3, "vote") == (4, "critique")


def test_select_candidate_plurality_wins():
    noms = {"a": "b", "b": "b", "c": "a"}
    assert protocol.select_candidate(noms, ["a", "b", "c"], "d:1") == ("b", False)


def test_select_candidate_tie_break_is_reproducible():
    """Same debate + round must always pick the same winner, so a debate
    stays verifiable by replaying its transcript."""
    noms = {"a": "c", "b": "b"}
    first = protocol.select_candidate(noms, ["a", "b", "c"], "d:1")
    assert first == protocol.select_candidate(noms, ["a", "b", "c"], "d:1")
    assert first[0] in ("b", "c")
    assert first[1] is False


def test_select_candidate_tie_break_is_not_config_order():
    """Regression: config order used to decide every tie, so the first agent
    in agents.yaml won structurally."""
    noms = {"a": "c", "b": "b"}
    winners = {
        protocol.select_candidate(noms, ["a", "b", "c"], f"d:{i}")[0]
        for i in range(20)
    }
    assert winners == {"b", "c"}


def test_select_candidate_no_nominations_is_a_flagged_fallback():
    winner, was_fallback = protocol.select_candidate({}, ["a", "b"], "d:1")
    assert winner in ("a", "b")
    assert was_fallback is True


def test_select_candidate_fallback_is_not_always_the_first_agent():
    winners = {
        protocol.select_candidate({}, ["a", "b"], f"d:{i}")[0] for i in range(20)
    }
    assert winners == {"a", "b"}


def test_required_accepts_uses_exact_fraction_arithmetic():
    # A float quorum of 0.667 would give ceil(0.667*3) == 3, silently
    # demanding unanimity on the default 3-agent roster.
    assert protocol.required_accepts(3, Q) == 2
    assert protocol.required_accepts(4, Q) == 3
    assert protocol.required_accepts(5, Q) == 4


def test_check_consensus_quorum_table():
    votes3 = {"a": ACCEPT, "b": ACCEPT}
    assert protocol.check_consensus(votes3, 3, Q) is True
    assert protocol.check_consensus({"a": ACCEPT}, 3, Q) is False
    assert protocol.check_consensus(votes3, 5, Q) is False
    five = {n: ACCEPT for n in "abcd"}
    assert protocol.check_consensus(five, 5, Q) is True


def test_check_consensus_any_reject_blocks_even_at_quorum():
    votes = {"a": ACCEPT, "b": ACCEPT, "c": REJECT}
    assert protocol.check_consensus(votes, 3, Q) is False


def test_check_consensus_no_votes_is_false():
    assert protocol.check_consensus({}, 3, Q) is False


def test_tally_derives_abstains_from_the_roster():
    """abstains is roster minus voters, not state['abstained'] — that list is
    per-round and mixes nominate- and vote-phase abstentions."""
    t = protocol.tally({"a": ACCEPT, "b": REJECT}, 5, Q)
    assert t == {
        "accepts": 1, "rejects": 1, "abstains": 3,
        "roster_size": 5, "required": 4, "quorum": "2/3",
    }
