# The Synthesis Phase — Design Spec

Status: proposed
Date: 2026-07-15
Supersedes: part of `2026-07-14-ai-debate-lab-design.md` (see "Superseded decisions")
Predecessor: `specs/2026-07-14-protocol-correctness-design.md` ("Deferred roadmap → Protocol features")

## Purpose

Make the debate produce an answer the debate actually built, rather than electing one agent's text and calling it the result.

Scope: the protocol's phase sequence, the split of the compound `vote` phase, one new phase and its prompt, and the candidate provenance that the three existing views need in order to describe what they are showing. No new state keys, no new artifacts, no changes to voting rules, quorum, retries, or the halt condition. `retry.py` is not touched, and `store.py` only in `render_summary`'s two candidate headings (§8).

The five preceding cycles hardened the mechanism: strict parsing, a quorum over a recorded roster, seeded selection, backoff, a verifiable replay, machine-readable results, a readable viewer. The mechanism is now trustworthy, and what it faithfully reports is that agent A's unmodified proposal won a plurality. That is the remaining gap, and it is a product gap rather than a correctness one — the first of this series.

## Motivating defects

Two structural gaps, one leak, one resume flaw.

**Structural — nothing in the protocol merges anything.** `_phase_vote` ends with `state["candidate"] = {"agent": winner, "text": proposals[winner]}` (`orchestrator.py:350`). The candidate is a *verbatim* proposal, selected by plurality. Every phase before it exists to improve proposals — `critique` gathers objections, `revise` folds each agent's own critiques back into its own text — but each agent only ever revises its own document. No artifact anywhere in the protocol combines the roster's work. A debate whose three agents each contribute one essential insight produces, as its answer, whichever single agent's essay won a vote. The other two insights survive only as critiques buried in `summary.md`.

This is what the tool exists to fix and it has never done it. The roadmap named it in one line — "winner drafts a merged answer incorporating accepted critiques" — and parked it behind five cycles of making the machinery trustworthy enough to be worth improving. That ordering was right. It is now done.

**Structural — "accepted critiques" does not exist and cannot be built.** The roadmap's phrasing presumes a mechanism this protocol does not have: critiques are never accepted or rejected. They are free prose, addressed to every proposal at once (`prompts.critique_prompt`), and the only verdict in the system is a vote on a whole candidate. There is no per-critique ledger to read. §3 resolves this by giving the synthesizer *every* critique and the previous round's reject reasons, and letting the merge decide what to incorporate — which is what "accepted" was always going to mean in practice, since nothing else could have.

**Leak — the approved answer opens with a changelog.** `revise_prompt` instructs agents to *"Start with a short 'Changes:' section stating what you changed and why (or why you changed nothing), then the full revised answer"* (`prompts.py:56-58`). Those replies become `state["proposals"]` (`orchestrator.py:289`). The winner's proposal becomes `candidate["text"]` (`:350`). `build_result` promotes `candidate_text` to `answer` unmodified (`result.py:87`). `render_final` prints it under `# Answer` (`result.py:121`).

So the answer this tool hands a human begins with the winning agent's diff notes against its own previous draft — a document written for the other agents, addressed to a conversation the reader never saw. The instruction is correct for a proposal under critique and wrong for a final answer, and nothing between the two notices the difference. `2026-07-15-result-presentation-design.md` built `final.md` to give the answer a place to be read; this is what has been sitting in it.

**Resume — the vote phase is compound, and its granularity is a phase.** `_phase_vote` (`orchestrator.py:296-390`, 95 lines, the largest function in the module) runs a nomination fanout, selects a candidate, then runs a vote fanout, all inside one `phase_started`/`phase_completed` bracket (`:121-130`). Those brackets are the resume granularity: `next_phase` restarts at whole phases. A halt in the vote fanout therefore re-runs every nomination on resume.

Today that wastes FAST calls and nobody notices. It stops being free the moment an expensive step joins that bracket, which is exactly what this cycle would do if synthesis went inside it. The defect is pre-existing; this spec is what makes it expensive, so this spec fixes it.

## Design constraint: the synthesizer's reply cannot be validated

Every agent reply in this protocol is one of two kinds. Either it carries a marker that a parser checks — `VOTE: accept`, `NOMINATE: <name>` — and an unparseable reply can be re-asked with a strict suffix and then counted as an abstention (`orchestrator.py:195-215`). Or it is free prose that only ever reaches a human — proposals, critiques, revisions — where there is nothing to validate because nothing downstream branches on its content.

**Synthesis is the first reply that is both free prose and load-bearing.** It becomes `candidate["text"]`, which the roster votes on and which `final.md` prints as the answer. There is no marker to require, so `prompts.reask` has nothing to demand and `parse_*` has nothing to match. The only two checks available are *did the call return* and *is the result non-empty*.

This is why §5's fallback is not defensive padding but the core of the design. A protocol step that cannot validate its output must have a defined behavior for producing none, and that behavior must not be "use it anyway". The alternative — a blank or errored synthesis silently becoming the candidate — is `parse_vote` reading *"I cannot accept this"* as **accept**, one layer up: a verdict no agent gave, reaching a human as a real one. That is the defect this entire spec series opened on, and it does not get to come back in a new phase.

## Design

### 1. Phase sequence (`protocol.py`)

```python
PHASES = ("propose", "critique", "revise", "nominate", "synthesize", "vote")
```

**`next_phase`'s body does not change.** Its three rules already generalize over the tuple:

```python
if round_num == 0:            return 1, "propose"
if last_completed == "vote":  return round_num + 1, "critique"
return round_num, PHASES[PHASES.index(last_completed) + 1]
```

Walked against the new constant, `revise → nominate → synthesize → vote → (next round) critique` falls out unchanged. `vote` remains the round terminator, so `propose` still runs exactly once per debate — the invariant `replay._proposal` documents at `replay.py:90-93` and depends on.

The one-line constant is the entire protocol change. That is the return on `protocol.py` having been kept pure: no files, no clock, no debate ids, 78 lines. A phase sequence that lives in one tuple is a phase sequence that can be extended in one tuple.

Round 1 runs six phases; every later round runs five.

### 2. Orchestrator: `_phase_vote` splits three ways

Each new function is exactly one fanout or one call:

| Function | Task | Work |
|---|---|---|
| `_phase_nominate` | FAST fanout | nominations, re-asks, `nomination_dropped`, `select_candidate`, `fallback_candidate`, `candidate` |
| `_phase_synthesize` | DEEP, one call | the merge; `synthesis` or `synthesis_failed` |
| `_phase_vote` | FAST fanout | votes, re-asks, abstentions |

`_phase_nominate` sets `candidate = {"agent": winner, "text": proposals[winner], "synthesized": False}` — the pre-synthesis candidate, which is also the fallback (§5).

`_phase_synthesize` on success emits `synthesis`, sets `candidate["text"]` to the merged draft and `candidate["synthesized"] = True`, **and assigns `proposals[winner] = synthesis`** (§4, the carry-forward). On failure it emits `synthesis_failed` and leaves the candidate exactly as `_phase_nominate` left it.

Two supporting changes:

- The candidate reset at `orchestrator.py:113-114` moves from `phase == "vote"` to `phase == "nominate"`. That reset exists so a re-run round cannot vote against a stale candidate; `nominate` is now where a candidate comes into being.
- Synthesis needs a single-call sibling to `_fanout`, `_ask_one`. `_fanout` raises `DebateHalted` under two responders (`:245-246`), a rule that is meaningless for a one-agent call — it would halt every synthesis. `_ask_one` reuses `retry.call_with_retry` and `_record_call`, so synthesis inherits the same backoff and the same per-attempt `agent_call` telemetry as every other call in the system. It is structurally `_reask` (`:195-215`) without the parse.

### 3. The synthesis prompt (`prompts.py`)

```python
def synthesize_prompt(
    name: str,
    problem: str,
    proposals: dict[str, str],
    critiques: dict[str, str],
    reject_reasons: dict[str, str] | None = None,
) -> str:
```

**What makes it not `revise_prompt`.** `revise_prompt` hands one agent *its own* proposal and *all* critiques, and asks it to defend and improve its own document (`prompts.py:45-59`). `synthesize_prompt` hands the winner *every* proposal and *all* critiques and asks for one merged answer. Per-agent defense versus one-agent merge: that difference is the phase's whole reason to exist, and the prompt has to state it explicitly or the winner will simply re-emit its own proposal, which is the status quo at DEEP cost.

`reject_reasons` carries the previous round's rejections, as `critique_prompt` already does (`orchestrator.py:263-267`). This is what makes §4 coherent.

**The output contract is the answer alone**: no `Changes:` preamble, no commentary on what was merged or why, no meta-discussion of the debate. The reply is published verbatim as the answer.

There is no marker and therefore no parser, no `reask`, and no `_reask` call site. Per the design constraint, the two available checks are the call succeeding and the reply being non-empty. **A reply that is empty or whitespace-only is a failure** and takes §5's path.

`revise_prompt` is deliberately not touched. Its `Changes:` section is written for the agents critiquing that proposal next round, and it does its job there.

### 4. Carry-forward: the synthesis becomes the winner's proposal

`_phase_synthesize` assigns `state["proposals"][winner] = synthesis`.

Without this, a rejected synthesis is discarded and round N+1 critiques the original proposals. That is incoherent in a way the code makes concrete: `_phase_critique` feeds the previous round's reject reasons into the next critique prompt (`orchestrator.py:263-273`). Those reasons describe the synthesis. If the synthesis is gone, every agent is handed *"here is why the answer was rejected"* alongside a set of proposals that are **not** the rejected answer and that nobody objected to. The reject reasons already sit slightly askew today, since they describe one proposal while the critique covers all of them; a discarded synthesis makes the rejected document guaranteed absent, which converts a rough edge into a contradiction.

With the carry-forward, round N+1's critique targets exactly the document the roster rejected, the reject reasons describe something on the table, and each round's merge absorbs another round of objections. The debate converges instead of restarting. It also means the most expensive artifact in the protocol is never thrown away.

```
round 1  proposals {A: a1, B: b1, C: c1}
         nominate   → A wins
         synthesize → S1  (merges a1, b1, c1)
         proposals {A: S1, B: b1, C: c1}
         vote on S1 → reject

round 2  critique   → targets S1, b1, c1   ← the rejected document
         revise     → A revises S1
         nominate   → B wins
         synthesize → S2
         vote on S2
```

**Entrenchment is the acknowledged cost.** The winner's proposal is now a merged document incorporating everyone's contributions, so it is plausibly the strongest text on the table and plausibly wins again. That is convergence working as intended rather than a bug, and two existing rules bound it: the winner may never nominate its own proposal (`prompts.py:73`, enforced at `orchestrator.py:329-335`), and ties resolve by seeded draw rather than config order (`protocol.select_candidate`), so a repeat win requires the roster to keep choosing it.

**A note the next cycle needs:** the carried-forward synthesis re-enters `revise` in round N+1, and `revise_prompt` asks for a `Changes:` preamble. So `proposals[winner]` in round 2 is a synthesis wearing a changelog. It is invisible on the normal path — round 2's synthesis produces a clean answer regardless — and visible only on §5's fallback. §9 records it.

### 5. Failure: fall back to the verbatim proposal

`_ask_one` returns `None` on `AgentError` after retries; an empty or whitespace-only reply is treated identically. In either case `_phase_synthesize` emits:

```
{"type": "synthesis_failed", "phase": "synthesize", "agent": winner,
 "content": <error string or "empty synthesis">, "reason": "agent_error" | "empty"}
```

and returns, leaving `candidate = {"agent": winner, "text": proposals[winner], "synthesized": False}` and `proposals` untouched. The vote proceeds.

**The degraded path is the protocol that shipped in cycles one through five.** The roster votes on a verbatim proposal, exactly as it has always done. This is the property that makes the fallback safe to rely on: it is not a new untested branch invented to absorb an error, it is five cycles of shipped behavior, still covered by every existing test.

Halting instead was rejected (see "Explicitly rejected"): it would discard a complete round of proposals, critiques, revisions, and nominations over one agent's transient failure, and the two-responder rule it would borrow exists because a debate needs multiple voices — which is not the situation a single synthesizer failing describes.

**The synthesize phase can never halt a debate.** `_ask_one` catches `AgentError`, so the halt surface is unchanged: fanout phases only, under two responders. `failed_phase` can never read `"synthesize"`, and `render_final`'s "Halted in round N during **X**" (`result.py:145-149`) can never name it.

### 6. State shape

One key changes. None is added.

```python
candidate = {"agent": str, "text": str, "synthesized": bool}   # was {"agent", "text"}
```

The carry-forward reuses `proposals`, so it needs no slot of its own — which is what keeps this cycle from touching `_initial()`'s key set (`replay.py:44-60`), `fsck`'s whole-dict comparison, and the compatibility surface generally.

`synthesized` must exist because it is the one fact that distinguishes a merged answer from a fallback, and both `final.md` and the viewer's hero panel describe the candidate's provenance to a human. Without it they would claim a synthesis that did not happen — a smaller sibling of the manufactured consensus the predecessor spec was written to eliminate. It is a fact known for free at the point of production; recording it there is the same discipline that put `failed_phase` on the raise site rather than regexing it out of an exception message (`2026-07-15-result-presentation-design.md`).

New events:

| Event | Phase | When | Folds |
|---|---|---|---|
| `synthesis` | `synthesize` | The merge succeeded; content is the merged answer | `candidate.text`, `candidate.synthesized`, `proposals[agent]` |
| `synthesis_failed` | `synthesize` | Call errored or returned empty | audit-only |

The `consensus` event gains a `synthesized` field (§8).

### 7. `replay.py`

The highest-risk file in the cycle, and the best-defended. `replay.py` is a deliberate independent reimplementation of the orchestrator's state updates, not a shared fold (`replay.py:8-15`), and `tests/test_replay_differential.py` is the differential test between them. A mistake here is caught by the suite rather than shipped.

| Change | Detail |
|---|---|
| `_phase_started` | candidate reset keys on `"nominate"`, not `"vote"` (`replay.py:81-82`) |
| `_candidate` | adds `"synthesized": False` (`:101-102`) |
| `_synthesis` (new) | `candidate["text"] = e["content"]`, `candidate["synthesized"] = True`, `proposals[e["agent"]] = e["content"]` |
| `_FOLD` | gains `"synthesis": _synthesis` |
| `AUDIT_ONLY` | gains `"synthesis_failed"` |

`synthesis_failed` is genuinely audit-only under `AUDIT_ONLY`'s stated rule — "real events that carry real information and change no `state.json` key" (`replay.py:29-30`). It changes nothing because `_phase_nominate` already established `synthesized: False`; the event records *why* that value stands, which is information a human needs and the checkpoint does not hold.

**No staging.** The `attempt` machinery (`replay.py:211-252`) exists so that a re-run phase *replaces* accumulated dict state rather than merging it — `critiques` on `phase_completed` of `critique`, `votes` on `phase_completed` of `vote`. Synthesis writes one candidate and one `proposals` key, so last-wins is already correct, which is the same reason `candidate` is unstaged today. Those two commit branches are untouched: votes and critiques still occur only in their own phases, and the split moves neither.

### 8. `result.py`, `summary.md`, and the viewer

`build_result`'s `consensus` handler **rebuilds** the candidate dict from the event (`result.py:50-53`) rather than amending the existing one, so it would silently drop `synthesized`. The `consensus` event therefore carries the flag explicitly, exactly as it already carries `tally` (`orchestrator.py:142-144`). `no_consensus` needs nothing: it serializes `state["candidate"]` whole (`:108`), and `set_candidate` reads the snapshot (`result.py:20-27`).

Handler changes: `set_candidate` and the `candidate` handler default `synthesized` via `.get("synthesized", False)`; a new `synthesis` handler sets `candidate_text` and marks the candidate synthesized.

`render_final`'s provenance splits:

```
Approved 2026-07-15T… · synthesized by **A**, round 2 · 3 accept / 0 reject / 0 abstain
Approved 2026-07-15T… · from **A**, round 2 · 3 accept / 0 reject / 0 abstain
```

The second is today's wording, retained for the fallback. The rejected-candidate branch (`result.py:126-133`) splits the same way. This one-word difference is the cycle's entire user-visible payoff: the answer stops being *agent A's text* and becomes *A's merge of the roster's work*.

**`summary.md` is the third view that credits a candidate**, and it splits identically: `render_summary` prints `Candidate from **A**` (`store.py:321`) and `## Current candidate (from A) — pending human decision` (`store.py:331`). Leaving it alone would have `final.md` credit a merge while `summary.md`, describing the same candidate, credits its author.

The wording is duplicated inline rather than shared. `result.py` imports nothing and `store.py` does not import it, so a shared helper is available — but the viewer must express the same rule in JavaScript regardless, so factoring it would unify two of three sites at the cost of a new dependency from the I/O module to the projection module. A one-line ternary is not worth that.

**The viewer needs one edit: the hero panel's provenance line**, mirroring `render_final`. The event taxonomy needs nothing — `classifyEvent` defaults unknown types to `content` by explicit design, "A new event type must degrade to a card, never blank the reading view" (`viewer/index.html:192-195`), so `synthesis` and `synthesis_failed` render as content cards on arrival. Phase grouping is generic over `PHASE_DELIMITERS` (`:186`), so six phases group without change. `2026-07-15-viewer-rendering-design.md` §1 wrote that taxonomy specifically so the next cycle would not silently degrade the viewer the way the reliability and replay cycles did. This is that cycle, and it does not.

### 9. Compatibility

No migration, no rewritten transcripts.

- **The four committed debates are pre-genesis.** They raise `MissingGenesis` and `fsck` reports exit 3, unverifiable — unchanged by this cycle, and still true for the reason the replay spec gave.
- **An old checkpoint at `last_completed_phase == "revise"`** resumes into `nominate` rather than `vote`. Correct without special-casing: it runs the superset, `nominate → synthesize → vote`.
- **An old checkpoint at `last_completed_phase == "vote"`** resumes into the next round's `critique`. Unchanged.
- **Old candidate dicts lack `synthesized`.** Reads default it to `False` rather than raising, per the compatibility pattern the predecessor spec set in its §9. It self-heals on the next `_phase_nominate`, which rebuilds the candidate whole.

**Known limitation — the fallback answer keeps its changelog.** On §5's path the candidate is a verbatim revision, so `final.md` opens with the winner's `Changes:` section, exactly as it does today. Synthesis makes the leak rare rather than universal; it does not close it. Closing it properly means parsing agent prose into sections, which this spec rejects for the reason recorded below.

## Testing

Unit, `MockAgent` only, no network.

- **Sequencing:** `next_phase` walks all six phases from round 0; round 2 enters at `critique`; `propose` occurs exactly once across three rounds.
- **Prompt:** `synthesize_prompt` contains every proposal, every critique, and the reject reasons; it forbids a preamble; it does not ask the winner to defend its own proposal.
- **Happy path:** the synthesis becomes `candidate["text"]`, `synthesized` is `True`, `proposals[winner]` is replaced, and exactly one DEEP call is made in the phase.
- **Fallback — error:** an agent raising `AgentError` on synthesize emits `synthesis_failed`, leaves the candidate verbatim with `synthesized: False`, leaves `proposals` untouched, and the vote still reaches consensus.
- **Fallback — empty:** a reply of `"   "` behaves identically, with `reason: "empty"`.
- **No halt:** synthesis failure never produces status `error`; `failed_phase` is never `"synthesize"`.
- **Carry-forward:** a rejected round-1 synthesis appears in round 2's critique prompt; the original proposal does not.
- **Resume granularity:** a debate halted in the vote fanout and resumed makes **zero** further synthesis calls — the phase split's entire justification, asserted on `agent_call` events.
- **Replay:** exact state equality through a synthesized debate, a fallback debate, and a two-round debate with carry-forward; `synthesis_failed` changes no key; the candidate resets on `nominate`.
- **`fsck`:** clean on a synthesized debate; detects a tampered `candidate["synthesized"]`.
- **Result:** `synthesized` survives `consensus` into `result.json`; `render_final` renders both provenance wordings; `no_consensus` preserves the flag through the snapshot.
- **Viewer:** `synthesis` renders as a content card with no taxonomy change; the hero shows synthesized provenance.

Integration: extend the mock-agent debate so the winner synthesizes, asserting the candidate text differs from every proposal and that `final.md` carries no `Changes:` line.

## Superseded decisions

From `2026-07-14-ai-debate-lab-design.md`:

- The candidate is a verbatim proposal selected by plurality → the candidate is now a merged answer drafted by the plurality winner, and a verbatim proposal only when synthesis fails.
- The four-phase protocol → six phases. `nominate` is not new work; it is the first half of the compound `vote` phase given the name it always had in the events it emits.

## Deferred

- **Context budgeting.** `synthesize_prompt` embeds every proposal *and* every critique, making it the largest prompt in the protocol — larger than `revise_prompt`, which was already the roster-quadratic one. **This cycle makes context budgeting materially more pressing**, and it is the natural next item in the predecessor's Reliability bucket.
- **Splitting `Changes:` out of revisions**, which would close §9's known limitation and make the viewer's deferred revision-diff item substantially easier. Wants its own cycle because it changes what a proposal *is*.
- **Prompt anonymization** — the other half of the predecessor's Protocol features bucket, and now cheaper: `synthesize_prompt` is one more prompt to map, and the mapping's natural home is a wrapper over all six.
- **Whether the synthesizer should vote on its own synthesis.** Self-nomination is forbidden; self-voting is not, and never has been — today the winner votes on its own proposal. Synthesis does not change the rule but sharpens the question, since the candidate is now authored during the round rather than selected from it.
- **A neutral or rotating synthesizer** rather than the nomination winner.
- Revision diffs, richer `debate status`, `summary.md`'s "Debate process" divider, `run()` resuming from replay, `debate rebuild`, sequence numbers / `schema_version`, the remaining locks (`index.json`, `approve`/`reject`), and CLI polish — all still parked where their specs left them.

## Explicitly rejected

- **Synthesizing inside the existing `vote` phase** — no `PHASES` change and no replay change, but it grows the module's largest function and puts a DEEP call inside a bracket that a vote-fanout halt re-runs from the top. Phase boundaries are the resume granularity; the expensive step gets its own.
- **Folding synthesis into a merged `nominate` phase** (the roadmap's literal "5th phase") — one phase name for a roster-wide FAST fanout and a single-agent DEEP draft, which is the same compounding that made `_phase_vote` worth splitting in the first place.
- **Discarding a rejected synthesis** — rebuilds the most expensive artifact each round with no memory of the objections, and guarantees the rejected document is absent from the critique that the reject reasons are attached to. See §4.
- **A dedicated `synthesis` state key** alongside `proposals` — keeps `proposals` a clean record of what each agent actually said, at the cost of a key that `replay`, `result`, and `fsck` must all model, plus a rule for whether the synthesis is nominatable that tangles authorship with the self-nomination ban.
- **Halting on synthesis failure** — discards a full round over one transient failure, and borrows a two-responder rule written for a different situation. See §5.
- **Re-drawing a synthesizer from the remaining nominees** — most likely to still produce a merged answer, and it makes the agent that gets voted on differ from the agent the roster nominated. "The mechanism quietly substituted a different answer" is the class of defect this series exists to eliminate.
- **Parsing the synthesis reply for a marker or section structure** — nothing to parse; the reply is the answer. See "Design constraint".
- **Parsing `Changes:` out of revisions in this cycle** — the `parse_vote` sin re-run one layer up, where a missing marker corrupts a proposal rather than a vote.
- **Letting an empty synthesis stand as the candidate** — a blank answer reaching a human as a real one.
- **Changing `revise_prompt`'s contract** — its `Changes:` section is written for the agents critiquing it next round and works there.
