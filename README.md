# AI Debate Lab

Multiple AI agents (Claude, Codex, Antigravity, Grok, ...) analyze the same
problem in a structured debate — propose, critique, revise, vote — until
they unanimously agree on one answer. Nothing is final until a human
approves it. Every artifact is a plain file you can read, diff, and commit.

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
# or: debate reject <id> -m "reason"
```

`run` resumes from the last completed phase if interrupted. If no unanimous
vote happens within `--max-rounds` (default 5), the debate ends
`no_consensus` with all dissents recorded — you still decide with
approve/reject. `--max-rounds` must be at least 1.

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

Each round: **critique → revise → vote** (round 1 starts with **propose**).
The vote phase nominates a candidate (plurality, config-order tie-break),
then every agent accepts/rejects it. Unanimous accept = consensus →
`awaiting_human`. Failed agent calls retry once, then abstain for the
phase; a phase needs at least 2 responders. Agents that can't respond at
all (missing API key, command not on PATH) are skipped at `debate run`
startup with a warning — the debate proceeds with the remaining agents.

## Tests

```bash
.venv/bin/python -m pytest -v
```
