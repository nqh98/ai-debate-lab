# Protocol Correctness — Design Spec

**Date:** 2026-07-14
**Status:** Approved design, pending implementation
**Supersedes:** parts of `2026-07-14-ai-debate-lab-design.md` (see "Superseded decisions")

## Purpose

Make the debate mechanism incapable of silently producing a verdict no agent gave. Today a debate can report consensus that never happened, and can structurally favor whichever agent is listed first in `agents.yaml`. This spec fixes the mechanism itself.

Scope is deliberately narrow: correctness of parsing, voting, and candidate selection. No new phases, no state-shape rewrites, no presentation changes. Remaining work from the source analysis is parked in "Deferred roadmap".

## Motivating defects

One true bug, three superseded design decisions, one security slip.

**Bug — `parse_vote` manufactures consensus.** `prompts.py:106-107` falls back to `"accept" if "accept" in first.lower() else "reject"`. The reply *"I cannot accept this"* parses as **accept**. The function's own docstring (line 102) claims "unparseable replies count as reject", so the code contradicts its stated contract. This is the worst failure mode available to this tool: a fabricated consensus that reaches a human as a real one.

**Bug — `parse_nomination` guesses from prose.** `prompts.py:95-97` scans for the first valid agent name appearing anywhere in the text, so *"codex's answer is weakest"* nominates codex.

**Superseded — self-nomination.** `prompts.py:68` invites agents to nominate their own proposal ("including your own"), per the original spec line 101. If every agent self-nominates — a plausible LLM behavior — every nomination ties.

**Superseded — config-order tie-break.** `protocol.py:24` breaks ties via `min(tied, key=agent_order.index)`, per the original spec line 101. Combined with universal self-nomination, the first agent in `agents.yaml` wins every round structurally, regardless of answer quality.

**Superseded — consensus among responders.** `protocol.py:29` requires unanimity only among agents that voted, per the original spec line 143 ("unanimity is computed over agents that actually voted"). With `orchestrator.py:102` requiring only 2 responders, a 5-agent roster with 3 network failures and 2 accepts reports unanimous consensus. The debate `20260714-how-can-this-repository-be-improved-furt` is a live instance: 2 accepts, 1 abstention, recorded as consensus.

**Security — `serve` exposes the working directory.** `cli.py:195` passes `Path.cwd()` to the static handler, so the viewer serves `agents.yaml`, `.env`, and every other file in the folder. Pulled into this spec as a drive-by: it is a two-line change and does not warrant waiting for a viewer cycle.

## Design constraint: agents cannot be schema-constrained

The source analysis converged on "remove regex parsing, enforce native JSON Schema / Structured Outputs". **This is not implementable here.** The default roster is CLI-backed (`claude -p`, `codex exec`, `agy -p`); `CliAgent.ask` is a one-shot `subprocess.run` returning stdout (`cli_agent.py:22-40`) and `Agent.ask` returns `str` (`base.py:16`). No schema can be imposed on a subprocess.

Asking for JSON *in the prompt* is an unenforced convention with the same failure mode as `VOTE: accept`, but with more parsing surface (code fences, prose preambles). We therefore keep a strict line marker and make failure explicit rather than guessed.

A second consequence: **CLI agents are stateless.** Each `ask()` is a fresh subprocess with no session, so a re-ask cannot say "try again" — it must resend the full original prompt plus a strict suffix.

## Design

### 1. Strict parsers (`prompts.py`)

Both prose-guessing fallbacks are deleted. Parsers return `None` on no match; they never infer a verdict.

```python
def parse_vote(text: str) -> str | None:
    """Return 'accept' or 'reject', or None if the reply has no VOTE: line."""

def parse_nomination(text: str, valid_names: list[str]) -> str | None:
    """Return the nominated name, or None if no NOMINATE: line matches."""
```

`valid_names` is passed by the caller already excluding the nominating agent, so `parse_nomination` needs no self-awareness.

A re-ask prompt builder resends the original prompt with a strict suffix:

```python
def reask(original_prompt: str, required: str) -> str:
    return f"{original_prompt}\n\nReply with ONLY the line {required}. No other text."
```

`nominate_prompt` drops "(including your own)", states "You may not nominate yourself", and lists valid names excluding the recipient.

### 2. Candidate selection (`protocol.py`)

```python
def select_candidate(
    nominations: dict[str, str], agent_order: list[str], seed: str
) -> tuple[str, bool]:
    """Return (winner, was_fallback). Ties and the zero-nomination fallback
    resolve via a RNG seeded with `seed`, so selection is reproducible."""
```

The seed is passed **in** as an opaque string; `protocol.py` never learns what a debate id is. The module stays pure — the property the source analysis correctly identified as its strength. The orchestrator passes `f"{debate_id}:{round}"`.

Tie-break: `random.Random(seed).choice(sorted(tied))`. Unbiased across agents, yet deterministic and reproducible from the transcript alone. Plain `random.choice` was rejected: it would make candidate selection unverifiable on replay, defeating the event-sourcing direction of the deferred reliability work.

Zero valid nominations → `was_fallback=True`, winner chosen by the same seeded RNG over agents holding proposals. Never `agent_order[0]`.

### 3. Quorum (`protocol.py`)

```python
def tally(votes: dict, roster_size: int, quorum: Fraction) -> dict:
    """-> {accepts, rejects, abstains, roster_size, required}"""

def check_consensus(votes: dict, roster_size: int, quorum: Fraction) -> bool:
    """Zero rejects AND accepts >= ceil(quorum * roster_size)."""
```

`abstains` is **derived** as `roster_size - accepts - rejects`, not read from `state["abstained"]`. That list is reset per round (`orchestrator.py:48`) and accumulates abstentions from both the nominate and vote fanouts, so an agent that failed to nominate but voted fine would otherwise be double-counted. Deriving it keeps the tally's columns summing to the roster by construction.

**Quorum uses exact `fractions.Fraction` arithmetic, never floats.** Stored as the string `"2/3"`. A float `0.667` would give `ceil(0.667 * 3) = ceil(2.001) = 3`, silently requiring unanimity on the default 3-agent roster and breaking the rule this spec defines. `ceil(Fraction(2,3) * 3) = 2` is exact.

Default `2/3`, configurable via `--quorum` on `debate run`, persisted in state alongside `max_rounds`.

| roster | accepts | rejects | abstains | required | result |
|---|---|---|---|---|---|
| 3 | 2 | 0 | 1 | 2 | consensus |
| 3 | 1 | 0 | 2 | 2 | no_consensus |
| 3 | 2 | 1 | 0 | 2 | no_consensus (reject blocks) |
| 5 | 3 | 0 | 2 | 4 | no_consensus |
| 5 | 4 | 0 | 1 | 4 | consensus |

### 4. Denominator integrity: `roster` in state

State gains `roster: list[str]`, recorded at run start from the orchestrator's agent set.

Without it the quorum denominator is unauditable after the fact, and — worse — resuming a debate after editing `agents.yaml` would silently change the denominator mid-debate. On resume with a differing roster, emit a `roster_changed` event recording both sets and continue with the current roster.

`roster` is the set the `Orchestrator` was constructed with: agents both `enabled` and passing readiness (`cli.py:38-48`). An agent enabled in YAML but missing its API key never entered the debate; counting it would make consensus permanently unreachable.

### 5. Orchestrator: parse failure is not agent failure

`_fanout` is unchanged — `AgentError` → `abstained`, `DebateHalted` under 2 responders. Parse handling is a separate pass, because the two failures are different: an agent that responded unparseably has not vanished.

This distinction matters concretely. If all three nominations fail to parse, that is **not** a halt — it is exactly the zero-nomination case, which emits `fallback_candidate` and proceeds. Only genuine non-response halts a debate.

Per agent whose reply did not parse: re-ask once with the strict suffix; if it still fails, record `abstained` with the raw reply as content. Re-asks run serially after the fanout — they are rare and FAST-task, so concurrency is not worth the complexity.

New events:

| Event | When |
|---|---|
| `fallback_candidate` | Zero valid nominations; records the seeded pick |
| `nomination_dropped` | Agent self-nominated despite the prompt |
| `roster_changed` | Resume detected a roster differing from state |
| `consensus` | Now carries the full `tally()` |

### 6. Serve scope (`cli.py`)

`cmd_serve` passes the debates root, not `Path.cwd()`. The viewer fetches `index.json` and per-debate files, all of which live under `debates/`, so nothing else needs serving.

### 7. Compatibility

Existing `state.json` files predate `roster` and `quorum`. Reads default them (`quorum="2/3"`; `roster` from the current run, emitting `roster_changed`) rather than raising.

## Testing

Unit tests, `MockAgent` only, no network:

- **Parsing:** `"I cannot accept this"` does not parse as accept (the headline regression); `VOTE: reject` with prose after; missing marker → `None`; `"codex's answer is weakest"` does not nominate codex.
- **Re-ask:** unparseable then parseable → verdict counted, one extra call; unparseable twice → `abstained` with raw text; `AgentError` during re-ask → `abstained`.
- **Quorum:** every row of the table above.
- **Selection:** self-nominations dropped and `nomination_dropped` emitted; all-self-nominating roster → `fallback_candidate`; zero nominations never returns `agent_order[0]`.
- **Tie-break:** same `(debate_id, round)` yields the same winner across runs (reproducibility); different rounds distribute across tied agents (no fixed bias).
- **Roster:** quorum uses recorded roster, not responder count; changed roster on resume emits `roster_changed`.
- **Serve:** a file outside `debates/` (e.g. `agents.yaml`) is not retrievable.

Integration: extend the existing mock-agent debate so one agent replies unparseably, asserting it abstains and does not flip consensus.

## Superseded decisions

From `2026-07-14-ai-debate-lab-design.md`:

- **Line 101**, "including its own" → self-nomination is now forbidden.
- **Line 101**, "ties broken by lowest agent index in config order" → now a seeded, reproducible RNG.
- **Lines 103 and 143**, "unanimous `accept` among voting agents" / "unanimity is computed over agents that actually voted" → now zero rejects plus a quorum over the recorded roster.

The original rationale for line 143 was that abstentions appear in the tally "so the human can judge legitimacy at approval time". That remains true and useful, but it places the burden on a human noticing a weak tally, while the status still reads as consensus. The quorum rule makes the mechanism itself refuse to call it consensus.

## Deferred roadmap

Each needs its own spec/plan cycle. Not scheduled by this spec.

**Reliability** — per-debate run lock with stale detection; atomic writes for `summary.md` and `index.json` (`store.py:101,132` use bare `write_text` while `write_state` at `store.py:88-92` is atomic); `replay(events) -> state` plus `debate fsck` to make the checkpoint a cache rather than a second truth; error classification and exponential backoff (`orchestrator.py:87-88` retries instantly, which is exactly wrong for rate limits); per-call telemetry (`duration_ms`, model, tokens); context budgeting, since every critique/revise prompt embeds all proposals in full each round.

**Result presentation** — `result.json` (machine-readable) and `final.md` (the answer alone, with provenance), both derived from the transcript rather than becoming a third source of truth; `debate result <id>`; richer `debate status`; `summary.md` split by an explicit "Debate process" divider.

**Viewer** — sanitized markdown rendering (agents write markdown, `viewer/index.html:113` shows escaped monospace — the highest readability-per-line change available); final-answer hero panel with the transcript collapsed beneath; phase grouping within rounds; revision diffs.

**Protocol features** — synthesis phase (winner drafts a merged answer incorporating accepted critiques, voted on instead of a verbatim proposal; adds a 5th phase, changing `PHASES`, `next_phase`, resume semantics, and state shape); prompt anonymization to "Agent A/B/C" against brand-reputation bias.

**Polish** — word-boundary title truncation (`cli.py:30` cuts at 60 chars mid-word; the source debate is titled "…from its intern"); non-zero exit codes (`run` prints `final status: error` but exits 0); `run --new`; `debate export`.

## Explicitly rejected

- **Native structured outputs / JSON schema as the parsing contract** — not implementable against CLI-backed agents (see "Design constraint").
- **Plain random tie-break** — breaks reproducibility from the transcript.
- **Message brokers, Redis, microservices, React migration** — disproportionate to a stdlib-only local CLI; the orchestrator already fans out concurrently via `ThreadPoolExecutor`.
- **Five-class orchestrator split** (`PhasePlanner`/`AgentRunner`/`ConsensusEngine`/`CheckpointWriter`) — the separation already exists across `protocol.py`, `prompts.py`, and `store.py`; adding classes over 195 lines buys indirection, not capability.
