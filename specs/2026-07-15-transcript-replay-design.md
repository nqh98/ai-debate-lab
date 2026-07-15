# Transcript Replay and `fsck` — Design Spec

Status: proposed
Date: 2026-07-15
Supersedes: nothing
Predecessor: `specs/2026-07-14-protocol-correctness-design.md` ("Deferred roadmap → Reliability")

## Purpose

Make the transcript verifiably the source of truth it already claims to be, and give divergence a name, a command, and an exit code.

Scope: a pure `replay(events) -> state` fold, the four events the transcript is missing before that fold can be total, and `debate fsck <id>` to compare replay against the checkpoint. **`run()` is not rewired to resume from replay by this spec** — replay is read-only here, and `fsck` is the evidence a later cycle needs before `run()` may depend on it. `protocol.py` is not touched.

## Motivating defects

One false claim, one unreconstructible field, one unprovable inference, one hand-rolled precedent.

**False claim — the transcript is not the source of truth.** `store.py:1-2` opens:

```python
"""File-backed debate storage: transcript.jsonl is the source of truth,
state.json is the derived checkpoint, summary.md the human-readable view.
"""
```

`create()` (`store.py:133-166`) writes `state.json` with `title`, `max_rounds: 5`, `quorum: "2/3"`, and `roster: None`, then `touch()`es an empty `transcript.jsonl` and emits nothing. The first event in every real transcript is a `proposal`. Five of `state.json`'s fourteen keys — `id`, `title`, `max_rounds`, `quorum`, `roster` — have no event backing at all, so `replay(events) -> state` as the roadmap words it cannot be written against today's transcript. The docstring describes an intention, not the system.

**Unreconstructible — the roster, which is the consensus denominator.** `run()` emits `roster_changed` only when the roster *differs* from a previous run (`orchestrator.py:42-48`); on a first run it emits nothing and assigns `state["roster"]` directly at line 49. The roster is what `protocol.check_consensus` divides by — the whole point of the quorum work in the predecessor spec. A replay that has to guess it can reach a different verdict than the run did, which is the one failure a verification tool must never have.

**Unprovable — a completed phase is indistinguishable from a halted one.** `state["last_completed_phase"]` is assigned at `orchestrator.py:71`, after the phase function returns. Nothing marks that boundary in the transcript. This is not speculative: a probe fold over the four committed debates reproduced **31 of 32 derivable keys**, and the single divergence is exactly this case:

```
20260714-how-can-this-repository-be-improved-furt-2  (3 events)
  DIVERGE last_completed_phase:
     state.json: null        # propose raised DebateHalted
     replay    : "propose"   # inference read it as completed
```

Both agents abstained, `_fanout` raised `DebateHalted` (`orchestrator.py:178-182`), and the phase never completed. Any rule that infers completion from event coverage — "every roster member emitted a terminal event for this `(round, phase)`" — guesses, and on the one committed debate that crashed, it guesses wrong. The `vote` phase makes inference worse still: seven event types interleave around the `candidate` midpoint, and two fanouts share one phase.

**Precedent — the reconciliation already exists, for one event type out of sixteen.** `cmd_decide` (`cli.py:106-138`) reads `human_decision` events back out of the transcript and reconciles `state.json` against them, refusing to proceed on conflict. That is replay's argument, hand-rolled, for 1/16th of the event vocabulary. The pattern was found necessary once and never generalized.

## Design constraint: replay must not share code with the orchestrator

The obvious refactor is to extract the state mutations from `Orchestrator` into functions that both `run()` and `replay()` call. It is DRY, and it defeats the purpose.

If both sides share the fold, `fsck` compares `state.json` against the very logic that produced it. It can still catch a truncated write, a lost append, or a corrupted file — but it can never catch a fold bug, which is the class of error most likely to exist and the only class `replay` introduces. The comparison would be a tautology.

This is the same circularity that rules out back-filling genesis events into legacy transcripts from their `state.json`: `fsck` would then verify `state.json` against a replay seeded from `state.json` and pass unconditionally.

**The duplication is the feature.** `replay` is an independent reimplementation of the fold, and `fsck` is the differential test between the two. The cost is real and is accepted: a change to how a phase updates state means editing two places, and a `replay` left stale reports false divergence. §6 exists to make that cost loud instead of silent.

## Design

### 1. Genesis events (`store.py`, `orchestrator.py`)

Two events carry the facts that are currently inputs rather than history.

```json
{"type": "debate_created", "id": "20260715-...", "title": "...",
 "max_rounds": 5, "quorum": "2/3", "round": 0, "phase": "create", "agent": null}

{"type": "run_config", "roster": ["claude", "codex", "agy"],
 "max_rounds": 5, "quorum": "2/3", "round": 0, "phase": "run", "agent": null}
```

`create()` emits `debate_created` as the transcript's first line. `run()` emits `run_config` **on every run**, not only when something changed — that is precisely the bug in `roster_changed`, which fires on a difference and therefore records nothing on the run that matters most. `run_config` supersedes `roster_changed` for replay's purposes; the older event stays as an audit note (§4) and is not removed, since existing transcripts contain it.

**`debate_created` records `max_rounds` and `quorum` even though `create()` defaults them.** Replay must not import those defaults from `store`/`protocol`. If it did, changing the default from `5` to `10` would silently rewrite the history of every debate created before the change — replay would assert a `max_rounds` the run never used, and `fsck` would report divergence on healthy debates with no code having touched them. Recording the value at creation makes an old transcript replay correctly forever. The redundancy against `state.json` is the price of a self-describing ledger, and it is the point.

### 2. Phase markers (`orchestrator.py`)

```json
{"type": "phase_started",   "round": 2, "phase": "critique", "agent": null}
{"type": "phase_completed", "round": 2, "phase": "critique", "agent": null}
```

`phase_started` is emitted where `state["round"]` is assigned (`orchestrator.py:67-69`); `phase_completed` where `state["last_completed_phase"]` is assigned (`:71`). Together they mirror the two assignments replay must reproduce, exactly and without inference.

They are not symmetric decoration. Each earns its place:

- **`phase_completed`** is the fix for the divergence proved above. It is the difference between recording that a phase ended and guessing it from who spoke.
- **`phase_started`** carries `round`. `run()` assigns `state["round"] = rnd` *before* the phase runs, so a debate halted mid-phase checkpoints with `round` set to the round that failed — `...-furt-2` has `round: 1, last_completed_phase: null`. Deriving `round` from the last `phase_completed` yields `0` there. `phase_started` also gives the reset points in §4 an explicit trigger, and a `phase_started` with no matching `phase_completed` is what §5 uses to recognise a crashed phase.

`max(e["round"] for e in events)` would also recover `round` today, but implicitly, from events that carry `phase: "end"` and `phase: "human"` — values outside `protocol.PHASES`. Given that this module's entire value is exactness, an explicit marker beats an incidental one.

### 3. `replay(events) -> state` (`debatelab/replay.py`, new)

A pure module: no files, no network, no clock, no store. Same contract as `protocol.py` and `retry.py`, enforced by the same AST import check the reliability plan used (`store`, `orchestrator`, `prompts`, `cli` are all banned imports). It is a dispatch table from event type to a fold function, applied left to right over the event list.

Twenty event types exist after §1–§2. **Fourteen fold into state; six are audit-only** and replay ignores them by name:

| Folds into state | Audit-only (ignored) |
|---|---|
| `debate_created` → `id`, `title`, `max_rounds`, `quorum` | `agent_call` |
| `run_config` → `roster`, `max_rounds`, `quorum`, `status` | `nomination` |
| `phase_started` → `round`, resets (§4) | `nomination_retry` |
| `phase_completed` → `last_completed_phase` | `nomination_dropped` |
| `proposal`, `revision` → `proposals[agent]` | `fallback_candidate` |
| `critique` → `critiques[agent]` | `roster_changed` |
| `candidate` → `candidate` | |
| `vote` → `votes[agent]` | |
| `abstained` → `abstained` | |
| `consensus`, `no_consensus`, `error` → `status` | |
| `human_decision` → `human_decision`, `status` | |

The six audit-only types are real events with real value — `agent_call` is the reliability cycle's telemetry, `nomination_dropped` records a self-nomination — but none of them changes `state.json`, so none of them may change replay's output.

### 4. The fold rules that are not uniform

Replay must reproduce these exactly. Each was verified against the four committed transcripts, not reasoned about in the abstract; a rule that is merely plausible produces a false positive, and a verification tool that cries wolf on the debates it exists to inspect gets ignored permanently.

**`proposals` — last write wins, across two event types.** `_phase_propose` assigns `state["proposals"] = results` (`:190`, replace) and `_phase_revise` assigns `{**state["proposals"], **results}` (`:225`, merge). The distinction is moot: `protocol.next_phase` returns `critique` after `vote` (`protocol.py:20-21`), never `propose`, so the propose phase runs exactly once per debate and replaces an empty dict. Replay writes `proposals[agent] = content` for both `proposal` and `revision`, last wins. An agent that abstains during `revise` therefore keeps its earlier proposal text — which is what the merge at `:225` does.

**`critiques` and `votes` — reset at `phase_started`, not merged.** `_phase_critique` assigns `state["critiques"] = results` (`:212`) and `_phase_vote` assigns `state["votes"] = votes` (`:326`); both replace wholesale. An agent that critiqued in round 1 and abstained in round 2 must vanish from `critiques`. A merging fold would keep it, and would diverge only on multi-round debates with a mid-debate abstention — rare enough to survive review and land in production.

**`abstained` — resets at every `phase_started`, not every round.** `run()` clears it per phase iteration (`:68`). The `vote` phase's two fanouts (nominate, then vote) both append to it within a single iteration, so the value is a union across both. Replay keeps it sorted (`:106`, `:173`).

**`status`** — `run_config` → `running` (mirroring `:50`), `consensus` → `awaiting_human`, `no_consensus` → `no_consensus`, `error` → `error`, `human_decision` → its `content`. Initial value `created`, from `debate_created`.

### 5. `debate fsck <id>` (`cli.py`)

**`state.json` is the last checkpoint, not the latest truth.** `_checkpoint` runs at phase boundaries (`orchestrator.py:87`, `:89`, `:96`) and `cmd_decide` writes after a human decision. A hard crash leaves events appended after the last checkpoint — per-agent events land as futures complete (`:174`), while the checkpoint waits for the phase. Those trailing events are *supposed* to be absent from `state.json`. Comparing a total replay against the checkpoint would report divergence on exactly the crashed debates `fsck` exists to inspect.

So `fsck` truncates before comparing:

1. No `debate_created` → **`unverifiable`**, exit 3. Stop.
2. Find the last boundary event: `phase_completed`, `consensus`, `no_consensus`, `error`, or `human_decision`. (None found → the prefix is just `debate_created`, which is compared against `create()`'s initial state. That is a real check, not a vacuous one.)
3. `expected = replay(events[:boundary + 1])`.
4. Compare `expected` against `state.json` as whole dicts. Both carry the same fourteen keys for any debate created after this spec, which step 1 has already guaranteed is the only kind reaching this step (§7).
5. Equal → **`ok`**, exit 0, noting `N events in flight after the last checkpoint` when the transcript continues past the boundary. Unequal → **`diverged`**, exit 1, printing a per-key diff.

Exit 2 is left to argparse for usage errors. The trailing in-flight events are not verified against anything, because there is nothing to verify them against — that is a statement about what a checkpoint is, not a gap.

`replay` knows none of this. It is a total fold over whatever list it is handed; the boundary logic is `fsck`'s alone.

### 6. Unknown event types raise

The six audit-only types of §3 are an explicit allowlist. Any type in neither the fold table nor that allowlist raises `UnknownEvent`.

This is the forcing function that keeps the duplication accepted in "Design constraint" honest. Silently ignoring an unmodelled event is how `replay` drifts out of sync with `Orchestrator` and `fsck` decays from a differential test into a green light that means nothing. Raising makes the next person to add an event type declare whether it folds. The cost is that adding a purely cosmetic event breaks `fsck` on new debates until replay is updated by one line — which is the intended pressure, applied at the moment the decision is being made.

### 7. Compatibility

`str`-level and file-level compatibility are unaffected: no existing event changes shape, no state key is added or removed, `render_summary` is untouched.

The four committed debates predate genesis events and report `unverifiable`. They keep working everywhere else — `show`, `status`, `list`, and the viewer read `state.json`, which is unchanged. They are not migrated: back-filling genesis from `state.json` would make `fsck` pass unconditionally on them (see "Design constraint"), which is worse than an honest `unverifiable`, and it would rewrite an append-only ledger to do it.

**`state.json`'s shape has already drifted once, silently, and the `unverifiable` gate is what keeps that out of `fsck`.** All four committed debates carry **12 keys**; `create()` writes **14**. The predecessor spec added `quorum` and `roster` (§3, §4 there) and never back-filled existing debates, so `run()` absorbs the difference at `orchestrator.py:41-42`:

```python
state.setdefault("quorum", str(protocol.DEFAULT_QUORUM))
recorded = state.get("roster")
```

A whole-dict comparison against a legacy debate would therefore report `diverged` on two missing keys — a shape mismatch, not a fold bug, and a false positive of precisely the kind §4 exists to prevent. Refusing legacy debates at step 1 of §5 forecloses it. This is a second, independent reason for `unverifiable` beyond the missing genesis facts, and it is also the concrete evidence behind the deferred `schema_version` item: a shape change lands silently today, and the next one would break `fsck` on debates created between this spec and it.

Debates created after this spec are verifiable from their first line.

## Testing

**The load-bearing test is differential, not unit.** Drive `Orchestrator.run()` with `MockAgent` through each debate shape and assert `replay(events) == read_state()` for every one:

- consensus reached → `awaiting_human`
- round cap exhausted → `no_consensus`
- `DebateHalted` → `error`, with `last_completed_phase` still `null` when propose halts (the divergence from "Motivating defects", now a regression test)
- `approve` / `reject` after consensus
- a transient failure retried, and a permanent one abstaining (the reliability cycle's `agent_call` events must not perturb replay)
- a self-nomination dropped, a re-asked nomination, a fallback candidate
- multi-round: an agent that critiques in round 1 and abstains in round 2 (the reset rule of §4)

This is `fsck` run in process. It is what catches the two folds drifting apart, and it is why the duplication is affordable.

**Unit tests on the fold:** each reset rule; last-wins on duplicate `(round, phase, agent)` events, which is the crash-resume case the README already documents; `UnknownEvent` on an unmodelled type; audit-only events provably not perturbing state; `MissingGenesis` on a legacy event list.

**`fsck` tests:** all four verdicts and their exit codes, including a deliberately corrupted `state.json` for `diverged`, one of the committed legacy debates for `unverifiable`, and a transcript truncated mid-phase for the in-flight note.

**Purity test:** the AST import check from the reliability plan, retargeted at `replay.py`.

The suite is 207 tests in ~3s and must stay single-digit seconds. Replay is pure and `fsck` touches two files; nothing here sleeps or spawns.

## Deferred

- **`run()` resuming from replay** — the roadmap's actual "checkpoint is a cache" goal. Deliberately next, not now: `run()` must not depend on replay before `fsck` has demonstrated on real debates that replay is faithful. Shipping the check first is what earns the right to the switch, the same way the reliability cycle shipped modest defaults plus telemetry before tuning anything.
- **`debate rebuild <id>`** — `write_state(replay(events))`, a repair path to pair with `fsck`'s detection. Small once replay exists, and better specified once there is one real divergence to look at.
- **Sequence numbers and `schema_version` on events** — dedup on resume, and migration safety. Wanted by the source debate; nothing consumes them yet, and replay's last-wins rule handles duplicates without them. §7 records the case for the `schema_version` half: `state.json` already drifted from 12 keys to 14 with no marker, and `fsck`'s whole-dict comparison is what makes the next such drift expensive.
- **`result.json` / `final.md`** — the next presentation cycle, and the reason this one goes first: both must be projections of replay rather than a third source of truth.
- Context budgeting, prompt anonymization, the synthesis phase, remaining locks (`index.json`, `approve`/`reject`), and CLI polish — all still parked in the predecessor's deferred roadmap.

## Superseded decisions

- **`roster_changed` as the roster's record** (`orchestrator.py:42-48`) — fires only on a difference, so the first run records the roster nowhere. Superseded by `run_config`, which fires every run. The event itself remains, as an audit note, because existing transcripts contain it.
- **`store.py:1-2`'s docstring** — currently describes an intention. It becomes true for debates created after this spec, and the docstring should say which.

## Explicitly rejected

- **Sharing the fold between `run()` and `replay()`** — DRY, and it reduces `fsck` to a tautology. See "Design constraint".
- **Back-filling genesis events into the four legacy transcripts** — makes `fsck` pass unconditionally on exactly the debates it would be verifying, by seeding the replay from the file under test. Rewrites an append-only ledger for a check that proves nothing.
- **Deleting the legacy debates to avoid the compatibility path** — they are the repo's only real worked examples, and one of them is the transcript that produced this roadmap.
- **Inferring phase completion from roster coverage** — proved wrong on `...-furt-2` before this spec was written. `vote`'s seven event types and two fanouts make it worse, not better.
- **Importing `max_rounds`/`quorum` defaults into replay instead of recording them** — turns a future default change into a silent rewrite of every old debate's history.
- **`replay` as a `DebateStore` method** — puts a pure fold inside the I/O class and makes it untestable without a filesystem, against the grain of `protocol.py` and `retry.py`.
- **Rewiring resume in this cycle** — see "Deferred".
