# Viewer Rendering: Event Taxonomy, Markdown, and the Hero Panel — Design Spec

Status: proposed
Date: 2026-07-15
Supersedes: nothing
Predecessor: `specs/2026-07-14-protocol-correctness-design.md` ("Deferred roadmap → Viewer")

## Purpose

Make the viewer show the debate instead of the machinery around it, and make agent markdown readable without letting agent text execute.

Scope: `debatelab/viewer/index.html` and the tests that cover it. No protocol changes, no state-shape changes, no new artifacts. `orchestrator.py`, `protocol.py`, `replay.py`, `result.py`, and `store.py` are read from and not touched; the one Python change is a test-side fixture and an assertion that `result.json` is served, which it already is.

## Motivating defects

One regression the last two cycles caused, one rendering failure, one structural gap, one class of test that cannot see any of it.

**Regression — the viewer is now majority bookkeeping, and no committed debate shows it.** `eventCard` (`viewer/index.html:107-115`) renders *every* transcript event as an identical `<details>` card wrapping `<pre>${esc(ev.content)}</pre>`. That rule was reasonable when the transcript held eight event types that all carried prose. It now holds twenty. A one-round, three-agent debate on today's code emits 43 events, so the viewer draws 43 cards:

| Type | Count | Class |
|---|---|---|
| `agent_call` | 15 | bookkeeping |
| `phase_started` / `phase_completed` | 8 | bookkeeping |
| `debate_created` / `run_config` | 2 | bookkeeping |
| `proposal` / `critique` / `revision` / `nomination` / `vote` | 15 | content |
| `candidate` / `consensus` / `nomination_dropped` | 3 | content |

**25 of 43 cards are bookkeeping, and 23 render an empty `<pre>`** — `phase_started`, `phase_completed`, and successful `agent_call` events all carry `"content": ""` (`orchestrator.py:123`, `:129`, `:184-186`). The ratio worsens per round.

Neither cycle that caused this was wrong. `2026-07-15-agent-reliability-design.md` added `agent_call` telemetry to answer "why did this agent drop out"; `2026-07-15-transcript-replay-design.md` added `debate_created`, `run_config`, and the `phase_started`/`phase_completed` boundaries because replay could not be total without them. Each was correct and each silently degraded the viewer, because the viewer's rule enumerates nothing and therefore adapts to nothing. **It went unnoticed because all four committed debates predate both cycles** — the repo's only worked examples are the ones that cannot exhibit the defect. The taxonomy in §1 exists so that cycle six does not do this a third time.

**Rendering — agents write markdown and the viewer shows the asterisks.** `viewer/index.html:113` escapes the content and drops it in a monospace `<pre>`. Every proposal, critique, and revision arrives as markdown — headings, lists, fenced code — and is displayed as its own source. The predecessor spec called this "the highest readability-per-line change available" and it remains so.

**Structural — the answer is below the process, and only by accident.** The viewer renders the candidate (`:151-154`) after the header and votes, then every round in order. `result.json` and `final.md` shipped last cycle precisely so a reader could get the answer without scrolling the audit trail, and nothing reads them. `2026-07-15-result-presentation-design.md` said so directly: the hero panel is "the natural consumer of `result.json`, and the reason this cycle precedes the viewer bucket."

**Tests — the existing viewer tests assert source text, not behavior.** `tests/test_serve.py:64-89` greps `index.html` for its own implementation:

```python
assert "function schedulePoll(id, generation)" in source
assert "let routeGeneration = 0;" in source
```

These pass if the function exists and is broken, and fail on a rename that changes nothing. For the four behaviors they cover that is merely weak. For sanitization it is disqualifying: `assert "esc(" in source` is a string match standing in for a security property, which is the substitution this codebase spent seven tasks removing from `parse_vote` and refused to reintroduce twice since. A renderer that turns agent text into markup must be tested by running it.

## Design constraint: one file, no dependencies, no CDN

`make_server` (`cli.py:298-319`) special-cases `/` and `/index.html` and roots everything else at the **debates directory**. A split-out `viewer/md.js` would resolve against `debates/md.js` and 404; serving it needs a new route and a `package-data` entry (`pyproject.toml` ships `viewer/*.html`). The viewer is also offline-only by construction — served from `127.0.0.1` with no network egress.

So the renderer lives inside `index.html`, and it is written rather than imported. This is a real cost: an intentional markdown subset, not CommonMark. It is accepted because the alternative is committing ~100KB of minified third-party JS into a project whose stated dependency budget is stdlib plus PyYAML, and inheriting CVE tracking for two JS libraries to render four local debates.

## Design

### 1. Event taxonomy

The viewer classifies each event into one of three roles. The rule is a lookup with a default, not an enumeration of everything.

| Class | Types | Rendering |
|---|---|---|
| Structure | `debate_created`, `run_config`, `roster_changed`, `phase_started`, `phase_completed` | Never a card. Becomes the layout itself. |
| Content | `proposal`, `critique`, `revision`, `nomination`, `vote`, `abstained`, `candidate`, `consensus`, `no_consensus`, `error`, `human_decision`, `fallback_candidate`, `nomination_dropped`, `nomination_retry` | A card, body rendered as markdown. |
| Telemetry | `agent_call` | An annotation on a content card (§5). Never a card. |

Structure events are not discarded — they are promoted. `phase_started`/`phase_completed` delimit the phase sections of §4; `run_config` and `roster_changed` render as a metadata line in the debate header.

**An unrecognized type renders as a content card.** This is the load-bearing half of the rule. `2026-07-15-result-presentation-design.md` rejected raising on unknown event types for a scan that models five events by name; the viewer needs the same default for a stronger reason. A new event type must degrade to a card, never blank the reading view — the failure this spec exists to repair was caused by two cycles adding event types that the viewer had no rule for.

### 2. `renderMarkdown(text) -> html`

Escape first, then apply rules to the escaped string:

```js
function renderMarkdown(text) {
  let s = esc(text ?? "");   // agent <script> is &lt;script&gt; before any rule runs
  // ... fence extraction, block rules, inline rules over s
}
```

The ordering *is* the sanitizer. Because `esc` (`viewer/index.html:60-64`) runs before any rule inserts markup, the only tags in the output are ones `renderMarkdown` itself emitted. There is no cleanup pass to get wrong and no allowlist of agent-authored tags to maintain, because agent-authored tags do not survive to be considered. Raw HTML in agent text is therefore displayed, never interpreted — a deliberate subset decision, not an omission.

Subset: ATX headings, fenced code, inline code, bold, italic, links, ordered and unordered lists, blockquotes, paragraphs. Not: tables, reference links, setext headings, raw HTML passthrough.

Fenced blocks are extracted to placeholders before inline rules run and reinstated after, so `*` and `_` inside code stay literal.

**Links carry a scheme allowlist — `http`, `https`, `mailto`.** This is the one hole escape-first does not close: `[click](javascript:alert(1))` builds an `href` the renderer emits itself, so escaping the input cannot help. A link whose scheme is not allowlisted renders as plain text rather than a link.

Every rendered body goes through `renderMarkdown`, including system-authored strings. Classifying by authorship — agent prose as markdown, system strings plain — was considered and rejected: it adds a rule that buys nothing, since markdown passes plain text through unchanged and escape-first makes both safe by construction. It is also required rather than merely harmless: `render_final` emits markdown for its own provenance line (`result.py:112-121`), so the hero cannot render as plain text.

### 3. Hero panel

`showDebate` fetches `/${id}/result.json` alongside `state.json` and `transcript.jsonl`.

| `result.status` | Hero |
|---|---|
| `approved` | The answer, rendered as markdown, plus provenance: agent, round, tally, `decided_at` |
| anything else | The outcome facts: status, `reason`, tally (`accepts`/`rejects`/`abstains` of `roster_size`, `required`), `round`, `failed_phase` |
| `result.json` absent (404) | No hero; render as today |

**The hero renders prose only when a human approved it.** `2026-07-15-result-presentation-design.md` kept `candidate.text` out of `result.json` so that unapproved prose is never one field access from being read as an answer, and rejected a `final.md` that shows the candidate under a status banner because that "puts unapproved text under a heading called 'final'". A hero panel is that argument's strongest case, not its exception: it is the largest element on the page and the one that survives a screenshot without its banner. Unapproved candidate text keeps today's honest heading — "Candidate answer (from X)" (`viewer/index.html:151-154`) — below the hero.

**The transcript collapses beneath the hero only when `status` is `approved`.** While a debate is running, awaiting review, halted, or short of quorum, the transcript is what the reader came for: a reviewer at `awaiting_human` is deciding *because of* the process, and a halt is only legible from it. Collapsing by outcome rather than always is the difference between a hero panel and a hidden transcript.

**The hero reads `result.json` rather than deriving from `state.json`.** `build_result` (`result.py:4`) is the projection that already exists, and `state.json` cannot express an outcome — the constraint the result spec established at length. Reimplementing that fold in JavaScript would put a second source of truth in a second language.

**Legacy debates 404 and that is the whole compatibility path.** `result.json` is written at run end (`orchestrator.py:163-169`) and on approve/reject (`cli.py:136-137`); the four committed debates predate it. They lose the hero and keep everything else — markdown rendering, phase grouping, and telemetry annotation all read the transcript, so the repo's only real examples still improve.

### 4. Phase grouping

Rounds group as today (`viewer/index.html:165-169`); phases group within them.

Phase order comes from the `phase_started` events in the round, which is chronological by construction and therefore resume-safe — a resumed round re-runs a phase and appends it again in the order it actually ran. Legacy transcripts have no `phase_started`; they fall back to first-seen order of each event's `phase` field, which is equivalent for a debate that never resumed.

**A phase with a `phase_started` and no `phase_completed` renders as halted.** The replay cycle added that boundary because "a completed phase is indistinguishable from a halted one" in the transcript, and paid for it there; the viewer gets the answer to "where did it stop" for the cost of a lookup. `20260714-how-can-this-repository-be-improved-furt-2` is the shape — `propose` raised `DebateHalted` and never completed.

### 5. Telemetry annotation

`agent_call` events attach to the content card they produced.

**Pairing cannot key on `(round, phase, agent)`.** The `vote` phase runs two fanouts — nominate and vote — under one phase name, so that key holds two calls per agent per round, not one. The rule is ordering-based instead:

> Accumulate `agent_call` events per `(round, phase, agent)`. On the next content event with that key, flush the accumulator into it.

This needs no knowledge of `task` or of which fanout is which. It holds because `_fanout` (`orchestrator.py:217-247`) appends every call before the phase function's content-emit loop runs, and because concurrency cannot reorder a single agent's events relative to each other — each is appended by that agent's own thread, in order. In the `vote` phase the sequence per agent is therefore `agent_call → nomination → agent_call → vote`, and each call flushes into the event it produced. Re-ask calls (`orchestrator.py:195-215`) append before that agent's terminal content event either way.

| Attempts | Annotation |
|---|---|
| One, ok | `1.4s` |
| More than one | `3 attempts · 4.2s · rate_limit` |

This is what makes 15 of 43 cards' worth of data useful rather than noisy: "why did claude abstain" becomes readable in the viewer, which is what the reliability cycle recorded `kind` for.

**Unflushed calls render as an unfinished-call note in the phase group.** When a phase halts, `_fanout` raises at `orchestrator.py:246` *before* the phase function emits any content, so the agents that succeeded have calls with no content event to attach to. Dropping them would erase the evidence of the only phase that matters on a halted debate.

## Testing

**Node, behavioral, `skipif`.** pytest extracts the `<script>` block from `index.html`, runs it under `node` via `subprocess`, and asserts what the renderer does:

- `<script>alert(1)</script>` in agent text renders inert — the headline regression.
- `[x](javascript:alert(1))` renders as text, not a link; `http`/`https`/`mailto` render as links.
- Markdown constructs render: headings, bold, italic, lists, blockquotes, fenced and inline code.
- Inline rules do not fire inside fenced code.
- `renderMarkdown(null)` and `renderMarkdown("")` return empty, not `"null"`.

Marked `skipif(shutil.which("node") is None)` — the project stays pure Python for anyone who only runs the CLI. The cost is real and stated: on a machine without node, the sanitization test does not run. It is accepted because requiring a JS runtime of every contributor to a stdlib-only Python project is a larger cost than a skip, and because the alternative on offer — grepping the source for `esc(` — does not test the property at any cost.

**Taxonomy, in node against real event fixtures:**

- A 43-event single-round transcript yields 18 content cards, not 43.
- No `phase_started`, `phase_completed`, `agent_call`, or `run_config` renders as a card.
- An unknown type (`{"type": "future_event", "content": "x"}`) renders as a card rather than throwing or vanishing.
- `agent_call` pairs to the right card across the `vote` phase's two fanouts.
- Calls with no content event render as an unfinished-call note.

**Hero, in node:** `approved` renders the answer; `awaiting_human` renders outcome facts and no answer prose in the hero; a missing `result.json` renders the legacy view with grouping intact; the transcript is collapsed only when approved.

**Python:** `test_serve.py` gains a case that `/${id}/result.json` is served and that a debate without one 404s cleanly. The four source-grep tests (`:64-89`) are replaced by node behavioral equivalents where they overlap; `test_files_outside_debates_are_not_served` stays as-is, being a real behavioral test of the server.

## Deferred

- **Revision diffs** — the fourth item in the predecessor's Viewer bucket. Independent of the other three, needs a diff implementation in client-side JS plus a pairing rule for proposal→revision across rounds, and reads better once phase grouping has settled what a round looks like.
- **Richer `debate status`** and **`summary.md`'s "Debate process" divider** — still parked in the result spec's Deferred.
- Context budgeting, prompt anonymization, the synthesis phase, the remaining locks (`index.json`, `approve`/`reject`), `run()` resuming from replay, and CLI polish — all still parked in the predecessor's deferred roadmap.

## Explicitly rejected

- **Vendoring marked.js + DOMPurify** — correct markdown and a real sanitizer, for ~100KB of unauditable third-party JS in a stdlib-plus-PyYAML project, plus CVE tracking for two libraries, to render four local debates.
- **Rendering markdown to HTML in Python** — no stdlib markdown library, so the renderer gets hand-written regardless; it would add a third derived artifact beside `summary.md` and `final.md`, and the viewer would inject it with `innerHTML`, separating the risk from the code responsible for it.
- **Splitting the renderer into `viewer/md.js`** — unreachable through `make_server`'s static root without a new route and a `package-data` change, for a file that is ~80 lines.
- **Sanitizing after rendering** — a cleanup pass over emitted markup, which is the class of design that has holes. Escape-first has no cleanup pass to get wrong.
- **Deriving the hero from `state.json`** — `build_result` in JavaScript, a second source of truth in a second language, against a constraint the result spec already established.
- **Candidate text in the hero under a banner** — re-litigates the result spec's rejected option in the medium where it fails worst: the largest element on the page, and the one a screenshot keeps after the banner scrolls away.
- **Always collapsing the transcript** — optimizes for the approved case and hides the process from the reviewer whose job is to judge it.
- **Raising on unknown event types** — the failure this spec repairs, converted into a crash.
- **Grepping `index.html` for `esc(`** — a string match standing in for a security property. See "Motivating defects".
- **Requiring node rather than skipping** — imposes a JS runtime on every contributor and on CI for a Python CLI whose viewer is optional.
