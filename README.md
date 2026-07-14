# AI Debate Lab

Multiple AI agents (Claude, ChatGPT, Gemini, Grok, ...) analyze the same
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
`api_key_env` env vars for API-backed agents). Check readiness:

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
approve/reject.

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
phase; a phase needs at least 2 responders.

## Tests

```bash
.venv/bin/python -m pytest -v
```
