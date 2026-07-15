# Reliability Leftovers: Write Coordination and Model Telemetry — Design Spec

**Date:** 2026-07-15
**Status:** Approved design, pending implementation
**Predecessor:** `2026-07-14-protocol-correctness-design.md` ("Deferred roadmap → Reliability"), `2026-07-15-agent-reliability-design.md` (§5, "Deferred")

## Purpose

Empty the reliability bucket the protocol-correctness spec opened, down to one item. Two things: every process that mutates shared debate state coordinates with every other one, and every agent call records which model answered it.

Afterwards the bucket holds exactly one piece of unattempted work — context budgeting — plus two the spec decides against on the merits rather than postpones: `tokens` (§3) and `model` on failed attempts (§3). The distinction matters for whoever reads the Deferred list next. One of the three is waiting for a cycle; the other two are settled, each with the specific condition that would reopen it recorded next to it.

Scope: a root-level lock for `index.json`, the debate lock extended to `approve`/`reject`, and a `model` field on `agent_call` events. No protocol changes, no new phases, no state-shape changes. `protocol.py`, `prompts.py`, and `replay.py` are not touched.

**The two halves are independent, and this spec does not pretend otherwise.** The predecessor could argue its halves were complementary — strict parsing decides what the right answer is, the run lock decides whether that answer survives being written down. No such claim is available here. These two are together because each is a small change against `store.py` and `orchestrator.py` that has been waiting for a cycle to attach to, and because together they empty the bucket down to the one item that genuinely needs its own spec. Inventing a theme to bind them would be worse than admitting there isn't one.

## Motivating defects

**Known limitation, now fixable — `index.json` races across debates.** `rebuild_index` (`store.py:240-253`) reads every `state.json`, then writes the whole index. Two runs on *different* debates can each read all states and then both write; the second write may miss the first's update because it read before that write landed. The predecessor named this precisely and deferred it (§6) on the grounds that it needs a root-level lock. It does. That lock is the first half of this spec.

The predecessor's reasoning for deferring was sound and remains worth recording: the atomic write it *did* ship removed the torn-read failure, which is the one the polling viewer actually hits. A lost index update self-heals on the next `rebuild_index`, because each rebuild is a full fresh scan rather than an incremental edit. This is therefore a real bug with a bounded blast radius — stale index entries until the next write — not an urgent one. It is fixed here because the tool to fix it already exists in the file.

**Unenforced — `approve`/`reject` mutate state with nothing held.** `cmd_decide` reads state at `cli.py:217`, mutates it in memory, and writes it back at `cli.py:243` or `cli.py:266`. A concurrent `run` checkpointing between that read and that write loses one of the two updates: either the human decision is overwritten by the run's checkpoint, or the run's round progress is overwritten by the decision. The predecessor deferred this explicitly and honestly — "locking them is deferred rather than dismissed" (§7) — on the grounds that the decide path is interactive, single-shot, and gated on a status that `run` does not produce concurrently.

That last clause is the part that does not hold, though not for the reason it looks like. `run` *does* refuse to resume an `awaiting_human` debate, returning early before it writes anything (`orchestrator.py:51-52`) — so that status is genuinely safe. The hole is the other status `cmd_decide` accepts: **`no_consensus`** (`cli.py:248`), which `run` resumes without complaint.

**The gate reads a checkpoint that a live run has not written yet.** `run` marks itself `"running"` in memory at `orchestrator.py:69`, but the first `_checkpoint` that puts that on disk is `orchestrator.py:149` — after the whole first phase has completed, which is minutes of concurrent agent calls. For that entire window, `state.json` still reads `no_consensus` while a run is live and about to write. A human who rejects the old answer in that window:

1. `cmd_decide` reads `no_consensus` (`cli.py:217`) and passes the gate (`cli.py:248`).
2. It appends a `human_decision` event and writes `state.json` with `status: "rejected"` (`cli.py:266`).
3. The run's first checkpoint (`orchestrator.py:149`) overwrites `state.json` with `status: "running"`.

The decision survives in the transcript and vanishes from the checkpoint. Because the transcript is the source of truth and `state.json` is a derived cache, this is precisely the divergence `debate fsck` exists to report — a spec ago we built the detector for a corruption we had not yet closed the door on. The window is not the microseconds between a run's last two checkpoints; it is however long a `propose` fanout takes.

**Blind spot — the DEEP/FAST routing is unverifiable.** `models.choose_model` picks the strongest model for propose/critique/revise and the cheapest for nominate/vote, and `agents.yaml` is built entirely around that promise (lines 3-8). Nothing records which model was actually chosen. The routing either works or silently does not, and no transcript can distinguish the two. `agent_call` events already carry `task` (`deep`/`fast`) — the *request*. They do not carry the resolution, which is the half that could be wrong.

## Design constraint: the predecessor deferred `model` for reasons that must be answered, not ignored

`2026-07-15-agent-reliability-design.md` §5 dropped `model` from telemetry deliberately, and its Deferred section set an explicit gate: *"Needs a safe way to read the resolved model without network I/O or cross-thread mutable state."* Its two objections were concrete:

1. A public accessor (`CliAgent._model_for` and `ApiAgent._model_for` are private) would be *"a public accessor that can do I/O and fail — inside the telemetry path, which must never be the thing that breaks a debate."* `ApiAgent._model_for` can perform network discovery and raise `AgentError`.
2. A `last_model` attribute would be *"the kind of state that is correct today only because each agent happens to receive exactly one concurrent call per phase"* — an agent object is shared across `_fanout`'s `ThreadPoolExecutor`.

Both objections share a hidden premise: **that telemetry must pull the model out of the agent after the fact.** Both dissolve if the agent pushes it back as part of the call's return value.

- No I/O is added, because none is new. `ApiAgent.ask` already calls `_model_for` (`api_agent.py:143`) and `CliAgent.ask` already calls it via `_build_command` (`cli_agent.py:23, 53`). The resolution happens on the call's critical path today; if it raises, the call already fails and telemetry already records the error. Reporting a value that was computed anyway adds no call and no new failure mode.
- No shared state exists, because a return value rides the caller's own stack. Thread count is irrelevant.

§5's third objection — *"a field nobody has asked a question of yet"* — is answered by the third motivating defect above. There is now a question: does the routing fire?

The gate is met. This spec supersedes that deferral for `model` only. `tokens` stays deferred, and §3 below explains why the same argument does not rescue it.

## Design

### 1. Root lock for `index.json` (`store.py`)

`_lock_transition(debate_path)` (`store.py:98-109`) already flocks a directory fd to serialize the check-then-act inside lock acquisition. It is the right primitive for this too, and it is generalized rather than duplicated — its name and docstring are debate-specific today:

```python
@contextlib.contextmanager
def _dir_lock(path: Path):
    """Serialize a check-then-act against everything under `path`."""
```

`rebuild_index` wraps its read-all-then-write in `_dir_lock(self.root)`. `_acquire_lock` and `_release_lock` keep their directory-scoped calls, renamed to `_dir_lock(path.parent)`.

**`self.root.mkdir(exist_ok=True)` must move above the lock, not inside it.** `_dir_lock` opens the directory to get an fd, so the directory must exist before the lock can be taken — and `rebuild_index` is reachable today with no `debates/` at all: `list_ids` returns `[]` when the root is missing (`store.py:229-230`) and the current `mkdir` at `store.py:252` is what creates it, *after* the scan. Wrapping the existing body verbatim therefore raises `FileNotFoundError` on the first `debate new` of a fresh checkout. The mkdir is idempotent and races harmlessly, which is what makes hoisting it out of the critical section safe.

**flock, not the `O_EXCL` PID protocol used by the debate lock.** A rebuild is milliseconds, so the correct behavior for a competing process is to *block*, not to refuse. flock is released by the kernel when the holder dies, so there is no staleness to detect, no PID-reuse race to inherit, and no `--force` to add. The entire PID-liveness protocol exists to make a *long-lived* holder's death detectable by another process; against a millisecond critical section it buys nothing and costs a concept. Using the heavier lock here because it is the lock we already have would be the wrong kind of consistency.

**The lock lives inside `rebuild_index`, not at its call sites.** All four callers — `store.create` (`store.py:180`), `Orchestrator.run` (`orchestrator.py:158`), and both `cmd_decide` paths (`cli.py:245`, `cli.py:268`) — are then protected by construction, and a fifth caller cannot forget. The alternative, wrapping each call site, is four chances to get it wrong for no benefit.

**No lock-ordering cycle is possible.** `_dir_lock(self.root)` is taken only inside `rebuild_index`, which never takes a debate-directory lock. `_dir_lock(debate_dir)` is taken only inside `_acquire_lock`/`_release_lock`, neither of which rebuilds the index. The root→debate edge does not exist, so the two locks cannot deadlock against each other. This is asserted by test, not by argument (see Testing). Note that the debate *lock file* is held across `rebuild_index` in `cmd_decide` — that is not a flock and holds no kernel lock, so it does not participate in ordering.

**flock is advisory**, so this works only because every writer of `index.json` goes through `rebuild_index`. That is true today and is the reason the lock belongs in that function.

This closes the "Known limitation" in the predecessor's §6.

### 2. `run.lock` → `debate.lock` (`store.py` + `cli.py`)

The lock's subject is the debate, not the verb that happens to be running. Once `approve` and `reject` take it, `run.lock` names the wrong thing.

| Today | After |
|---|---|
| `debates/<id>/run.lock` | `debates/<id>/debate.lock` |
| `DebateStore.run_lock(id, force=False)` | `DebateStore.debate_lock(id, *, command, force=False)` |
| `{pid, host, started_at, run_id}` | `{pid, host, started_at, run_id, command}` |

`command` is `"run"`, `"approve"`, or `"reject"` — the string the holder was invoked as. It exists so a refusal can name what it is refusing for:

```
error: debate is locked by pid 41 on box running `run` since
2026-07-15T10:03:11Z; use --force if that run is dead
```

Without it the message can only say a PID, and the operator's next question is always "doing what?".

`cmd_decide` wraps its read-mutate-write in the lock, which must cover the `read_state` at `cli.py:217` — a lock acquired after the read protects nothing, since the stale read is the whole defect. `approve` and `reject` each gain `--force`, matching `run`: a refusal a human cannot override is a wedged debate, and the stale-PID case is exactly as reachable here as it is for `run`.

Acquisition semantics, staleness, and `--force` are otherwise unchanged from the predecessor's §7, including the deliberate inheritance of the PID-reuse race: a spurious refusal is safe, two concurrent writers are not.

**Commands that do not lock.** `status`, `list`, `show`, `result`, and `fsck` are read-only. `new` creates a debate that nothing else can yet name; its index write is covered by §1.

**Known limitation — the rename has an upgrade window.** A `run` started by the previous version holds `run.lock`; a new `approve` takes `debate.lock` and does not see it. Both then write. Reaching it means upgrading the package in the exact minutes a debate is running, on a local dev tool. It is recorded rather than defended against: a shim that also checked the old name would be permanent code paying off a one-time, self-clearing risk, and `run.lock` files do not accumulate — the next release after this one would inherit the shim with nothing left for it to find.

### 3. `Reply` and the `model` field (`agents/base.py`, `agents/`, `retry.py`, `orchestrator.py`)

`Agent.ask` returns a value object instead of a bare string:

```python
@dataclass(frozen=True)
class Reply:
    text: str
    model: str | None = None   # None = the backend routed itself


class Agent(ABC):
    @abstractmethod
    def ask(self, prompt: str, task: str = models.DEEP) -> Reply: ...
```

**Changing the abstract method, rather than adding an `ask_detailed` alongside it, is what keeps the null honest.** The whole argument below rests on `model: null` meaning exactly one thing: *we declined to pin a model*. An optional method an adapter may skip gives the null a second meaning — *this adapter never reported* — and a field with two meanings, one of them a lie, is the exact defect §5 kept `tokens` out to avoid. Making it abstract means an adapter that reports nothing does not construct, so the ambiguity has nowhere to live. There are two adapters and one `MockAgent`; the churn is bounded and mechanical.

`retry.call_with_retry` passes the result to `on_attempt`, which is where the event is built:

```python
on_attempt(attempt, duration_ms, error, result)   # exactly one of error/result is None
```

`Orchestrator._record_call` (`orchestrator.py:180-196`) adds `"model": reply.model` to `agent_call` events. Both call sites unwrap `.text` (`_ask_one` at `orchestrator.py:236-246`, `_fanout.call` at `orchestrator.py:255-262`); nothing downstream of them changes, because `results[name]` is still the reply text.

**`model: null` is a fact, not a hole — and this is the entire reason `tokens` stays out.** The distinction decides both fields:

| Field | A null means | Honest? |
|---|---|---|
| `model` | We pinned nothing; the backend routed itself | Yes — records a decision we made |
| `tokens` | This call burned tokens and we cannot see how many | No — a measurement hole wearing a value's clothes |

`model` is knowable at the call site by construction, for every backend, because *we* are the ones who either resolve it or decline to. `claude` in the default roster has no `models_command` (`agents.yaml:11-14`), so its `model` is null on every call — and that null is true and useful: it says the CLI chose. `tokens` for that same call is null because a subprocess reports no usage (`cli_agent.py:47` returns `proc.stdout`), and the default roster is CLI-backed, so a tally of tokens would read as near-zero while real money was spent. §5's judgment that this *"invites exactly the wrong inference"* survives the `Reply` refactor unchanged, because `Reply` fixes plumbing and `tokens` was never a plumbing problem.

**Known limitation — failed attempts carry no model.** `AgentError` does not know it. Threading it out would mean attaching the model at the raise sites in `ApiAgent._request` (`api_agent.py:186-196`), several layers below where the model is resolved and inside the error path — which is the smuggling §5 declined to do, one level down. So "which model was rate-limited" stays unanswerable. `kind` already records why a call failed, which is the question that has actually been asked. Successful attempts carry `model`; failed ones omit the key, the same discipline `agent_call` already uses for `kind` and `content` on `ok` attempts.

**Coverage today is one agent in three, and that is worth stating plainly.** `claude` routes itself (null), `codex` resolves a model only when it falls back to the OpenAI API, and only `antigravity` resolves one on the CLI path (`agents.yaml:23-27`). The field earns its place anyway: it is the only thing that can answer whether `choose_model` picked a cheap model for `vote`, and coverage grows for free as backends gain `models_command`.

### 4. Compatibility

- Existing transcripts have `agent_call` events with no `model`, and stay valid. `replay` exempts `agent_call` as audit-only (`replay.py:32`) — an exemption keyed on the event *type*, not its shape, so a new field is inert to both `replay` and `fsck` by construction rather than by luck. This is the same property §5 relied on to add the event at all.
- `MockAgent` (`tests/conftest.py:14`) returns queued strings and moves to `Reply`. Its scripted-exception and synthesis-slot behavior is unchanged.
- Any `run.lock` left on disk by a previous version is ignored, not migrated. See §2's upgrade window.

## Testing

Unit tests, no network:

- **Root lock:** two `rebuild_index` calls racing under a `threading.Barrier` — neither loses the other's entry; a rebuild while the root lock is held by another thread blocks and then reflects both writes; `index.json` leaves no `.tmp` behind (the predecessor's guarantee still holds under the lock).
- **Lock ordering:** a thread holding `_dir_lock(debate_dir)` and a thread in `rebuild_index` complete in either interleaving — no deadlock. This is the argument in §1 made executable.
- **Debate lock:** `approve` against a debate locked by a live `run` exits non-zero and appends no event and writes no state (the read-mutate-write never starts); the refusal message names the holder's `command`; `approve --force` breaks it; two concurrent `approve`s serialize rather than interleave; `debate.lock` contains parseable JSON carrying `command`.
- **The decide race directly, as the defect describes it:** a debate at `no_consensus`, a `run` in its first phase (in memory `"running"`, on disk still `no_consensus`), and a concurrent `reject`. Without the lock the run's checkpoint overwrites the decision and `fsck` reports divergence — assert that first, so the test proves the bug exists before it proves the fix works. With the lock, `reject` refuses naming the holder, and `fsck` stays clean.
- **Telemetry:** a successful `agent_call` carries the resolved model for a pinned agent, and carries `"model": null` — the key present, the value null — for a CLI agent with no `models_command`. The null is the assertion: it is the claim "the backend routed itself", and a test that accepted an absent key would let the field's one meaning rot. A failed attempt omits the key entirely, which is the only case where absence is correct.
- **Routing:** `CliAgent` resolves *different* models for `DEEP` and `FAST` against a stub `models_command`. This is the assertion the field exists for, and it belongs at the adapter, not through the orchestrator: it tests `choose_model`, and routing it through a debate would only add ways to be wrong.
- **`Reply` is frozen:** a caller cannot patch a model in after the fact by mutating a reply, which is the shape the fix would take if someone hit a null they disliked. The deeper guarantee — that no adapter can *silently* skip reporting — is structural rather than a test: `ask` is abstract and returns `Reply`, so there is no optional method to decline and no path that yields a model-less call. That is the whole reason for changing the ABC instead of adding one.

Integration: extend the existing mock-agent debate to assert `agent_call` events carry `model` end to end, and that a second `cmd_run` against a locked debate still appends nothing (the predecessor's test, under the renamed lock).

## Superseded decisions

- **`2026-07-15-agent-reliability-design.md` §5 and Deferred, "`model` in telemetry."** The gate it set — no network I/O, no cross-thread mutable state — is met by a return value rather than an accessor or an attribute. Its reasoning was correct for the two options it weighed; it did not weigh the third. Its `tokens` judgment stands unchanged and is reaffirmed in §3.
- **`2026-07-14-protocol-correctness-design.md` §7, "Only `run` locks."** The stated ground was that `approve`/`reject` are "gated on a status that `run` does not produce concurrently". The gate is real but reads the wrong thing: `state.json` lags a live run by a full phase (`orchestrator.py:69` sets `"running"` in memory, `orchestrator.py:149` first writes it), and `no_consensus` — which `cmd_decide` accepts and `run` resumes — is unprotected for that entire window. A status gate cannot serialize writers when the status it reads is written by the writer it is trying to exclude.
- **`2026-07-14-protocol-correctness-design.md` §6, "Known limitation — `index.json` still races."** Fixed, on the terms that spec predicted: a root-level lock.

## Deferred

Each needs its own cycle.

- **Context budgeting.** The only unattempted item left in the predecessor's reliability bucket, and the reason this spec cannot claim to empty it. Every critique/nominate/revise/synthesize prompt embeds all proposals in full via `format_blocks` (`prompts.py:14-15`). It is deferred because it is not infrastructure: any budget changes *what agents see*, and therefore what they answer, which makes it a protocol change wearing a plumbing costume. It needs a spec that argues about truncation versus summarization on the merits, and it should be informed by the telemetry this cycle adds.
- **`tokens` in telemetry.** See §3. Reachable only if the roster stops being CLI-backed, at which point the honesty objection weakens on its own.
- **`model` on failed attempts.** Needs `AgentError` to carry it without instrumenting the raise sites. See §3.
- **`create`'s check-then-act race.** `store.create` (`store.py:141-145`) tests `self.path(debate_id).exists()` and then `mkdir(parents=True)` without `exist_ok`. Two `debate new` invocations with the same title on the same day both pass the check and one dies on `FileExistsError`. It fails loudly rather than corrupting, which is why it is a note rather than a fix — but it is the same class of bug as the two this spec closes, and it is recorded here so the next person to touch `create` finds it already written down.
- **A root lock for concurrent `create`.** The above, fixed properly, is `_dir_lock(self.root)` around the id-allocation loop. Not done here because a loud crash is not the failure this spec is about.
- Everything still parked in `2026-07-14-protocol-correctness-design.md` → "Deferred roadmap" and `2026-07-15-agent-reliability-design.md` → "Deferred".

## Explicitly rejected

- **The `O_EXCL` PID protocol for `index.json`** — staleness detection and `--force` are pure cost against a millisecond critical section that should block rather than refuse. See §1.
- **`ask_detailed()` alongside `ask()`** — an optional override reintroduces the silent-null failure that §5 rejected `model` to avoid, in the same place. See §3.
- **A `last_model` attribute on the agent** — cross-thread mutable state under `_fanout`'s executor. Rejected by §5 and rejected again here; the `Reply` return value is what makes the question moot rather than merely unlikely.
- **A separate `approve.lock`** — it would not serialize `approve` against `run`, which is the race being fixed. Two locks that do not exclude each other protect nothing.
- **A migration shim reading both `run.lock` and `debate.lock`** — permanent complexity for a window measured in minutes. See §2.
- **Wrapping `rebuild_index`'s call sites instead of its body** — four chances to forget, no benefit. See §1.
