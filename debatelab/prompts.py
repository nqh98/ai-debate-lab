"""Prompt templates for each debate phase, plus reply parsers."""

import re


def format_blocks(items: dict[str, str]) -> str:
    return "\n\n".join(f"### {name}\n{text}" for name, text in items.items())


def propose_prompt(name: str, problem: str) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        "Give your best complete answer to the problem. "
        "Be concrete and justify key choices."
    )


def critique_prompt(
    name: str,
    problem: str,
    other_proposals: dict[str, str],
    reject_reasons: dict[str, str] | None = None,
) -> str:
    extra = ""
    if reject_reasons:
        extra = (
            "\n\nRejection reasons from the last vote:\n"
            + format_blocks(reject_reasons)
        )
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        "Current proposals from the other agents:\n"
        f"{format_blocks(other_proposals)}\n\n"
        "Critique each proposal: where you agree, flaws, and missing "
        f"considerations.{extra}"
    )


def revise_prompt(
    name: str,
    problem: str,
    own_proposal: str,
    critiques: dict[str, str],
) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f"Your current proposal:\n{own_proposal}\n\n"
        f"Critiques from all agents:\n{format_blocks(critiques)}\n\n"
        'Submit your revised proposal. Start with a short "Changes:" section '
        "stating what you changed and why (or why you changed nothing), "
        "then the full revised answer."
    )


def nominate_prompt(
    name: str,
    problem: str,
    proposals: dict[str, str],
    names: list[str],
) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f"Current proposals:\n{format_blocks(proposals)}\n\n"
        "Which single proposal (including your own) is closest to correct?\n"
        "Reply with exactly one line in this format, then one sentence of "
        "reasoning:\nNOMINATE: <agent-name>\n"
        f"Valid agent names: {', '.join(names)}"
    )


def vote_prompt(
    name: str,
    problem: str,
    candidate_agent: str,
    candidate_text: str,
) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f'Candidate final answer (from agent "{candidate_agent}"):\n'
        f"{candidate_text}\n\n"
        "Do you accept this as the final answer? Reply with exactly one "
        "line, then your reasoning:\nVOTE: accept\nor\nVOTE: reject"
    )


def parse_nomination(text: str, valid_names: list[str]) -> str | None:
    match = re.search(r'NOMINATE:\s*"?([\w.-]+)', text, re.IGNORECASE)
    if match and match.group(1) in valid_names:
        return match.group(1)
    for name in valid_names:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
    return None


def parse_vote(text: str) -> tuple[str, str]:
    """Return the verdict and full text; unparseable replies count as reject."""
    match = re.search(r"VOTE:\s*(accept|reject)", text, re.IGNORECASE)
    if match:
        return match.group(1).lower(), text.strip()
    first = next((line for line in text.splitlines() if line.strip()), "")
    return ("accept" if "accept" in first.lower() else "reject"), text.strip()
