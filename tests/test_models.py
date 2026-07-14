from debatelab.agents.models import DEEP, FAST, choose_model

AGY_ROSTER = [
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
]


def test_deep_picks_heaviest_model():
    assert choose_model(AGY_ROSTER, DEEP) == "Gemini 3.1 Pro (High)"


def test_fast_picks_lightest_model():
    assert choose_model(AGY_ROSTER, FAST) == "Gemini 3.5 Flash (Low)"


def test_markers_match_whole_tokens_only():
    # "gemini" must not be read as the light marker "mini"
    assert choose_model(["gemini-x", "solid-x-mini"], DEEP) == "gemini-x"


def test_empty_list_gives_none():
    assert choose_model([], DEEP) is None
    assert choose_model([], FAST) is None


def test_indistinguishable_names_give_none():
    # No tier signal — callers fall back to the platform default.
    assert choose_model(["model-a", "model-b"], DEEP) is None
    assert choose_model(["one-pro", "two-pro"], FAST) is None


def test_tie_breaks_to_first_listed():
    roster = ["alpha-pro", "beta-pro", "gamma-mini"]
    assert choose_model(roster, DEEP) == "alpha-pro"
