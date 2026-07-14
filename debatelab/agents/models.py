"""Task-aware model auto-selection. No model names are hardcoded: each
platform reports the models it currently offers, and we rank those names
by generic capability/cost markers to pick the best fit per task."""
import re

# Task kinds the orchestrator asks for.
DEEP = "deep"  # propose/critique/revise — strongest reasoning wins
FAST = "fast"  # nominate/vote — short structured replies, cheapest model wins

_LIGHT_MARKERS = {"flash", "mini", "nano", "lite", "haiku", "fast", "low", "small"}
_HEAVY_MARKERS = {"pro", "opus", "high", "max", "ultra", "large", "deep"}


def _score(model: str) -> int:
    """Positive = heavyweight tier, negative = lightweight, 0 = no signal."""
    tokens = set(re.split(r"[^a-z]+", model.lower()))
    return len(tokens & _HEAVY_MARKERS) - len(tokens & _LIGHT_MARKERS)


def choose_model(available: list[str], task: str) -> str | None:
    """Pick the best available model for the task, or None when there is no
    basis to prefer one (empty list, or indistinguishable names) — callers
    then fall back to the platform's own default routing."""
    scores = [_score(model) for model in available]
    if not scores or len(set(scores)) == 1:
        return None
    target = min(scores) if task == FAST else max(scores)
    return available[scores.index(target)]
