from debatelab import prompts


def test_propose_prompt_contains_problem_and_name():
    p = prompts.propose_prompt("alpha", "What color?")
    assert "alpha" in p and "What color?" in p


def test_critique_prompt_excludes_nothing_but_shows_others():
    p = prompts.critique_prompt("alpha", "Q", {"beta": "B's idea"})
    assert "B's idea" in p and "### beta" in p


def test_critique_prompt_includes_reject_reasons_when_given():
    p = prompts.critique_prompt("alpha", "Q", {"beta": "B"}, {"gamma": "too vague"})
    assert "too vague" in p
    p2 = prompts.critique_prompt("alpha", "Q", {"beta": "B"})
    assert "Rejection reasons" not in p2


def test_revise_prompt_contains_own_and_critiques():
    p = prompts.revise_prompt("alpha", "Q", "my old take", {"beta": "weak point X"})
    assert "my old take" in p and "weak point X" in p and "Changes:" in p


def test_nominate_prompt_forbids_self_and_excludes_self_from_valid_names():
    p = prompts.nominate_prompt(
        "alpha", "Q", {"alpha": "A", "beta": "B"}, ["alpha", "beta"]
    )
    assert "NOMINATE:" in p
    assert "may NOT nominate your own" in p
    assert "### alpha" in p
    assert "Valid agent names: beta" in p


def test_vote_prompt_contains_candidate():
    p = prompts.vote_prompt("alpha", "Q", "beta", "the answer")
    assert "VOTE:" in p and "the answer" in p and "beta" in p


def test_reask_resends_the_whole_prompt():
    """CLI agents are stateless subprocesses with no session, so a re-ask
    cannot just say 'try again' — it must resend the original prompt."""
    original = prompts.vote_prompt("alpha", "Q", "beta", "the answer")
    r = prompts.reask(original, prompts.VOTE_REQUIRED)
    assert original in r
    assert "could not be parsed" in r
    assert "ONLY" in r
    assert prompts.VOTE_REQUIRED in r


def test_parse_nomination_reads_the_marker_line():
    names = ["alpha", "beta"]
    assert prompts.parse_nomination("NOMINATE: beta\nbecause...", names) == "beta"
    assert prompts.parse_nomination('NOMINATE: "beta"\nbecause...', names) == "beta"
    assert prompts.parse_nomination("nominate:   alpha", names) == "alpha"


def test_parse_nomination_never_guesses_from_prose():
    names = ["alpha", "beta"]
    assert prompts.parse_nomination("I think beta's plan is weakest", names) is None
    assert prompts.parse_nomination("NOMINATE: beta's", names) is None
    assert prompts.parse_nomination("no idea", names) is None
    assert prompts.parse_nomination("NOMINATE: gamma", names) is None


def test_parse_nomination_returns_self_so_caller_can_drop_it():
    assert prompts.parse_nomination("NOMINATE: alpha", ["alpha", "beta"]) == "alpha"


def test_parse_vote_reads_the_marker_line():
    assert prompts.parse_vote("VOTE: accept\nlooks good") == "accept"
    assert prompts.parse_vote("vote: REJECT\nmissing X") == "reject"


def test_parse_vote_never_infers_a_verdict_from_prose():
    assert prompts.parse_vote("VOTE: accepted") is None
    assert prompts.parse_vote("I cannot accept this") is None
    assert prompts.parse_vote("I do not accept") is None
    assert prompts.parse_vote("I accept this fine answer") is None
    assert prompts.parse_vote("hmm not sure about this") is None
    assert prompts.parse_vote("") is None


def test_synthesize_prompt_carries_every_proposal_and_critique():
    p = prompts.synthesize_prompt(
        "alpha",
        "Q",
        {"alpha": "A's idea", "beta": "B's idea"},
        {"alpha": "A's critique", "beta": "B's critique"},
    )
    assert prompts.SYNTHESIS_HEADER in p
    assert "A's idea" in p and "B's idea" in p
    assert "A's critique" in p and "B's critique" in p
    assert "### alpha" in p and "### beta" in p


def test_synthesize_prompt_includes_reject_reasons_when_given():
    p = prompts.synthesize_prompt(
        "alpha", "Q", {"alpha": "A"}, {"beta": "C"}, {"gamma": "too vague"}
    )
    assert "too vague" in p
    p2 = prompts.synthesize_prompt("alpha", "Q", {"alpha": "A"}, {"beta": "C"})
    assert "too vague" not in p2
    assert "rejected" not in p2.lower()


def test_synthesize_prompt_forbids_a_preamble():
    """The reply is published verbatim as the answer, so the prompt must ban
    the "Changes:" section revise_prompt asks for. See spec §3."""
    p = prompts.synthesize_prompt("alpha", "Q", {"alpha": "A"}, {"beta": "C"})
    assert "Changes:" in p
    assert "answer ONLY" in p


def test_synthesize_prompt_asks_for_a_merge_not_a_defence():
    p = prompts.synthesize_prompt("alpha", "Q", {"alpha": "A"}, {"beta": "C"})
    assert "merg" in p.lower()
    assert "Do not simply restate your own proposal." in p


def test_synthesize_prompt_is_detectable_by_its_header():
    """conftest.MockAgent routes on this substring, and no other prompt may
    collide with it."""
    others = [
        prompts.propose_prompt("a", "Q"),
        prompts.critique_prompt("a", "Q", {"b": "B"}),
        prompts.revise_prompt("a", "Q", "own", {"b": "B"}),
        prompts.nominate_prompt("a", "Q", {"a": "A", "b": "B"}, ["a", "b"]),
        prompts.vote_prompt("a", "Q", "b", "B"),
    ]
    for p in others:
        assert prompts.SYNTHESIS_HEADER not in p
