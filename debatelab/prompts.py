"""Prompt templates for each debate phase, plus reply parsers."""

import re


VOTE_REQUIRED = "'VOTE: accept' or 'VOTE: reject'"
NOMINATE_REQUIRED = "'NOMINATE: <agent-name>'"
# Part of the prompt's contract, not a test hook: the synthesize call is the
# only one addressed to a single agent, so this header is how a reader (and
# tests/conftest.py's MockAgent) tells a synthesis prompt from the others.
SYNTHESIS_HEADER = "Proposals to merge:"


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
        "Which single proposal is closest to correct?\n"
        "You may NOT nominate your own proposal.\n"
        "Reply with exactly one line in this format, then one sentence of "
        "reasoning:\nNOMINATE: <agent-name>\n"
        f"Valid agent names: {', '.join(n for n in names if n != name)}"
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


def synthesize_prompt(
    name: str,
    problem: str,
    proposals: dict[str, str],
    critiques: dict[str, str],
    reject_reasons: dict[str, str] | None = None,
) -> str:
    """Ask the nomination winner to merge the roster's work into one answer.

    Deliberately not revise_prompt: that hands one agent its OWN proposal and
    asks it to defend it. This hands the winner EVERY proposal and asks for a
    merge. The instruction not to restate its own proposal is load-bearing --
    without it the winner re-emits its proposal and the phase costs a DEEP
    call to reproduce the status quo.

    The reply is published verbatim as the answer, so there is no marker to
    parse and the prompt must ban the "Changes:" preamble revise_prompt asks
    for. See specs/2026-07-15-synthesis-phase-design.md §3.
    """
    extra = ""
    if reject_reasons:
        extra = (
            "\n\nThe roster rejected the previous answer for these reasons:\n"
            + format_blocks(reject_reasons)
        )
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n'
        "The other agents nominated your proposal, so you draft the final "
        "answer.\n\n"
        f"Problem:\n{problem}\n\n"
        f"{SYNTHESIS_HEADER}\n{format_blocks(proposals)}\n\n"
        f"Critiques from all agents:\n{format_blocks(critiques)}{extra}\n\n"
        "Write the single best answer to the problem. Merge the strongest "
        "reasoning from every proposal above and address the critiques. "
        "Do not simply restate your own proposal.\n\n"
        'Reply with the answer ONLY. No preamble, no "Changes:" section, no '
        "commentary about the debate or about what you merged."
    )


def parse_nomination(text: str, valid_names: list[str]) -> str | None:
    """Return a valid agent named by a NOMINATE marker, or None."""
    match = re.search(
        r'^[ \t]*NOMINATE:[ \t]*(?:"([\w.-]+)"|([\w.-]+))'
        r'(?=[ \t]*(?:\r?$|\r?\n))',
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    nominee = match.group(1) or match.group(2) if match else None
    if nominee in valid_names:
        return nominee
    return None


def parse_vote(text: str) -> str | None:
    """Return the marked verdict, or None when no VOTE marker is present."""
    match = re.search(
        r"^[ \t]*VOTE:[ \t]*(accept|reject)"
        r"(?=[ \t]*(?:\r?$|\r?\n))",
        text,
        re.IGNORECASE | re.MULTILINE,
    )
    return match.group(1).lower() if match else None


def reask(original_prompt: str, required: str) -> str:
    """Re-ask an agent whose reply did not parse.

    The full original prompt is resent because CLI agents are one-shot
    subprocesses with no conversation state - there is nothing for a bare
    "try again" to refer to.
    """
    return (
        f"{original_prompt}\n\n"
        "Your previous reply could not be parsed. "
        f"Reply with ONLY the line {required}. No other text."
    )
