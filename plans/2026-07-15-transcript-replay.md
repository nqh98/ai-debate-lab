# Transcript Replay and `fsck` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the transcript verifiably the source of truth it already claims to be, and give divergence a name, a command, and an exit code.

**Architecture:** Five tasks. Tasks 1–2 add the four events the transcript is missing, at the exact lines whose assignments they mirror — `store.create()` and `orchestrator.run()`. Task 3 adds `debatelab/replay.py`, a pure fold with no debatelab imports at all. Task 4 is the load-bearing differential test: drive the real `Orchestrator` through seven debate shapes and assert `replay(events) == state.json` for each. Task 5 wires `debate fsck`. `run()` is never rewired to resume from replay, and `protocol.py` is never touched.

**Tech Stack:** Python ≥ 3.10, PyYAML, stdlib only (`json`, `enum`-free, no new deps). pytest.

**Spec:** `specs/2026-07-15-transcript-replay-design.md`

## Global Constraints

- Python ≥ 3.10; runtime dependencies: **PyYAML only**. No new dependencies.
- **`protocol.py` must not be modified by any task in this plan.** Not one line.
- **`debatelab/replay.py` must not import `store`, `orchestrator`, `prompts`, or `cli`.** In practice it imports nothing from `debatelab` at all — it is a fold over plain dicts. Task 3 Step 6 enforces this with an AST check.
- **`replay.py` must not import `max_rounds`/`quorum` defaults from `store` or `protocol`.** They come off the `debate_created` event. Importing them would turn a future default change into a silent rewrite of every old debate's history (spec §1).
- **The fold must not be shared with `Orchestrator`.** The duplication is deliberate: `fsck` is the differential test between two independent implementations. Sharing the fold reduces `fsck` to comparing `state.json` against the logic that wrote it (spec, "Design constraint").
- **`run()` is not rewired to resume from replay.** Out of scope; deferred to the next cycle.
- Transcript event schema: `{ts, round, phase, agent, type, content}`, extra keys allowed. All four new events carry `content` — every existing event has it, and the viewer renders `esc(ev.content)` at `viewer/index.html:113`.
- No existing event changes shape; no `state.json` key is added or removed; `render_summary` is untouched.
- The four committed debates in `debates/` are **not** migrated and must keep working.
- Commit messages: conventional style (`feat:`, `fix:`, `test:`), **no attribution trailers of any kind**.
- All commands run from repo root `/home/bossbaby/Desktop/fix-me/ai-debate-lab`; Python is `.venv/bin/python`.
- Baseline before starting: `.venv/bin/python -m pytest -q` ⇒ **207 passed** in ~3s. The suite must stay single-digit seconds.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `debatelab/store.py` | `DEFAULT_MAX_ROUNDS`/`DEFAULT_QUORUM` constants; `create()` emits `debate_created` | 1 |
| `debatelab/orchestrator.py` | `run()` emits `run_config` every run; each phase bracketed by `phase_started`/`phase_completed` | 2 |
| `debatelab/replay.py` **(new)** | Pure fold: `replay(events) -> state`, `MissingGenesis`, `UnknownEvent` | 3 |
| `tests/conftest.py` | `make_store`/`happy_agent` moved here so Task 4 can share them | 4 |
| `debatelab/cli.py` | `debate fsck <id>`: boundary truncation, four verdicts, exit codes | 5 |

---

### Task 1: `create()` records the debate's identity and defaults

**Files:**
- Modify: `debatelab/store.py:1-16` (imports, add constants), `:133-166` (`create`)
- Test: `tests/test_store.py` (append)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `store.DEFAULT_MAX_ROUNDS = 5`, `store.DEFAULT_QUORUM = "2/3"`
  - A `debate_created` event as the transcript's first line, carrying `id`, `title`, `max_rounds`, `quorum`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
def test_create_emits_debate_created_as_the_first_event(tmp_path):
    """Regression: create() wrote state.json and touched an empty transcript,
    so five of state.json's fourteen keys had no event backing at all and
    replay(events) -> state could not be written."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("Pick a database", "which one?")
    events = store.read_events(did)
    assert events[0]["type"] == "debate_created"
    assert events[0]["id"] == did
    assert events[0]["title"] == "Pick a database"


def test_debate_created_records_the_creation_defaults(tmp_path):
    """The defaults are recorded, never imported by the reader: changing
    DEFAULT_MAX_ROUNDS later must not rewrite this debate's history."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "p")
    genesis = store.read_events(did)[0]
    assert genesis["max_rounds"] == 5
    assert genesis["quorum"] == "2/3"


def test_debate_created_agrees_with_the_state_written_beside_it(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "p")
    genesis = store.read_events(did)[0]
    state = store.read_state(did)
    for key in ("id", "title", "max_rounds", "quorum"):
        assert genesis[key] == state[key], key
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_store.py -q`
Expected: FAIL — `IndexError: list index out of range` (the transcript is empty).

- [ ] **Step 3: Add the constants**

In `debatelab/store.py`, add `protocol` to the imports. The import block (lines 5-15) becomes:

```python
import contextlib
import fcntl
import json
import os
import re
import socket
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import protocol

DEFAULT_MAX_ROUNDS = 5
DEFAULT_QUORUM = str(protocol.DEFAULT_QUORUM)
```

`protocol` imports only stdlib, so this adds no cycle. `DEFAULT_QUORUM` is derived from `protocol.DEFAULT_QUORUM` rather than repeating the literal `"2/3"`, which `create()` duplicated silently until now.

- [ ] **Step 4: Emit the event and use the constants**

Replace `create` (`debatelab/store.py:133-166`):

```python
    def create(self, title, problem, context_texts=()) -> str:
        base = f"{datetime.now().strftime('%Y%m%d')}-{slugify(title)}"
        debate_id, n = base, 2
        while self.path(debate_id).exists():
            debate_id = f"{base}-{n}"
            n += 1
        d = self.path(debate_id)
        d.mkdir(parents=True)
        parts = [f"# {title}", "", problem]
        for label, text in context_texts:
            parts += ["", f"## Context: {label}", "", text]
        (d / "problem.md").write_text("\n".join(parts) + "\n")
        (d / "transcript.jsonl").touch()
        # The transcript's first line, before state.json exists: the identity
        # and defaults are history, not merely inputs. Recorded rather than
        # left to the reader to import, so changing DEFAULT_MAX_ROUNDS later
        # cannot rewrite the history of debates created before the change.
        self.append_event(debate_id, {
            "round": 0, "phase": "create", "agent": None,
            "type": "debate_created", "content": title,
            "id": debate_id, "title": title,
            "max_rounds": DEFAULT_MAX_ROUNDS, "quorum": DEFAULT_QUORUM,
        })
        self.write_state(
            debate_id,
            {
                "id": debate_id,
                "title": title,
                "status": "created",
                "round": 0,
                "max_rounds": DEFAULT_MAX_ROUNDS,
                "quorum": DEFAULT_QUORUM,
                "roster": None,
                "last_completed_phase": None,
                "proposals": {},
                "critiques": {},
                "candidate": None,
                "votes": {},
                "abstained": [],
                "human_decision": None,
            },
        )
        self.rebuild_index()
        return debate_id
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 210 passed.

- [ ] **Step 6: Commit**

```bash
git add debatelab/store.py tests/test_store.py
git commit -m "feat: record a debate's identity and defaults in the transcript

store.py's docstring calls transcript.jsonl the source of truth and
state.json the derived checkpoint. create() wrote state.json and touched an
empty transcript, so id, title, max_rounds and quorum existed only in the
checkpoint and replay(events) -> state could not be written.

debate_created carries max_rounds and quorum even though create() defaults
them. A reader that imported those defaults instead would silently rewrite
the history of every existing debate the day a default changed."
```

---

### Task 2: `run()` records the roster and brackets every phase

**Files:**
- Modify: `debatelab/orchestrator.py:42-50` (add `run_config`), `:67-71` (add phase markers)
- Test: `tests/test_orchestrator.py` (append)

**Interfaces:**
- Consumes: Task 1's `debate_created`
- Produces: three events — `run_config` (`roster`, `max_rounds`, `quorum`) on every run; `phase_started` and `phase_completed` bracketing each phase, both carrying `round` and `phase`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orchestrator.py`:

```python
def _types(store, did, phase=None):
    return [
        e["type"] for e in store.read_events(did)
        if phase is None or e["phase"] == phase
    ]


def test_run_emits_run_config_on_every_run(tmp_path):
    """Regression: roster_changed fires only when the roster DIFFERS from a
    previous run, so a first run recorded the roster nowhere — and the roster
    is the denominator check_consensus divides by."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    configs = [e for e in store.read_events(did) if e["type"] == "run_config"]
    assert len(configs) == 1
    assert configs[0]["roster"] == ["a", "b"]
    assert configs[0]["max_rounds"] == 1
    assert configs[0]["quorum"] == "2/3"


def test_run_config_records_the_effective_overrides(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=3, quorum=Fraction(1, 2)
    )
    config = next(
        e for e in store.read_events(did) if e["type"] == "run_config"
    )
    assert config["max_rounds"] == 3
    assert config["quorum"] == "1/2"


def test_each_phase_is_bracketed_by_started_and_completed(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    for phase in ("propose", "critique", "revise", "vote"):
        types = _types(store, did, phase)
        assert types[0] == "phase_started", phase
        assert "phase_completed" in types, phase


def test_a_halted_phase_records_started_without_completed(tmp_path):
    """The divergence this whole cycle exists for: a phase that raised
    DebateHalted must be distinguishable from one that finished."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), DeadAgent("b"), DeadAgent("c")]
    ).run(did, max_rounds=1)
    types = _types(store, did, "propose")
    assert "phase_started" in types
    assert "phase_completed" not in types
    assert store.read_state(did)["status"] == "error"
    assert store.read_state(did)["last_completed_phase"] is None
```

`test_run_config_records_the_effective_overrides` needs `Fraction`, which `tests/test_orchestrator.py` does not import. Add it to that file's import block:

```python
from fractions import Fraction
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -q`
Expected: FAIL — `assert 0 == 1` on the `run_config` count (no such event exists).

- [ ] **Step 3: Emit `run_config`**

In `debatelab/orchestrator.py`, replace lines 42-50:

```python
        recorded = state.get("roster")
        if recorded is not None and recorded != self.order:
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "run", "agent": None,
                "type": "roster_changed",
                "content": f"roster changed from {recorded} to {self.order}",
            })
        state["roster"] = list(self.order)
        state["status"] = "running"
        # Every run, not only when something changed — that is precisely the
        # bug in roster_changed above, which fires on a difference and so
        # records nothing on a first run. The roster is the denominator
        # check_consensus divides by; a reader that has to guess it can reach
        # a different verdict than this run did.
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "run", "agent": None,
            "type": "run_config",
            "content": (
                f"roster {self.order}, max_rounds {state['max_rounds']}, "
                f"quorum {state['quorum']}"
            ),
            "roster": list(self.order),
            "max_rounds": state["max_rounds"],
            "quorum": state["quorum"],
        })
```

`roster_changed` stays: existing transcripts contain it, and it is a useful audit note. Replay ignores it (Task 3).

- [ ] **Step 4: Bracket every phase**

Replace lines 67-71 (which become the block below; the `getattr` dispatch is unchanged):

```python
                state["round"] = rnd
                state["abstained"] = []
                self.progress(f"round {rnd}/{state['max_rounds']}: {phase}")
                # Brackets the two assignments a reader must reproduce. Without
                # phase_completed, a phase that raised DebateHalted is
                # indistinguishable from one that finished; without
                # phase_started, a halted debate's round cannot be recovered,
                # because state["round"] is assigned before the phase runs.
                self.store.append_event(debate_id, {
                    "round": rnd, "phase": phase, "agent": None,
                    "type": "phase_started", "content": "",
                })
                getattr(self, f"_phase_{phase}")(debate_id, state, problem)
                state["last_completed_phase"] = phase
                self.store.append_event(debate_id, {
                    "round": rnd, "phase": phase, "agent": None,
                    "type": "phase_completed", "content": "",
                })
```

`phase_completed` must be emitted here — after the phase returns, before the consensus check at line 74 — so that a `DebateHalted` propagating out of `_phase_*` skips it.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 214 passed.

- [ ] **Step 6: Commit**

```bash
git add debatelab/orchestrator.py tests/test_orchestrator.py
git commit -m "feat: record run config and phase boundaries in the transcript

roster_changed fires only when the roster differs from a previous run, so a
first run recorded the roster nowhere — and the roster is the denominator
check_consensus divides by. run_config fires every run.

Nothing marked a phase boundary, so a phase that raised DebateHalted was
indistinguishable from one that finished. A probe fold over the four
committed debates reproduced 31/32 derivable keys; the one divergence was a
halted propose phase read as completed. phase_started additionally carries
the round, which state[\"round\"] takes before the phase runs and which a
halted debate would otherwise lose."
```

---

### Task 3: The pure fold

**Files:**
- Create: `debatelab/replay.py`
- Test: `tests/test_replay.py` (create)

**Interfaces:**
- Consumes: Tasks 1–2's events
- Produces:
  - `replay.replay(events: list[dict]) -> dict` — the fourteen-key state
  - `replay.MissingGenesis`, `replay.UnknownEvent` — both subclass `Exception`
  - `replay.AUDIT_ONLY: frozenset[str]` — the six types that never change state

- [ ] **Step 1: Write the failing tests**

Create `tests/test_replay.py`:

```python
import pytest

from debatelab import replay
from debatelab.replay import MissingGenesis, UnknownEvent


def genesis(**over):
    e = {"round": 0, "phase": "create", "agent": None,
         "type": "debate_created", "content": "T", "id": "d1", "title": "T",
         "max_rounds": 5, "quorum": "2/3"}
    return {**e, **over}


def ev(type_, **kw):
    return {"round": 1, "phase": "propose", "agent": None, "type": type_,
            "content": "", **kw}


def test_a_transcript_without_genesis_cannot_be_replayed():
    """Pre-genesis debates are refused, not guessed at."""
    with pytest.raises(MissingGenesis):
        replay.replay([ev("proposal", agent="a", content="x")])


def test_an_empty_transcript_cannot_be_replayed():
    with pytest.raises(MissingGenesis):
        replay.replay([])


def test_genesis_alone_reproduces_the_state_create_writes():
    state = replay.replay([genesis()])
    assert state["id"] == "d1"
    assert state["title"] == "T"
    assert state["max_rounds"] == 5
    assert state["quorum"] == "2/3"
    assert state["status"] == "created"
    assert state["round"] == 0
    assert state["roster"] is None
    assert state["last_completed_phase"] is None


def test_defaults_come_off_the_event_not_from_an_import():
    """A debate created when the default was 5 must still replay as 5 after
    someone changes the default to 10."""
    state = replay.replay([genesis(max_rounds=99, quorum="1/2")])
    assert state["max_rounds"] == 99
    assert state["quorum"] == "1/2"


def test_run_config_sets_the_roster_and_marks_the_debate_running():
    state = replay.replay([
        genesis(),
        ev("run_config", phase="run", roster=["a", "b"], max_rounds=5,
           quorum="2/3"),
    ])
    assert state["roster"] == ["a", "b"]
    assert state["status"] == "running"


def test_run_config_overrides_the_creation_defaults():
    state = replay.replay([
        genesis(max_rounds=5, quorum="2/3"),
        ev("run_config", phase="run", roster=["a"], max_rounds=3,
           quorum="1/2"),
    ])
    assert state["max_rounds"] == 3
    assert state["quorum"] == "1/2"


def test_phase_started_advances_the_round():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=2, phase="critique"),
    ])
    assert state["round"] == 2


def test_phase_completed_sets_last_completed_phase():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("phase_completed", phase="propose"),
    ])
    assert state["last_completed_phase"] == "propose"


def test_a_started_but_uncompleted_phase_leaves_last_completed_alone():
    """The divergence that motivated the marker events: a halted propose
    phase must not read as a completed one."""
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("abstained", agent="a", content="boom"),
        ev("error", phase="end", content="only 1 agent responded"),
    ])
    assert state["last_completed_phase"] is None
    assert state["round"] == 1
    assert state["status"] == "error"


def test_proposal_and_revision_both_write_proposals_last_wins():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="first"),
        ev("phase_started", phase="revise"),
        ev("revision", agent="a", content="revised"),
    ])
    assert state["proposals"] == {"a": "revised"}


def test_an_agent_that_skips_revise_keeps_its_proposal():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="a1"),
        ev("proposal", agent="b", content="b1"),
        ev("phase_started", phase="revise"),
        ev("revision", agent="a", content="a2"),
    ])
    assert state["proposals"] == {"a": "a2", "b": "b1"}


def test_critiques_reset_at_each_critique_phase():
    """orchestrator.py:212 assigns state["critiques"] = results — replace, not
    merge. An agent that critiqued in round 1 and abstained in round 2 must
    vanish; merging would keep it."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="critique"),
        ev("critique", round=1, phase="critique", agent="a", content="c1"),
        ev("critique", round=1, phase="critique", agent="b", content="c2"),
        ev("phase_started", round=2, phase="critique"),
        ev("critique", round=2, phase="critique", agent="a", content="c3"),
    ])
    assert state["critiques"] == {"a": "c3"}


def test_votes_reset_at_each_vote_phase():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("vote", round=1, phase="vote", agent="a", verdict="accept",
           content="yes"),
        ev("phase_started", round=2, phase="vote"),
        ev("vote", round=2, phase="vote", agent="b", verdict="reject",
           content="no"),
    ])
    assert state["votes"] == {"b": {"vote": "reject", "reason": "no"}}


def test_abstained_resets_every_phase_not_every_round():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="propose"),
        ev("abstained", agent="a", content="boom"),
        ev("phase_started", round=1, phase="critique"),
    ])
    assert state["abstained"] == []


def test_abstained_unions_across_the_vote_phases_two_fanouts():
    """_phase_vote fans out twice (nominate, then vote) inside one phase, and
    both append to the same list."""
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="vote"),
        ev("abstained", phase="vote", agent="b", content="x"),
        ev("abstained", phase="vote", agent="a", content="y"),
    ])
    assert state["abstained"] == ["a", "b"]


def test_candidate_and_terminal_statuses():
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="vote"),
        ev("candidate", phase="vote", agent="a", content="the answer"),
        ev("consensus", phase="vote", agent="a", content="the answer"),
    ])
    assert state["candidate"] == {"agent": "a", "text": "the answer"}
    assert state["status"] == "awaiting_human"


def test_no_consensus_sets_its_status():
    state = replay.replay([genesis(), ev("no_consensus", phase="end")])
    assert state["status"] == "no_consensus"


def test_human_decision_sets_the_decision_and_the_status():
    state = replay.replay([
        genesis(),
        ev("human_decision", phase="human", agent="human",
           content="approved", note="ship it"),
    ])
    assert state["human_decision"] == {"decision": "approved",
                                       "note": "ship it"}
    assert state["status"] == "approved"


def test_audit_only_events_never_change_state():
    """agent_call is the reliability cycle's telemetry; nomination_dropped
    records a self-nomination. Real events, but none of them touches
    state.json, so none may touch replay's output."""
    base = replay.replay([genesis()])
    noisy = replay.replay([genesis()] + [
        ev(t, agent="a", content="noise") for t in replay.AUDIT_ONLY
    ])
    assert noisy == base


def test_duplicate_events_last_write_wins():
    """A crash mid-phase re-runs that phase on resume and re-appends its
    events — the wart the README already documents."""
    state = replay.replay([
        genesis(),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="first try"),
        ev("phase_started", phase="propose"),
        ev("proposal", agent="a", content="second try"),
    ])
    assert state["proposals"] == {"a": "second try"}


def test_an_unmodelled_event_type_raises():
    """The forcing function: the next person to add an event type must say
    whether it folds, rather than have replay drift silently out of sync."""
    with pytest.raises(UnknownEvent, match="synthesis"):
        replay.replay([genesis(), ev("synthesis", agent="a")])
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_replay.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.replay'`

- [ ] **Step 3: Implement the fold**

Create `debatelab/replay.py`:

```python
"""Fold a debate transcript into the state it implies.

Pure in the sense that matters: no files, no network, no clock, no store, no
debate knowledge beyond the event vocabulary. It imports nothing from
debatelab at all — it is a fold over plain dicts, which is what lets the
whole suite test it without a filesystem.

This is deliberately an INDEPENDENT reimplementation of the state updates in
Orchestrator, not a refactor that shares them. If both sides ran the same
code, `debate fsck` would compare state.json against the logic that wrote it
— able to catch a torn write, never a fold bug, which is the class of error
this module introduces. The duplication is the feature; fsck is the
differential test between the two. tests/test_replay_differential.py is what
keeps them honest.
"""


class MissingGenesis(Exception):
    """The transcript has no debate_created event: a pre-genesis debate."""


class UnknownEvent(Exception):
    """An event type with no fold rule and no audit-only exemption."""


# Real events that carry real information and change no state.json key, so
# they must change nothing here either.
AUDIT_ONLY = frozenset({
    "agent_call",
    "nomination",
    "nomination_retry",
    "nomination_dropped",
    "fallback_candidate",
    "roster_changed",
})


def _initial():
    """The shape store.create() writes. id/title/max_rounds/quorum are None
    only until the mandatory genesis event fills them."""
    return {
        "id": None,
        "title": None,
        "status": "created",
        "round": 0,
        "max_rounds": None,
        "quorum": None,
        "roster": None,
        "last_completed_phase": None,
        "proposals": {},
        "critiques": {},
        "candidate": None,
        "votes": {},
        "abstained": [],
        "human_decision": None,
    }


def _debate_created(st, e):
    st["id"] = e["id"]
    st["title"] = e["title"]
    # Off the event, never imported: a default that changed since this debate
    # was created must not rewrite what this debate actually ran with.
    st["max_rounds"] = e["max_rounds"]
    st["quorum"] = e["quorum"]


def _run_config(st, e):
    st["roster"] = list(e["roster"])
    st["max_rounds"] = e["max_rounds"]
    st["quorum"] = e["quorum"]
    st["status"] = "running"          # mirrors orchestrator.py:50


def _phase_started(st, e):
    st["round"] = e["round"]          # mirrors orchestrator.py:67
    st["abstained"] = []              # mirrors orchestrator.py:68
    # Replace, not merge: orchestrator.py:212 and :326 assign wholesale, so an
    # agent that participated last round and abstained this one must vanish.
    if e["phase"] == "critique":
        st["critiques"] = {}
    elif e["phase"] == "vote":
        st["votes"] = {}


def _phase_completed(st, e):
    st["last_completed_phase"] = e["phase"]   # mirrors orchestrator.py:71


def _proposal(st, e):
    # propose runs exactly once per debate (protocol.next_phase returns to
    # critique after vote, never to propose), so :190's replace and :225's
    # merge coincide: last write wins, and an agent that skips revise keeps
    # its proposal.
    st["proposals"][e["agent"]] = e["content"]


def _critique(st, e):
    st["critiques"][e["agent"]] = e["content"]


def _candidate(st, e):
    st["candidate"] = {"agent": e["agent"], "text": e["content"]}


def _vote(st, e):
    st["votes"][e["agent"]] = {"vote": e["verdict"], "reason": e["content"]}


def _abstained(st, e):
    st["abstained"] = sorted(set(st["abstained"]) | {e["agent"]})


def _human_decision(st, e):
    st["human_decision"] = {"decision": e["content"], "note": e.get("note", "")}
    st["status"] = e["content"]


def _status_setter(value):
    def fold(st, e):
        st["status"] = value
    return fold


_FOLD = {
    "debate_created": _debate_created,
    "run_config": _run_config,
    "phase_started": _phase_started,
    "phase_completed": _phase_completed,
    "proposal": _proposal,
    "revision": _proposal,
    "critique": _critique,
    "candidate": _candidate,
    "vote": _vote,
    "abstained": _abstained,
    "consensus": _status_setter("awaiting_human"),
    "no_consensus": _status_setter("no_consensus"),
    "error": _status_setter("error"),
    "human_decision": _human_decision,
}


def replay(events):
    """Fold a transcript into the state it implies.

    Raises MissingGenesis if the transcript does not open with a
    debate_created event, and UnknownEvent for any type that is neither
    folded nor exempted as audit-only.
    """
    if not events or events[0].get("type") != "debate_created":
        raise MissingGenesis(
            "transcript does not open with a debate_created event: "
            "pre-genesis debate"
        )
    st = _initial()
    for e in events:
        kind = e.get("type")
        if kind in AUDIT_ONLY:
            continue
        fold = _FOLD.get(kind)
        if fold is None:
            raise UnknownEvent(f"no fold rule for event type {kind!r}")
        fold(st, e)
    return st
```

- [ ] **Step 4: Run the replay tests**

Run: `.venv/bin/python -m pytest tests/test_replay.py -q`
Expected: PASS — 21 passed.

- [ ] **Step 5: Verify the fold table covers the whole vocabulary**

Run:

```bash
.venv/bin/python -c "
from debatelab import replay
n = len(replay._FOLD) + len(replay.AUDIT_ONLY)
print('fold:', len(replay._FOLD), 'audit:', len(replay.AUDIT_ONLY), 'total:', n)
assert len(replay._FOLD) == 14, len(replay._FOLD)
assert len(replay.AUDIT_ONLY) == 6, len(replay.AUDIT_ONLY)
assert n == 20, n
assert not (set(replay._FOLD) & replay.AUDIT_ONLY), 'a type is both folded and exempt'
print('vocabulary complete')"
```

Expected: `fold: 14 audit: 6 total: 20` then `vocabulary complete`.

- [ ] **Step 6: Verify the module stayed pure**

Run:

```bash
.venv/bin/python -c "import ast,sys; tree=ast.parse(open('debatelab/replay.py').read()); mods=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]+[a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]; banned=[m for m in mods if any(b in m for b in ('store','orchestrator','prompts','cli'))]; print('IMPORTS:',mods); print('BANNED:',banned) or sys.exit(1 if banned else 0)"
```

Expected: `IMPORTS: []`, `BANNED: []`, exit 0.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 235 passed.

- [ ] **Step 8: Commit**

```bash
git add debatelab/replay.py tests/test_replay.py
git commit -m "feat: add a pure transcript replay fold

replay(events) -> state folds a transcript into the state it implies: 14 of
the 20 event types fold, 6 are audit-only and provably change nothing. It
imports nothing from debatelab, so the suite tests it without a filesystem.

It is deliberately an independent reimplementation of Orchestrator's state
updates rather than a shared refactor. Sharing the fold would reduce fsck to
comparing state.json against the logic that wrote it — able to catch a torn
write, never a fold bug. The duplication is the feature.

Unknown event types raise rather than being ignored: that is the forcing
function that keeps the two implementations in sync, applied when the
decision is being made rather than after fsck has quietly become a lie."
```

---

### Task 4: The differential test

**Files:**
- Modify: `tests/conftest.py` (move `make_store`/`happy_agent` here)
- Modify: `tests/test_orchestrator.py:1-24` (import the moved helpers)
- Test: `tests/test_replay_differential.py` (create)

**Interfaces:**
- Consumes: Tasks 1–3
- Produces: `conftest.make_store(tmp_path)`, `conftest.happy_agent(name, nominee="a")` — shared by `test_orchestrator.py` and `test_replay_differential.py`.

This is the load-bearing test of the whole plan. Tasks 1–3 can each pass in isolation while `replay` and `Orchestrator` still disagree; only this task proves they don't. It is `fsck` run in process.

- [ ] **Step 1: Move the shared helpers to `conftest.py`**

`tests/conftest.py` already exports `MockAgent`, and `tests/test_orchestrator.py` already does `from .conftest import MockAgent` — so this follows the established pattern rather than inventing one.

Add `DebateStore` to `tests/conftest.py`'s import block:

```python
import pytest

from debatelab import orchestrator
from debatelab.agents import models
from debatelab.agents.base import Agent, AgentError
from debatelab.store import DebateStore
```

Append to `tests/conftest.py`:

```python
def make_store(tmp_path):
    return DebateStore(tmp_path / "debates")


def happy_agent(name, nominee="a"):
    return MockAgent(name, [
        f"proposal from {name}",
        f"critique from {name}",
        f"revised proposal from {name}",
        f"NOMINATE: {nominee}\nbest one",
        "VOTE: accept\nagreed",
    ])
```

In `tests/test_orchestrator.py`, delete the local `make_store` and `happy_agent` function definitions and change the import line to:

```python
from .conftest import MockAgent, happy_agent, make_store
```

Leave that file's `from debatelab.store import DebateStore` in place — ten other tests construct a `DebateStore` directly, so it does not become unused. `DeadAgent` stays where it is; Task 2's tests use it there.

- [ ] **Step 2: Run the suite to confirm the move broke nothing**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 235 passed. A pure move; no count change.

- [ ] **Step 3: Write the differential tests**

Create `tests/test_replay_differential.py`:

```python
"""replay must agree with Orchestrator on every debate shape.

Tasks 1-3 can each pass while the two implementations still disagree. This
file is what proves they don't, and it is the reason the deliberate
duplication in replay.py is affordable. It is `debate fsck` run in process.
"""
from fractions import Fraction

import pytest

from debatelab import cli
from debatelab.agents.base import AgentError, ErrorKind
from debatelab.orchestrator import Orchestrator
from debatelab.replay import replay

from .conftest import MockAgent, happy_agent, make_store


def assert_agrees(store, did):
    __tracebackhide__ = True
    expected = replay(store.read_events(did))
    actual = store.read_state(did)
    diffs = {
        k: (actual.get(k), expected.get(k))
        for k in set(expected) | set(actual)
        if expected.get(k) != actual.get(k)
    }
    assert not diffs, f"replay disagrees with state.json on {sorted(diffs)}"


def test_agrees_after_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "awaiting_human"
    assert_agrees(store, did)


def test_agrees_after_no_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        MockAgent("b", [
            "proposal from b", "critique from b", "revised proposal from b",
            "NOMINATE: a\nbest one", "VOTE: reject\nnot yet",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "no_consensus"
    assert_agrees(store, did)


def test_agrees_after_a_halt(tmp_path):
    """The shape that produced the one probe divergence: propose halts, so no
    phase ever completes and last_completed_phase must stay None."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    dead = [MockAgent("b", []), MockAgent("c", [])]
    Orchestrator(store, [happy_agent("a"), *dead]).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["status"] == "error"
    assert state["last_completed_phase"] is None
    assert state["round"] == 1
    assert_agrees(store, did)


def test_agrees_after_approve(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    cli.main(["approve", did, "-m", "ship it"])
    assert store.read_state(did)["status"] == "approved"
    assert_agrees(store, did)


def test_agrees_after_reject(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    cli.main(["reject", did, "-m", "not good enough"])
    assert store.read_state(did)["status"] == "rejected"
    assert_agrees(store, did)


def test_agrees_with_retries_and_abstentions(tmp_path):
    """agent_call events from the reliability cycle are audit-only and must
    not perturb the fold; an exhausted agent still abstains."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 429", kind=ErrorKind.RATE_LIMIT),
            "proposal from c", "critique from c", "revised proposal from c",
            "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert any(
        e["type"] == "agent_call" for e in store.read_events(did)
    ), "expected telemetry in the transcript"
    assert_agrees(store, did)


def test_agrees_across_rounds_when_an_agent_drops_out(tmp_path):
    """The reset rule: an agent that critiques in round 1 and abstains in
    round 2 must vanish from critiques. A merging fold would keep it, and
    would diverge only here."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b", "VOTE: reject\nno",
            "crit a2", "rev a2", "NOMINATE: b", "VOTE: accept\nyes",
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: accept\nyes",
            "crit b2", "rev b2", "NOMINATE: a", "VOTE: accept\nyes",
        ]),
        MockAgent("c", [
            "prop c", "crit c", "rev c", "NOMINATE: a", "VOTE: accept\nyes",
            # nothing left: c abstains from round 2 onward
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=2)
    assert store.read_state(did)["round"] == 2
    assert_agrees(store, did)


def test_agrees_when_a_self_nomination_is_dropped(tmp_path):
    """nomination_dropped is audit-only; the fold must not react to it."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a", nominee="a"), happy_agent("b", nominee="a")]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert any(
        e["type"] == "nomination_dropped" for e in store.read_events(did)
    ), "expected a self-nomination to be dropped"
    assert_agrees(store, did)
```

- [ ] **Step 4: Run the differential tests**

Run: `.venv/bin/python -m pytest tests/test_replay_differential.py -q`
Expected: PASS — 8 passed. If any test reports `replay disagrees with state.json on [...]`, **do not adjust the assertion** — the named key is a real fold bug in `replay.py` or a missing event in `orchestrator.py`. Fix the source.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q --durations=5`
Expected: PASS — 243 passed, still single-digit seconds.

- [ ] **Step 6: Commit**

```bash
git add tests/conftest.py tests/test_orchestrator.py tests/test_replay_differential.py
git commit -m "test: prove replay agrees with the orchestrator on every shape

replay reimplements Orchestrator's state updates on purpose, so that fsck is
a differential test rather than a tautology. That trade is only affordable
if something keeps the two in sync; this is that something.

Drives the real Orchestrator through consensus, no_consensus, a halt,
approve, reject, retries with abstentions, a multi-round drop-out, and a
dropped self-nomination, asserting replay(events) == state.json for each.
The multi-round drop-out is the one that catches a merging fold, which
diverges nowhere else."
```

---

### Task 5: `debate fsck <id>`

**Files:**
- Modify: `debatelab/cli.py:1-11` (imports), add `cmd_fsck`, add the subparser
- Test: `tests/test_cli.py` (append)

**Interfaces:**
- Consumes: Task 3's `replay`/`MissingGenesis`, Tasks 1–2's events
- Produces: `debate fsck <id>` — exit 0 `ok`, exit 1 `diverged`, exit 3 `unverifiable`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cli.py`'s import block (it already imports `json` and `pytest`):

```python
from debatelab.orchestrator import Orchestrator

from .conftest import happy_agent, make_store
```

Append to `tests/test_cli.py`:

```python
def _run_a_debate(workdir):
    store = make_store(workdir)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    return store, did


def test_fsck_reports_ok_on_a_healthy_debate(workdir, capsys):
    _store, did = _run_a_debate(workdir)
    cli.main(["fsck", did])
    assert capsys.readouterr().out.strip() == f"{did}: ok"


def test_fsck_reports_ok_on_a_created_but_unrun_debate(workdir, capsys):
    """No boundary event exists, so the prefix is genesis alone — compared
    against exactly what create() wrote. A real check, not a vacuous one."""
    cli.main(["new", "Pick a database"])
    did = capsys.readouterr().out.strip()
    cli.main(["fsck", did])
    assert capsys.readouterr().out.strip() == f"{did}: ok"


def test_fsck_notes_events_in_flight_after_the_last_checkpoint(
    workdir, capsys
):
    """state.json is the last checkpoint, not the latest truth. A hard crash
    leaves events past it, and reporting that as divergence would cry wolf on
    exactly the debates fsck exists to inspect."""
    store, did = _run_a_debate(workdir)
    store.append_event(did, {
        "round": 1, "phase": "propose", "agent": "a", "type": "agent_call",
        "task": "deep", "attempt": 1, "duration_ms": 5, "ok": True,
        "content": "",
    })
    cli.main(["fsck", did])
    out = capsys.readouterr().out.strip()
    assert out.startswith(f"{did}: ok")
    assert "1 event in flight" in out


def test_fsck_reports_diverged_and_names_the_key(workdir, capsys):
    store, did = _run_a_debate(workdir)
    state = store.read_state(did)
    state["status"] = "approved"          # a lie the transcript does not tell
    store.write_state(did, state)
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", did])
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert f"{did}: diverged" in out
    assert "status" in out
    assert "awaiting_human" in out


def test_fsck_reports_unverifiable_on_a_pre_genesis_debate(workdir, capsys):
    """The four committed debates predate genesis events. They are refused,
    not guessed at, and not migrated."""
    store, did = _run_a_debate(workdir)
    events = store.read_events(did)[1:]           # strip debate_created
    path = store.path(did) / "transcript.jsonl"
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", did])
    assert exc.value.code == 3
    assert "unverifiable" in capsys.readouterr().out


def test_fsck_on_a_missing_debate_fails_cleanly(workdir):
    with pytest.raises(SystemExit) as exc:
        cli.main(["fsck", "no-such-debate"])
    assert exc.value.code != 0
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: FAIL — `SystemExit: 2` / `argument cmd: invalid choice: 'fsck'`.

- [ ] **Step 3: Implement `cmd_fsck`**

In `debatelab/cli.py`, replace the import block (lines 1-11):

```python
"""The `debate` command-line interface."""
import argparse
import functools
import http.server
import json
import sys
from fractions import Fraction
from importlib import resources
from pathlib import Path

from . import replay as replay_mod
from .agents import models, registry
from .store import DebateStore, LockError, render_summary
```

Two additions: `json` (which `_brief` needs, and which `cli.py` did not import) and the module alias `replay_mod`, which avoids shadowing the `replay` function the module exposes.

Add `cmd_fsck` immediately after `cmd_show` (above `cmd_agents`):

```python
# The events after which run() checkpoints: state.json equals the replay of
# the transcript up to the last of these, and nothing later.
_BOUNDARY_TYPES = frozenset({
    "phase_completed", "consensus", "no_consensus", "error", "human_decision",
})


def _brief(value, limit=70):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return text if len(text) <= limit else text[:limit] + "..."


def cmd_fsck(args):
    """Check state.json against a replay of the transcript.

    state.json is the last checkpoint, not the latest truth: _checkpoint runs
    at phase boundaries, while per-agent events land as futures complete. A
    hard crash therefore leaves events after the last checkpoint, and those
    are SUPPOSED to be absent from state.json. Comparing a total replay
    against it would report divergence on exactly the crashed debates this
    command exists to inspect — so compare the prefix up to the last
    boundary, and report the rest as in flight.
    """
    store = get_store()
    events = store.read_events(args.id)
    state = store.read_state(args.id)
    boundary = 0
    for i, event in enumerate(events):
        if event.get("type") in _BOUNDARY_TYPES:
            boundary = i
    try:
        expected = replay_mod.replay(events[: boundary + 1])
    except replay_mod.MissingGenesis as e:
        print(f"{args.id}: unverifiable — {e}")
        sys.exit(3)
    in_flight = len(events) - (boundary + 1)
    if expected == state:
        note = ""
        if in_flight:
            plural = "s" if in_flight != 1 else ""
            note = (
                f" ({in_flight} event{plural} in flight after the last "
                "checkpoint)"
            )
        print(f"{args.id}: ok{note}")
        return
    print(f"{args.id}: diverged")
    for key in sorted(set(expected) | set(state)):
        if expected.get(key) != state.get(key):
            print(f"  {key}:")
            print(f"    state.json: {_brief(state.get(key))}")
            print(f"    replay    : {_brief(expected.get(key))}")
    sys.exit(1)
```

- [ ] **Step 4: Wire the subparser**

In `main`, add immediately after the `show` subparser (`cli.py:262-264`):

```python
    sp = sub.add_parser(
        "fsck", help="check state.json against a replay of the transcript"
    )
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_fsck)
```

Exit 2 is left to argparse for usage errors, which is why `diverged` is 1 and `unverifiable` is 3.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 249 passed.

- [ ] **Step 6: Commit**

```bash
git add debatelab/cli.py tests/test_cli.py
git commit -m "feat: add debate fsck to check state.json against the transcript

Gives divergence a name, a command, and an exit code: ok (0), diverged (1),
unverifiable (3). Exit 2 stays with argparse for usage errors.

fsck compares the replay of the transcript up to the last checkpoint
boundary, not a total replay. state.json is the last checkpoint, not the
latest truth — per-agent events land as futures complete while _checkpoint
waits for the phase, so a hard crash leaves events past it that are supposed
to be absent. Reporting those as divergence would cry wolf on exactly the
crashed debates this command exists to inspect, and a verification tool that
is wrong on day one never gets trusted again.

Pre-genesis debates report unverifiable rather than being guessed at or
migrated: back-filling genesis from state.json would make fsck verify
state.json against a replay seeded from state.json, and pass unconditionally."
```

---

## Verification

After all five tasks:

```bash
.venv/bin/python -m pytest -q --durations=5
```

Expected: all green, 249 passed, still single-digit seconds.

Confirm `protocol.py` was never touched:

```bash
git diff --stat dc0a0d3..HEAD -- debatelab/protocol.py
```

Expected: empty output.

Confirm `run()` was not rewired to resume from replay — this cycle ships the check, not the switch:

```bash
git diff dc0a0d3..HEAD -- debatelab/orchestrator.py | grep -E "^\+" | grep -c "replay"
```

Expected: `0`.

Confirm the four committed debates still behave as the spec says — `unverifiable`, and untouched:

```bash
for d in debates/*/; do .venv/bin/python -m debatelab.cli fsck "$(basename "$d")"; echo "  exit=$?"; done
git status --porcelain debates/
```

Expected: four `unverifiable` lines, each `exit=3`, and **empty** `git status` output — no legacy transcript was rewritten.

Manual end-to-end check that a new debate is verifiable from its first line:

```bash
.venv/bin/python - <<'PY'
import json, pathlib, tempfile
from debatelab.orchestrator import Orchestrator
from debatelab.replay import replay, MissingGenesis
from debatelab.store import DebateStore
from tests.conftest import happy_agent

with tempfile.TemporaryDirectory() as tmp:
    store = DebateStore(pathlib.Path(tmp) / "debates")
    did = store.create("T", "problem")
    events = store.read_events(did)
    assert events[0]["type"] == "debate_created", events[0]["type"]

    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    events = store.read_events(did)
    state = store.read_state(did)
    assert replay(events) == state, "replay disagrees with the checkpoint"
    assert state["status"] == "awaiting_human", state["status"]

    # The roster is recorded, not guessed: it is the consensus denominator.
    config = next(e for e in events if e["type"] == "run_config")
    assert config["roster"] == ["a", "b"], config["roster"]

    # A pre-genesis transcript is refused rather than guessed at.
    try:
        replay(events[1:])
    except MissingGenesis:
        pass
    else:
        raise AssertionError("replay accepted a transcript with no genesis")

print("transcript replay verified")
PY
```

Expected: `transcript replay verified`.

## Out of scope

Tracked in the spec's "Deferred" section; do **not** build these here: `run()` resuming from replay (the next cycle — `fsck` must first demonstrate on real debates that replay is faithful), `debate rebuild <id>`, sequence numbers and `schema_version` on events, `result.json`/`final.md`, `debate result`, richer `debate status`, context budgeting, prompt anonymization, the synthesis phase, the remaining locks (`index.json`, `approve`/`reject`), and CLI polish. No migration of the four committed debates.
