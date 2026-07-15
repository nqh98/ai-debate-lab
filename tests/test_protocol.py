from debatelab import protocol


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


def test_check_consensus():
    accept = {"vote": "accept", "reason": "r"}
    reject = {"vote": "reject", "reason": "r"}
    assert protocol.check_consensus({"a": accept, "b": accept}) is True
    assert protocol.check_consensus({"a": accept, "b": reject}) is False
    assert protocol.check_consensus({}) is False
