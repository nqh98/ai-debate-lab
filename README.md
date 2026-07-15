# AI Debate Lab

Multiple AI agents (Claude, Codex, Antigravity, Grok, ...) analyze the same
problem in a structured debate — propose, critique, revise, nominate,
synthesize, vote — until they reach consensus on one answer. Nothing is
final until a human approves it. Every artifact is a plain file you can
read, diff, and commit.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
```

Configure agents in `agents.yaml` (flip `enabled`, add entries, set
`api_key_env` env vars for API-backed agents). Each agent uses one of three
backends: `cli` (runs a local command), `api` (direct HTTP call using an API
key), or `auto` (uses the `command` if its binary is on PATH, otherwise
falls back to the API key — configure both).

Agents are named after the platform that powers them (the Antigravity
agent runs Gemini through the `agy` CLI). Model names are not pinned:
each agent auto-selects the most appropriate model per debate task —
the strongest available model for propose/critique/revise, the cheapest
for nominate/vote — from the models the platform reports at runtime
(`models_command` for CLI backends, the provider's models endpoint for
API backends). Platforms that can't report models use their own default
routing; add `model: <name>` to an entry to pin one. Check readiness:

```bash
debate agents          # static readiness check
debate agents --ping   # live test prompt to each ready agent
```

## Run a debate

```bash
debate new "Which caching strategy should we adopt?" --context notes.md
debate run 20260714-which-caching-strategy   # streams phase progress
debate show 20260714-which-caching-strategy  # read the summary
debate approve 20260714-which-caching-strategy -m "agreed"
debate result 20260714-which-caching-strategy        # prints final.md
debate result 20260714-which-caching-strategy --json # prints result.json
# or: debate reject <id> -m "reason"
```

`run` resumes from the last completed phase if interrupted. If no consensus
is reached within `--max-rounds` (default 5), the debate ends
`no_consensus` with all dissents recorded — you still decide with
approve/reject. `--max-rounds` must be at least 1.

`run` exits `0` for consensus awaiting review, `3` for a halted error, and `1`
for no consensus or any other non-awaiting, non-error status. `result` exits
`0` for an approved answer and `1` when there is no answer. Under `set -e`, a
legitimate no-consensus result aborts a script; use `|| true` when that outcome
should not abort the script.

Human decisions are recovered from the append-only `human_decision` transcript
event. Repeating the same approve/reject command reconciles `state.json`,
`summary.md`, and the index without adding another event; a conflicting later
decision is refused. Debate phases checkpoint state only after a phase finishes,
so phase events emitted before an interruption may be duplicated when that
uncommitted phase resumes.

## Watch in the browser

```bash
debate serve --port 8080   # http://127.0.0.1:8080/
```

## How a debate is stored (`debates/<id>/`)

| File | Purpose |
|---|---|
| `problem.md` | Problem + shared context |
| `transcript.jsonl` | Append-only event log — source of truth |
| `state.json` | Current derived state (resume checkpoint) |
| `summary.md` | Human-readable summary, regenerated each round |

## Protocol

Each round: **critique → revise → nominate → synthesize → vote** (round 1
starts with **propose**). The nominate phase elects a candidate by plurality
— an agent may not nominate itself, and ties break by a draw seeded from
`<debate-id>:<round>`, so selection is unbiased yet reproducible from the
transcript. The synthesize phase asks the winner to merge every proposal and
critique into a single answer; if that call fails or comes back empty, the
debate falls back to the winner's verbatim proposal and records
`synthesis_failed`. Every agent then accepts or rejects the candidate. Zero
rejects plus accepts ≥ `ceil(quorum × roster)` (default `2/3` of the roster
the run started with) = consensus → `awaiting_human`. Failed agent calls
retry with exponential backoff, then abstain for the phase; a phase needs at
least 2 responders, except synthesize, which degrades instead of halting.
Agents that can't respond at all (missing API key, command not on PATH) are
skipped at `debate run` startup with a warning — the debate proceeds with
the remaining agents.

## Tests

```bash
.venv/bin/python -m pytest -v
```
