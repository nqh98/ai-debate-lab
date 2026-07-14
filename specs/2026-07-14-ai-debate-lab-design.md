# AI Debate Lab — Design Spec

**Date:** 2026-07-14
**Status:** Approved design, pending implementation

## Purpose

A modular repository where multiple AI agents (Claude, Codex/ChatGPT, Gemini, Grok, others) analyze the same problem through a structured debate: independent proposals, cross-critique, revision, and voting, repeated until all participating agents accept one answer. Every artifact — shared context, discussion history, critiques, consensus result, and the final human-approved decision — is stored in plain files inside the repo.

## Goals

- Agents are trivially pluggable: add/remove/enable/disable via one YAML file, no code changes for existing backend types.
- Both CLI-backed agents (installed tools like `claude`, `codex`, `gemini`) and API-backed agents (OpenAI, Anthropic, Google, xAI) work behind one interface.
- Structured multi-round protocol with explicit consensus detection.
- Nothing is final without explicit human approval.
- All state is git-friendly plain text (JSON/JSONL/Markdown).
- Minimal dependencies: Python stdlib + PyYAML (+ httpx if preferred over urllib).

## Non-goals (v1)

- Live streaming UI (viewer polls instead of SSE).
- Cross-debate querying/analytics.
- Agent memory across debates.
- Concurrent runs of the same debate.

## Architecture

Single Python package `debatelab` with a console script `debate`. Synchronous orchestrator; per-phase agent calls fan out concurrently via `ThreadPoolExecutor` (calls are I/O-bound). File-backed state, append-only JSONL transcript as source of truth. Static single-page web viewer served by stdlib `http.server`.

### Repository layout

```
ai-debate-lab/
├── agents.yaml              # agent roster: backend, model, enabled flag
├── pyproject.toml           # package `debatelab`, console script `debate`
├── debatelab/
│   ├── agents/
│   │   ├── base.py          # Agent ABC: name, ask(prompt) -> str
│   │   ├── cli_agent.py     # generic subprocess adapter
│   │   ├── api_agent.py     # generic HTTP adapter (anthropic / openai-compatible / google)
│   │   └── registry.py      # loads agents.yaml -> enabled Agent instances
│   ├── protocol.py          # round state machine: propose → critique → revise → vote
│   ├── orchestrator.py      # runs a debate: fan-out, consensus check, resume
│   ├── store.py             # debate dirs, JSONL append, state.json, Markdown rendering
│   ├── prompts.py           # prompt templates for each phase
│   ├── cli.py               # argparse commands
│   └── viewer/index.html    # static viewer (vanilla JS, no build step)
├── debates/                 # one folder per debate (committed artifacts)
└── tests/
```

## Agent abstraction

`Agent` interface: `name: str`, `ask(prompt: str) -> str`.

- **CliAgent** — runs a configured command template as a subprocess (e.g. `["claude", "-p", "{prompt}"]`), captures stdout, enforces a timeout (default 180 s).
- **ApiAgent** — thin provider drivers selected by `provider` key:
  - `anthropic` — Messages API
  - `openai` — Chat Completions, honors optional `base_url` (covers xAI and any OpenAI-compatible endpoint)
  - `google` — Gemini generateContent
  API keys are read from the env var named in `api_key_env`; keys never live in config or artifacts.

New provider types register by name in a driver map — one small class each.

### agents.yaml

```yaml
agents:
  - name: claude
    backend: cli
    command: ["claude", "-p", "{prompt}"]
    enabled: true
  - name: chatgpt
    backend: api
    provider: openai
    model: gpt-5
    api_key_env: OPENAI_API_KEY
    enabled: true
  - name: gemini
    backend: cli
    command: ["gemini", "-p", "{prompt}"]
    enabled: true
  - name: grok
    backend: api
    provider: openai            # xAI is OpenAI-compatible
    base_url: https://api.x.ai/v1
    model: grok-4
    api_key_env: XAI_API_KEY
    enabled: true
```

Enable/disable = flip `enabled`. Add/remove = edit the list. The registry validates config at load and reports actionable errors (missing command, missing env var) per agent.

## Debate protocol

A debate runs rounds of four phases; every phase's output is appended to the transcript.

1. **Propose** (round 1 only) — each agent independently answers the problem given `problem.md`. Parallel.
2. **Critique** — each agent reviews all *other* agents' current proposals: agreements, flaws, missing considerations. Parallel.
3. **Revise** — each agent sees all critiques and submits a revised proposal, stating what it changed and why. Parallel.
4. **Vote** — the orchestrator selects a **candidate answer**: each agent names which current proposal (including its own) is closest to correct; plurality wins, ties broken by lowest agent index in config order. Every agent then votes `accept`/`reject` (with reason) on the candidate.

- **Consensus** = unanimous `accept` among voting agents → status `awaiting_human`.
- Any `reject` → reject reasons feed the next round's critique phase; rounds 2+ skip Propose (current revisions carry forward).
- After `max_rounds` (default 5, settable per debate via `run --max-rounds N`) without unanimity → status `no_consensus`, recording the final candidate, vote tally, and each dissent for the human.

**Human gate:** `debate approve <id> [-m note]` or `debate reject <id> -m note` records the decision as a transcript event with timestamp. `summary.md` shows the final decision only stamped APPROVED/REJECTED after this. Applies to both `awaiting_human` and `no_consensus` outcomes (the human may approve the candidate despite dissent).

## Storage — `debates/<id>/`

`<id>` = `YYYYMMDD-slug` derived from the problem title.

| File | Role |
|---|---|
| `problem.md` | Problem statement + shared context (pasted text or files copied in at creation) |
| `transcript.jsonl` | Append-only event log — source of truth. One JSON object per event: `{ts, round, phase, agent, type, content}` |
| `state.json` | Derived current state (status, round, current proposals, tally); rewritten after each phase |
| `summary.md` | Human-readable rendering regenerated from the transcript after each round |

`debates/index.json` (regenerated by the CLI) lists all debates for the viewer.

Statuses: `created → running → awaiting_human | no_consensus → approved | rejected`, plus `error` for unrecoverable halts.

## CLI

| Command | Behavior |
|---|---|
| `debate new "problem" [--context file...]` | Create debate folder, print id |
| `debate run <id> [--max-rounds N]` | Run rounds until consensus/cap; stream phase progress; resumes from last completed phase if interrupted |
| `debate status <id>` / `debate list` | Current state / all debates |
| `debate show <id>` | Print `summary.md` |
| `debate approve <id> [-m note]` / `debate reject <id> -m note` | Record human decision |
| `debate agents` | List configured agents, enabled state, connectivity check (dry-run ping prompt) |
| `debate serve [--port 8080]` | Serve viewer + `debates/` via stdlib `http.server` |

## Web viewer

Single static `index.html`, vanilla JS, inlined CSS, no build step. Fetches `debates/index.json` for the list; per debate fetches `state.json` + `transcript.jsonl`. Renders: round timeline, per-agent proposal cards, critique threads, vote tally, human-decision banner. Polls every few seconds while a debate is `running`.

## Error handling

- **Agent call failure** (missing CLI, non-zero exit, API error, timeout): one retry; on second failure the agent is marked `abstained` for that phase (transcript event). Debate continues if ≥ 2 agents remain active in the phase; otherwise halts with status `error` and a clear message.
- **Abstentions and consensus:** unanimity is computed over agents that actually voted; abstentions appear in the tally so the human can judge legitimacy at approval time.
- **Resume:** `state.json` is written after every phase; `run` resumes from the last completed phase.
- **Config errors:** reported per agent at load with actionable messages; a disabled or misconfigured agent never silently vanishes — `debate agents` shows why.

## Testing

- **Unit:** protocol state machine, candidate selection + consensus/vote logic (incl. abstentions and ties), store round-tripping (JSONL ↔ state ↔ Markdown), config loading/validation. All use a `MockAgent` with scripted responses — no network.
- **Integration:** full 2-round debate with three mock agents that disagree in round 1 and converge in round 2; asserts transcript contents, state transitions, and that approval is required before final stamping.
- **Adapters:** CliAgent against a stub shell script (echo/fail/timeout modes); ApiAgent against a local dummy HTTP handler.

## Dependencies

Python ≥ 3.10, PyYAML. HTTP via stdlib `urllib` (or `httpx` if it proves cleaner during implementation — either is acceptable; no provider SDKs).
