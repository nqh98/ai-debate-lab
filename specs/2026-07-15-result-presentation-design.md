# Result Presentation: `result.json`, `final.md`, and Exit Codes — Design Spec

Status: proposed
Date: 2026-07-15
Supersedes: part of `2026-07-15-transcript-replay-design.md` (see "Superseded decisions")
Predecessor: `specs/2026-07-14-protocol-correctness-design.md` ("Deferred roadmap → Result presentation")

## Purpose

Give the answer this tool exists to produce a place to be read, and make "was there an answer" a question a script can ask without parsing prose.

Scope: two derived artifacts (`result.json`, `final.md`), the command that prints them, the two event fields they need, and exit codes for `run` and `result`. No protocol changes, no new phases, no state-shape changes. `protocol.py`, `replay.py`, and `render_summary` are not touched.

## Motivating defects

One structural gap, two unrecorded facts, one silent lie.

**Structural — the answer has nowhere to be read.** `render_summary` (`store.py:291-333`) emits one document that interleaves the candidate with the votes, every current proposal, and every critique. It is the only rendered view. A human who wants the answer scrolls past the process; a script that wants the answer has no option at all. The tool's entire output is buried in its own audit trail.

**Unrecorded — the failing tally.** The `consensus` event carries the full `protocol.tally()` (`orchestrator.py:128-130`). Its counterpart does not: `no_consensus` carries the prose string `"no consensus reached within the configured round limit"` and nothing else (`:89-96`). The one outcome where a human most needs to see *how close* the roster came records no numbers at all.

**Unrecorded — the halting phase.** The `error` event is appended with `"phase": "end"` (`:137-140`), which is a literal, not the phase that failed. The failing phase exists only inside the message `DebateHalted` raised: `"only 1 agent(s) responded in phase 'critique' — need at least 2"` (`:223-226`). Rendering "halted during critique" from that means regexing a message string — the prose-guessing that `2026-07-14-protocol-correctness-design.md` spent seven tasks removing from `parse_vote`, and that `2026-07-15-agent-reliability-design.md` §3 refused to reintroduce one layer down.

**Silent lie — `run` reports failure and exits 0.** `cmd_run` prints `f"final status: {status}"` and returns (`cli.py:80`). A halted debate, a debate that never reached consensus, and a debate awaiting review are indistinguishable to `&&`, to `set -e`, and to CI. `debate fsck` already established the opposite convention in this same CLI — 0 ok, 1 diverged, 3 unverifiable — so the inconsistency is the tell, exactly as it was for the non-atomic writes in the predecessor spec.

## Design constraint: `state.json` cannot describe an outcome

The obvious implementation is `render_final(state)`, mirroring `render_summary(state)`. It cannot work, and the reason generalizes.

`state.json`'s fourteen keys hold the *current position* of a debate, not the *history* of how it ended. Against the outcome facts:

| Fact the result needs | In `state.json`? | Where it actually lives |
|---|---|---|
| Candidate text and agent | yes — `{"agent", "text"}` (`orchestrator.py:331`) | also the `consensus` event |
| The winning tally | **no** | `consensus` event, `tally` key |
| The round consensus was reached | **no** — `round` is the *last* round | `consensus` event, `round` key |
| Approval timestamp | **no** — `human_decision` is `{"decision", "note"}` (`cli.py:206-209`) | `human_decision` event, `ts` key |
| Why a debate halted | **no** — only `status: "error"` | `error` event, `content` key |

**`replay(events)` does not help either**, which is the more surprising half. Its contract is to reproduce `state.json`'s exact fourteen keys — that is what makes `fsck`'s whole-dict comparison meaningful — so it necessarily omits every fact the checkpoint omits. Deriving the result from replay would mean scanning the events anyway, on top of inheriting `MissingGenesis` and reporting nothing for the four legacy debates.

The resolution is that **the result needs no fold.** Every fact above already sits on a terminal event that carries its own complete payload. `build_result` is a *scan* for at most five events, not an accumulation over all of them. It duplicates nothing in `replay.py`, and the "duplication is the feature" argument of the replay spec does not apply here, because there is no second implementation of anything.

## Design

### 1. Two additive event fields (`orchestrator.py`)

Record the two facts where they are known, rather than inferring them later.

```json
{"type": "no_consensus", "round": 5, "phase": "end", "agent": null,
 "content": "no consensus reached within the configured round limit",
 "tally": {"accepts": 1, "rejects": 1, "abstains": 1, "roster_size": 3, "required": 2}}

{"type": "error", "round": 2, "phase": "end", "agent": null,
 "content": "only 1 agent(s) responded in phase 'critique' — need at least 2",
 "failed_phase": "critique"}
```

`tally` is computed at the break from facts already in hand: `protocol.tally(state["votes"], len(state["roster"]), Fraction(state["quorum"]))`. It is recomputed there rather than reusing the loop's `roster_size`/`quorum_frac` (`:117-118`), which are bound inside the phase loop and would be read at the `no_consensus` break only by leaked loop scope — correct today by accident, and the kind of accident that survives review.

`failed_phase` is a **new key**; `phase` stays `"end"`. Changing `phase` in place would alter the meaning of an event type in every existing transcript, and `2026-07-15-transcript-replay-design.md` §2 explicitly reasons about `phase: "end"` events when discussing round recovery. Additive is free; a shape change is not.

**Neither field can perturb `replay` or `fsck`.** Both types fold through `_status_setter` (`replay.py:118-121`), which reads only the type and writes only `status`. Extra keys are invisible to it, and `fsck` compares replayed state against `state.json`, which gains nothing. This is checked by test, not asserted by argument.

### 2. `debatelab/result.py` (new)

A pure module: no files, no network, no clock, no store. Same contract as `protocol.py`, `retry.py`, and `replay.py`, enforced by the same AST import check — `store`, `orchestrator`, `prompts`, `cli` are banned imports.

**`replay` is a banned import too**, despite being pure. Not because importing it would break anything, but because it would be the first step toward `build_result` growing a fold. The result is a scan; the ban makes that structural rather than a matter of discipline.

```python
def build_result(
    events: list, *, id_fallback: str | None = None, title_fallback: str | None = None
) -> dict: ...

def render_final(result: dict) -> str: ...
```

The two fallbacks are how a legacy transcript with no `debate_created` still gets an `id` and a `title` (§3) — both facts live only on that event. The caller has both already (the debate id it was invoked with, and `state["title"]`) and passes them in; `build_result` never touches a file.

The scan, in one pass:

| Event | Contributes |
|---|---|
| `debate_created` | `id`, `title` — optional; absent on legacy transcripts, where both come from the caller's fallbacks |
| `consensus` (last) | `candidate.agent`, `candidate.round`, `tally`, and the candidate text — promoted to `answer` only if a `human_decision` approved it (§3) |
| `no_consensus` | `reason`, `tally`, `round` |
| `error` | `reason`, `failed_phase`, `round` |
| `human_decision` (last) | `status`, `decided_at` (its `ts`), `note` |

Every other event type is ignored. Unlike `replay` (§6 there), **unknown types do not raise**: replay must model every event that touches state or `fsck` decays into a tautology, whereas the result models five events by name and is indifferent to the other seventeen by construction. A new `agent_call`-style telemetry event must never break `debate result`.

"Last" rather than "first" for `consensus` and `human_decision`: a debate that was rejected and re-run reaches consensus twice, and the later one is the live outcome.

### 3. The result document

```json
{"id": "20260715-...", "title": "Which caching strategy?",
 "status": "approved",
 "answer": "Use a write-through cache with...",
 "candidate": {"agent": "claude", "round": 3},
 "tally": {"accepts": 3, "rejects": 0, "abstains": 0, "roster_size": 3, "required": 2},
 "decided_at": "2026-07-15T10:12:03+00:00", "note": "",
 "reason": null}
```

`status` is `state.json`'s vocabulary unchanged: `created`, `running`, `awaiting_human`, `approved`, `rejected`, `no_consensus`, `error`.

**`answer` is the only field that may contain answer text, and it is `null` unless a human approved.** This is the invariant the whole cycle turns on. `candidate` carries provenance — the agent and the round — and deliberately **not** the candidate's text: a `candidate.text` on an `awaiting_human` debate is unapproved prose sitting one careless field access away from being treated as a verdict, which is `parse_vote` manufacturing consensus wearing a new hat. Consumers that want to *display* a candidate for review read `state.json` or `summary.md`, which are the views that already exist for exactly that.

`reason` is the machine-readable explanation of why `answer` is null, and is itself null when `answer` is not:

| status | `reason` |
|---|---|
| `approved` | `null` |
| `rejected` | the human's note, or `"rejected without a note"` |
| `awaiting_human` | `"awaiting human review"` |
| `no_consensus` | the `no_consensus` event's `content` |
| `error` | the `error` event's `content` |
| `created` / `running` | `"debate has not produced a candidate yet"` |

`id` and `title` come from `debate_created`, falling back to the caller-supplied values (§2) on legacy transcripts that predate genesis events. Both are `null` if a caller supplies neither, which only a direct unit-test call can produce.

### 4. `final.md`

Two headings, ever: `# Answer` and `# No answer`. The heading is a function of `result["answer"] is None` and nothing else.

```markdown
# Answer

Use a write-through cache with a 60s TTL...

---
Approved 2026-07-15T10:12:03+00:00 · from **claude**, round 3 · 3 accept / 0 reject / 0 abstain
```

```markdown
# No answer

Candidate from **claude** (round 3) was **rejected** on 2026-07-15T10:12:03+00:00:

> misses the cold-start case

The full debate is in `summary.md`.
```

```markdown
# No answer

Round cap 5 reached without a quorum: 1 accept / 1 reject / 1 abstain of 3 (2 required).

The full debate is in `summary.md`.
```

```markdown
# No answer

Halted in round 2 during **critique**: only 1 agent(s) responded in phase 'critique' — need at least 2.

The full debate is in `summary.md`.
```

The provenance line is below the answer, not above it. The answer is what the reader came for; the audit trail earns its place underneath.

### 5. Write sites

`result.json` and `final.md` are written wherever `summary.md` is written, and nowhere else — three sites, all of which already call `write_summary`:

- `Orchestrator._checkpoint` (`orchestrator.py:145-147`)
- `cmd_decide`'s reconciliation path (`cli.py:212`)
- `cmd_decide`'s normal path (`cli.py:235`)

They are **fully regenerated every time, and read back by no code** — `summary.md`'s existing contract exactly, which is why this adds no new category to the file layout:

```
transcript.jsonl   source of truth
state.json         checkpoint (cache)
summary.md         derived view
result.json        derived view   <- new
final.md           derived view   <- new
```

All three sites hold `state`, not `events`, so each gains a `store.read_events(id)` call. **This re-reads the whole transcript at every phase boundary** — roughly twenty reads of a small local file across a five-round debate, growing linearly with the transcript. Stated here rather than discovered later: it is accepted because the file is local, small, and already fully re-read by `fsck` and `cmd_decide`, and because the alternative — threading an event list down through `run()` — buys micro-optimization at the cost of a parameter that exists only for performance on a tool whose phases each block on multiple LLM calls.

`store.py` gains `write_result` and `write_final` next to `write_summary`, both routed through the existing `_atomic_write`. The viewer polls this directory; a torn `result.json` is the failure `_atomic_write` exists to prevent.

### 6. `debate result <id>`

Prints `final.md`; `--json` prints `result.json`. Both are computed fresh rather than read off disk, so the command is correct on a debate whose files predate this spec.

### 7. Exit codes

The exit code says exactly what `final.md`'s heading says.

| Command | Code | Meaning |
|---|---|---|
| `run` | 0 | `awaiting_human` — consensus reached |
| `run` | 1 | `no_consensus` — ran correctly, agents disagreed |
| `run` | 3 | `error` — halted |
| `run` | 1 | `LockError` (existing `sys.exit(str(e))` behavior, unchanged) |
| `result` | 0 | `approved` — `# Answer` |
| `result` | 1 | everything else — `# No answer` |
| both | 2 | argparse usage errors |

This matches `fsck`'s existing 0/1/3 and `grep`'s convention, where 1 means "nothing found" rather than "I broke".

**The cost, stated plainly:** under `set -e`, a legitimate `no_consensus` now aborts a script that previously continued. That is the intended signal — a debate that never agreed should not silently feed a deploy — and `|| true` is the documented escape. It is a behavior change to a command that has always exited 0, and it belongs in the README.

### 8. Compatibility

- **Legacy debates work.** `debate_created` is the only optional input and `title` has a fallback, so all four committed debates produce a real result rather than `fsck`'s `unverifiable`. This is the payoff of scanning terminal events instead of folding: the result needs no genesis.
- Pre-existing transcripts lack `tally` on `no_consensus` and `failed_phase` on `error`. `build_result` treats both as absent, and `render_final` omits the tally sentence and says "halted in round N" without a phase. Degraded, honest, and never inferred.
- Debates that have never been re-run since this spec have no `result.json`/`final.md` on disk until their next checkpoint or decision. `debate result` computes fresh and is unaffected.
- No event changes shape, no state key is added or removed, `render_summary` is untouched, and `replay`/`fsck` behavior is unchanged.

## Testing

Unit tests over hand-written event lists — `build_result` is pure, so every outcome is a table test with no orchestrator and no filesystem:

- **The invariant:** an `awaiting_human` result has `answer is None` and no candidate text anywhere in the document; the same for `rejected`, `no_consensus`, `error`, `created`, and `running`. `# Answer` appears in `render_final` **only** for `approved`. This is the headline regression and the reason the cycle exists.
- **Each status:** every row of the `reason` table; `rejected` surfaces the human's note; a rejection with no note reads `"rejected without a note"`.
- **Provenance:** `candidate.round` is consensus's round, not the debate's last round (they differ whenever a debate is re-run); `decided_at` is the `human_decision` event's `ts`.
- **Last-wins:** a debate that reached consensus, was rejected, and reached consensus again reports the second.
- **Legacy:** an event list with no `debate_created` still builds, taking `id` and `title` from the caller's fallbacks; a `no_consensus` without `tally` and an `error` without `failed_phase` render without inventing either. `debate result` against each of the four committed pre-genesis debates exits without raising — the concrete claim of §8, tested against the real fixtures rather than asserted.
- **Enrichments:** the new `no_consensus` tally sums to `roster_size`; `failed_phase` matches the phase named in the message that `DebateHalted` raised (asserting the field agrees with the prose, which is the last time anything reads that prose).
- **Replay is unperturbed:** `replay(events)` returns identical state with and without the two new keys, and the existing differential test still passes — the enrichments must be invisible to `fsck`.
- **Purity:** the AST import check, retargeted at `result.py`, banning `replay` alongside `store`/`orchestrator`/`prompts`/`cli`.

Integration, driving the real `Orchestrator` with `MockAgent`:

- A halted debate exits 3 from `cmd_run`; a `no_consensus` debate exits 1; a consensus debate exits 0.
- `debate result` on an `awaiting_human` debate exits 1 and prints `# No answer`; after `debate approve`, exits 0 and prints `# Answer` with the candidate text.
- `_checkpoint` leaves a parseable `result.json` and a `final.md` on disk, and no `.tmp` file beside either.

The suite is 207 tests in ~3s and must stay single-digit seconds. Nothing here sleeps, spawns, or touches the network.

## Superseded decisions

- **`2026-07-15-transcript-replay-design.md` → Deferred, "`result.json` / `final.md` — both must be projections of replay rather than a third source of truth."** The second half stands: they are derived views, regenerated and read back by nothing. The first half does not. Replay reproduces `state.json`'s exact fourteen keys by contract, so it omits the tally, both timestamps, and the halt reason — every fact that distinguishes a result from a checkpoint. That wording was written before anyone enumerated what the checkpoint lacks. The result is a projection of the **transcript's terminal events**, which is the stronger form of the same intent: it needs no genesis, so it works on the four legacy debates that replay must refuse.

## Deferred

- **Richer `debate status`** and **`summary.md`'s "Debate process" divider** — the rest of the roadmap's presentation bucket. Both read better once `final.md` has settled what "the answer" means, and both touch `render_summary`, which this cycle deliberately does not.
- **The viewer's final-answer hero panel** — the natural consumer of `result.json`, and the reason this cycle precedes the viewer bucket. It will want a candidate's text for `awaiting_human` review, which §3 keeps out of `result.json` on purpose; it reads `state.json` for that, as it does today.
- **`debate export`** and word-boundary title truncation — still parked in the predecessor's Polish bucket.
- **`run()` resuming from replay**, `debate rebuild`, sequence numbers / `schema_version` — still parked in the replay spec, still gated on `fsck` evidence from real post-genesis debates, of which the repo currently has none.
- Context budgeting, prompt anonymization, the synthesis phase, and the remaining locks (`index.json`, `approve`/`reject`) — all still parked in the predecessor's deferred roadmap.

## Explicitly rejected

- **`render_final(state)`** — mirrors `render_summary` and cannot express an outcome. See "Design constraint".
- **`build_result(replay(events))`** — honors the replay spec's literal wording while inheriting `MissingGenesis` and still lacking every fact that makes a result a result.
- **`candidate.text` in `result.json`** — puts unapproved prose one field access from being read as an answer. The one field that may hold answer text is `answer`, and it is null unless a human approved.
- **Parsing the failing phase out of `DebateHalted`'s message** — the `parse_vote` sin, twice-rejected in this codebase already. The fact is free at the raise site; `failed_phase` records it there.
- **Changing the `error` event's `phase` from `"end"` to the failing phase** — rewrites the meaning of an event type in every existing transcript to save one additive key.
- **`final.md` existing only when approved** — presence-as-signal makes "no answer" and "the run crashed before writing" indistinguishable.
- **`final.md` always showing the candidate under a status banner** — the most convenient option, and it puts unapproved text under a heading called "final".
- **Raising on unknown event types** (replay §6's rule) — correct for a fold that must model everything, wrong for a scan that models five events by name. A new telemetry event must not break `debate result`.
- **Threading `events` down through `run()` to avoid re-reading the transcript** — a parameter that exists only to optimize file reads on a tool whose every phase blocks on multiple LLM calls.
