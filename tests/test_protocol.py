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


def test_select_candidate_plurality():
    noms = {"a": "b", "b": "b", "c": "a"}
    assert protocol.select_candidate(noms, ["a", "b", "c"]) == "b"


def test_select_candidate_tie_breaks_by_config_order():
    noms = {"a": "c", "b": "b"}
    assert protocol.select_candidate(noms, ["a", "b", "c"]) == "b"


def test_select_candidate_empty_falls_back_to_first():
    assert protocol.select_candidate({}, ["a", "b"]) == "a"


def test_check_consensus():
    accept = {"vote": "accept", "reason": "r"}
    reject = {"vote": "reject", "reason": "r"}
    assert protocol.check_consensus({"a": accept, "b": accept}) is True
    assert protocol.check_consensus({"a": accept, "b": reject}) is False
    assert protocol.check_consensus({}) is False
