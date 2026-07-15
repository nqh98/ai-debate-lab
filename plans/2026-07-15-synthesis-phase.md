# Synthesis Phase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the debate produce an answer the debate actually built — the nomination winner drafts a merged answer from every proposal and critique, and the roster votes on that instead of one agent's verbatim text.

**Architecture:** Six tasks. Task 1 adds `synthesize_prompt` and gives the test fixture a synthesis slot, changing no behavior. Task 2 is a pure refactor: `nomination` becomes a real phase, splitting the compound `_phase_vote` in two. Task 3 adds the `synthesize` phase itself — the new call, the two new events, the `synthesized` flag, the carry-forward, and the verbatim fallback. Task 4 carries provenance out through `result.json` and `final.md`. Task 5 does the same for the viewer's hero. Task 6 is the integration test and the README.

**Tech Stack:** Python ≥ 3.10, stdlib + PyYAML only, pytest ≥ 8. Vanilla ES2020 in one HTML file for the viewer, tested under node.

**Spec:** `specs/2026-07-15-synthesis-phase-design.md`

## Global Constraints

- **No new runtime dependencies.** Runtime deps stay PyYAML only. No `tenacity`, no diff library, no markdown library.
- **`protocol.py` stays pure** — no files, no clock, no network, no debate ids. The only change it may receive in this plan is the `PHASES` tuple (Tasks 2 and 3). If a task appears to need more, stop: that is a spec violation.
- **`replay.py` must stay an independent reimplementation.** Never import orchestrator logic into it, never refactor the two to share a fold. The duplication is the feature; `tests/test_replay_differential.py` is what proves they agree. See `replay.py:8-15`.
- **Any task that changes orchestrator state updates must change `replay.py` in the same task**, or `tests/test_replay_differential.py` goes red. These are never separate commits.
- **The synthesize phase may never halt a debate.** `AgentError` is caught; `failed_phase` can never read `"synthesize"`. The halt surface stays fanout-only, under 2 responders (spec §5).
- **An empty or whitespace-only synthesis is a failure**, never a candidate (spec, "Design constraint").
- **Synthesis has no parser and no re-ask.** The reply is the answer. Do not add a marker, do not call `prompts.reask` for it.
- **`revise_prompt` is not modified by any task in this plan.** Its `Changes:` section is written for the agents critiquing it next round (spec §3).
- **State gains no new key.** The only shape change is `candidate` gaining `synthesized: bool`. The carry-forward reuses `proposals` (spec §6).
- Commit messages: conventional style (`feat:`, `fix:`, `refactor:`, `test:`, `docs:`), **no attribution trailers of any kind**.
- All commands run from repo root `/home/bossbaby/Desktop/fix-me/ai-debate-lab`; Python is `.venv/bin/python`.
- Baseline before starting: `.venv/bin/python -m pytest -q` ⇒ **360 passed** in ~7s. The suite must stay single-digit seconds.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `debatelab/prompts.py` | `SYNTHESIS_HEADER`, `synthesize_prompt` | 1 |
| `tests/conftest.py` | `MockAgent.synthesis` slot; `happy_agent` passthrough | 1 |
| `debatelab/protocol.py:12` | `PHASES` — the entire protocol change | 2, 3 |
| `debatelab/orchestrator.py` | `_phase_nominate` / `_phase_synthesize` / `_phase_vote`, `_ask_one`, `_reject_reasons` | 2, 3, 4 |
| `debatelab/replay.py` | `_synthesis` fold, `AUDIT_ONLY`, candidate reset key | 2, 3 |
| `debatelab/result.py` | `synthesized` through `build_result`; provenance in `render_final` | 4 |
| `debatelab/store.py:314-333` | `render_summary`'s two candidate headings | 4 |
| `debatelab/viewer/index.html` | `renderHero` + candidate heading provenance | 5 |
| `README.md:4,48,83-89` | Six-phase protocol; drift fixes in the rewritten paragraph | 6 |

**Why `MockAgent` dispatches synthesis on the prompt instead of the queue:** only the nomination winner is asked to synthesize, so the call is out-of-band relative to the per-agent round-robin the queue models. Appending a sixth response to `happy_agent` would desynchronise every *non*-winner — their vote-phase `ask` would pop the synthesis text and `parse_vote` would fail on it. All 72 `happy_agent` call sites nominate `"a"`, but keying the fixture on `name == nominee` breaks on any roster where `"a"` is absent and the seeded fallback draw elects someone else. Dispatching on `prompts.SYNTHESIS_HEADER` is correct for every roster and leaves all 72 call sites untouched. `prompts.py` already exports `VOTE_REQUIRED` and `NOMINATE_REQUIRED` as shared prompt-contract constants (`prompts.py:6-7`); `SYNTHESIS_HEADER` follows that pattern.

---

### Task 1: `synthesize_prompt` and the fixture's synthesis slot

Pure addition. Nothing calls `synthesize_prompt` yet, so the suite stays at 360 plus the new tests.

**Files:**
- Modify: `debatelab/prompts.py:6-7` (add `SYNTHESIS_HEADER`), append `synthesize_prompt` after `vote_prompt` (`prompts.py:93`)
- Modify: `tests/conftest.py:14-46` (`MockAgent`, `happy_agent`)
- Modify: `tests/test_prompts.py`

**Interfaces:**
- Consumes: `prompts.format_blocks(items: dict[str, str]) -> str` (`prompts.py:10-11`)
- Produces:
  - `prompts.SYNTHESIS_HEADER: str` — the literal `"Proposals to merge:"`. `MockAgent` and every later task detect a synthesis prompt by this substring.
  - `prompts.synthesize_prompt(name: str, problem: str, proposals: dict[str, str], critiques: dict[str, str], reject_reasons: dict[str, str] | None = None) -> str`
  - `MockAgent(name, responses, synthesis=None)` — `synthesis` is `str | Exception | None`; `None` means the default `f"synthesis from {name}"`.
  - `happy_agent(name, nominee="a", synthesis=None)` — passes `synthesis` through.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_prompts.py`:

```python
def test_synthesize_prompt_carries_every_proposal_and_critique():
    p = prompts.synthesize_prompt(
        "alpha",
        "Q",
        {"alpha": "A's idea", "beta": "B's idea"},
        {"alpha": "A's critique", "beta": "B's critique"},
    )
    assert prompts.SYNTHESIS_HEADER in p
    assert "A's idea" in p and "B's idea" in p
    assert "A's critique" in p and "B's critique" in p
    assert "### alpha" in p and "### beta" in p


def test_synthesize_prompt_includes_reject_reasons_when_given():
    p = prompts.synthesize_prompt(
        "alpha", "Q", {"alpha": "A"}, {"beta": "C"}, {"gamma": "too vague"}
    )
    assert "too vague" in p
    p2 = prompts.synthesize_prompt("alpha", "Q", {"alpha": "A"}, {"beta": "C"})
    assert "too vague" not in p2
    assert "rejected" not in p2.lower()


def test_synthesize_prompt_forbids_a_preamble():
    """The reply is published verbatim as the answer, so the prompt must ban
    the "Changes:" section revise_prompt asks for. See spec §3."""
    p = prompts.synthesize_prompt("alpha", "Q", {"alpha": "A"}, {"beta": "C"})
    assert "Changes:" in p
    assert "answer ONLY" in p


def test_synthesize_prompt_asks_for_a_merge_not_a_defence():
    p = prompts.synthesize_prompt("alpha", "Q", {"alpha": "A"}, {"beta": "C"})
    assert "merg" in p.lower()
    assert "Do not simply restate your own proposal." in p


def test_synthesize_prompt_is_detectable_by_its_header():
    """conftest.MockAgent routes on this substring, and no other prompt may
    collide with it."""
    others = [
        prompts.propose_prompt("a", "Q"),
        prompts.critique_prompt("a", "Q", {"b": "B"}),
        prompts.revise_prompt("a", "Q", "own", {"b": "B"}),
        prompts.nominate_prompt("a", "Q", {"a": "A", "b": "B"}, ["a", "b"]),
        prompts.vote_prompt("a", "Q", "b", "B"),
    ]
    for p in others:
        assert prompts.SYNTHESIS_HEADER not in p
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: FAIL — `AttributeError: module 'debatelab.prompts' has no attribute 'SYNTHESIS_HEADER'`

- [ ] **Step 3: Add the constant and the prompt**

In `debatelab/prompts.py`, extend the constants at `prompts.py:6-7`:

```python
VOTE_REQUIRED = "'VOTE: accept' or 'VOTE: reject'"
NOMINATE_REQUIRED = "'NOMINATE: <agent-name>'"
# Part of the prompt's contract, not a test hook: the synthesize call is the
# only one addressed to a single agent, so this header is how a reader (and
# tests/conftest.py's MockAgent) tells a synthesis prompt from the others.
SYNTHESIS_HEADER = "Proposals to merge:"
```

Append after `vote_prompt` (`prompts.py:93`):

```python
def synthesize_prompt(
    name: str,
    problem: str,
    proposals: dict[str, str],
    critiques: dict[str, str],
    reject_reasons: dict[str, str] | None = None,
) -> str:
    """Ask the nomination winner to merge the roster's work into one answer.

    Deliberately not revise_prompt: that hands one agent its OWN proposal and
    asks it to defend it. This hands the winner EVERY proposal and asks for a
    merge. The instruction not to restate its own proposal is load-bearing --
    without it the winner re-emits its proposal and the phase costs a DEEP
    call to reproduce the status quo.

    The reply is published verbatim as the answer, so there is no marker to
    parse and the prompt must ban the "Changes:" preamble revise_prompt asks
    for. See specs/2026-07-15-synthesis-phase-design.md §3.
    """
    extra = ""
    if reject_reasons:
        extra = (
            "\n\nThe roster rejected the previous answer for these reasons:\n"
            + format_blocks(reject_reasons)
        )
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n'
        "The other agents nominated your proposal, so you draft the final "
        "answer.\n\n"
        f"Problem:\n{problem}\n\n"
        f"{SYNTHESIS_HEADER}\n{format_blocks(proposals)}\n\n"
        f"Critiques from all agents:\n{format_blocks(critiques)}{extra}\n\n"
        "Write the single best answer to the problem. Merge the strongest "
        "reasoning from every proposal above and address the critiques. "
        "Do not simply restate your own proposal.\n\n"
        'Reply with the answer ONLY. No preamble, no "Changes:" section, no '
        "commentary about the debate or about what you merged."
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: PASS

- [ ] **Step 5: Give `MockAgent` a synthesis slot**

Replace `tests/conftest.py:14-46` entirely:

```python
class MockAgent(Agent):
    """Scripted agent: each ask() pops the next response. Exception instances
    are raised instead of returned; running out of responses raises AgentError.

    The synthesize call is answered from a dedicated `synthesis` slot rather
    than the queue. Only the nomination winner is asked to synthesize, so the
    call is out-of-band relative to the per-agent round-robin the queue
    models: a sixth queued response would be popped by every NON-winner's
    vote-phase ask and fail to parse as a vote. Routing on the prompt is
    correct for any roster, including one where the seeded fallback draw
    elects an agent nobody nominated.
    """

    def __init__(self, name, responses, synthesis=None):
        super().__init__(name)
        self.responses = list(responses)
        self.synthesis = synthesis
        self.prompts = []
        self.tasks = []

    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        self.prompts.append(prompt)
        self.tasks.append(task)
        if prompts.SYNTHESIS_HEADER in prompt:
            item = self.synthesis
            if item is None:
                item = f"synthesis from {self.name}"
            if isinstance(item, Exception):
                raise item
            return item
        if not self.responses:
            raise AgentError(f"{self.name}: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_store(tmp_path):
    return DebateStore(tmp_path / "debates")


def happy_agent(name, nominee="a", synthesis=None):
    return MockAgent(name, [
        f"proposal from {name}",
        f"critique from {name}",
        f"revised proposal from {name}",
        f"NOMINATE: {nominee}\nbest one",
        "VOTE: accept\nagreed",
    ], synthesis=synthesis)
```

Add the import at `tests/conftest.py:3-6`:

```python
from debatelab import orchestrator, prompts
```

- [ ] **Step 6: Run the full suite — nothing calls synthesis yet, so nothing changes**

Run: `.venv/bin/python -m pytest -q`
Expected: **365 passed** (360 baseline + 5 new prompt tests)

- [ ] **Step 7: Commit**

```bash
git add debatelab/prompts.py tests/conftest.py tests/test_prompts.py
git commit -m "feat: add the synthesis prompt and a fixture slot for it"
```

---

### Task 2: Nomination becomes a phase

Pure refactor: no new behavior, no state shape change. Nominations move from `phase: "vote"` to `phase: "nominate"`, and the compound function splits in two.

**Files:**
- Modify: `debatelab/protocol.py:12` (`PHASES`)
- Modify: `debatelab/orchestrator.py:113-114` (candidate reset key), `:296-390` (split `_phase_vote`)
- Modify: `debatelab/replay.py:81-82` (candidate reset key)
- Modify: `tests/test_protocol.py`, `tests/test_orchestrator.py`, `tests/test_replay.py`

**Interfaces:**
- Consumes: `prompts.nominate_prompt`, `prompts.parse_nomination`, `prompts.NOMINATE_REQUIRED`, `protocol.select_candidate`, `Orchestrator._fanout`, `Orchestrator._reask`
- Produces:
  - `protocol.PHASES == ("propose", "critique", "revise", "nominate", "vote")`
  - `Orchestrator._phase_nominate(debate_id, state, problem) -> None` — sets `state["candidate"] = {"agent", "text"}`, emits `nomination`, `nomination_retry`, `nomination_dropped`, `fallback_candidate`, `candidate`, all under `phase: "nominate"`
  - `Orchestrator._phase_vote(debate_id, state, problem) -> None` — reads `state["candidate"]`, emits `vote` / `abstained` under `phase: "vote"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_protocol.py`:

```python
def test_next_phase_walks_nominate_before_vote():
    assert protocol.next_phase(1, "revise") == (1, "nominate")
    assert protocol.next_phase(1, "nominate") == (1, "vote")


def test_next_phase_still_ends_the_round_on_vote():
    assert protocol.next_phase(1, "vote") == (2, "critique")
    assert protocol.next_phase(0, None) == (1, "propose")
```

Append to `tests/test_orchestrator.py`:

```python
def test_nominations_are_recorded_under_the_nominate_phase(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    events = store.read_events(did)
    for kind in ("nomination", "candidate"):
        phases = {e["phase"] for e in events if e["type"] == kind}
        assert phases == {"nominate"}, f"{kind} should be a nominate event"
    votes = {e["phase"] for e in events if e["type"] == "vote"}
    assert votes == {"vote"}


def test_nominate_and_vote_are_separately_bracketed(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    started = [
        e["phase"] for e in store.read_events(did) if e["type"] == "phase_started"
    ]
    assert started == ["propose", "critique", "revise", "nominate", "vote"]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_protocol.py tests/test_orchestrator.py -q`
Expected: FAIL — `next_phase(1, "revise")` returns `(1, "vote")`, and `started` is missing `"nominate"`.

- [ ] **Step 3: Add `nominate` to `PHASES`**

`debatelab/protocol.py:12`:

```python
PHASES = ("propose", "critique", "revise", "nominate", "vote")
```

`next_phase`'s body is not touched: `vote` remains the round terminator, so `propose` still runs exactly once per debate.

- [ ] **Step 4: Move the candidate reset to `nominate`**

`debatelab/orchestrator.py:113-114`:

```python
                if phase == "nominate":
                    state["candidate"] = None
```

`debatelab/replay.py:81-82`:

```python
    if e["phase"] == "nominate":
        st["candidate"] = None        # mirrors orchestrator.py:113
```

- [ ] **Step 5: Split `_phase_vote`**

Replace `debatelab/orchestrator.py:296-390` entirely:

```python
    def _phase_nominate(self, debate_id, state, problem):
        proposals = state["proposals"]
        names = list(proposals)
        nom_raw = self._fanout(
            debate_id, state, "nominate",
            lambda name: prompts.nominate_prompt(name, problem, proposals, names),
            task=models.FAST,
        )
        nominations = {}
        for name, text in nom_raw.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "nominate", "agent": name,
                "type": "nomination", "content": text,
            })
            nominee = prompts.parse_nomination(text, names)
            if nominee is None:
                nominee, retry_text = self._reask(
                    debate_id,
                    state,
                    "nominate",
                    name,
                    prompts.nominate_prompt(name, problem, proposals, names),
                    lambda t: prompts.parse_nomination(t, names),
                    prompts.NOMINATE_REQUIRED,
                    models.FAST,
                )
                if retry_text is not None:
                    text = retry_text
                    self.store.append_event(debate_id, {
                        "round": state["round"], "phase": "nominate",
                        "agent": name, "type": "nomination_retry",
                        "content": retry_text, "nominee": nominee,
                    })
            if nominee == name:
                self.store.append_event(debate_id, {
                    "round": state["round"], "phase": "nominate", "agent": name,
                    "type": "nomination_dropped", "content": text,
                    "reason": "self-nomination",
                })
                continue
            if nominee:
                nominations[name] = nominee
        order_with_proposals = [n for n in self.order if n in proposals]
        winner, was_fallback = protocol.select_candidate(
            nominations, order_with_proposals, f"{debate_id}:{state['round']}"
        )
        if was_fallback:
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "nominate", "agent": winner,
                "type": "fallback_candidate",
                "content": (
                    "no valid nominations; candidate chosen by seeded draw"
                ),
            })
        state["candidate"] = {"agent": winner, "text": proposals[winner]}
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "nominate", "agent": winner,
            "type": "candidate", "content": proposals[winner],
        })

    def _phase_vote(self, debate_id, state, problem):
        winner = state["candidate"]["agent"]
        candidate_text = state["candidate"]["text"]
        vote_raw = self._fanout(
            debate_id, state, "vote",
            lambda name: prompts.vote_prompt(
                name, problem, winner, candidate_text
            ),
            task=models.FAST,
        )
        votes = {}
        for name, text in vote_raw.items():
            verdict = prompts.parse_vote(text)
            if verdict is None:
                verdict, retry_text = self._reask(
                    debate_id,
                    state,
                    "vote",
                    name,
                    prompts.vote_prompt(name, problem, winner, candidate_text),
                    prompts.parse_vote,
                    prompts.VOTE_REQUIRED,
                    models.FAST,
                )
                if retry_text is not None:
                    text = retry_text
            if verdict is None:
                self._abstain(
                    debate_id, state, "vote", name, text, "unparseable vote"
                )
                continue
            votes[name] = {"vote": verdict, "reason": text}
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "vote", "verdict": verdict, "content": text,
            })
        state["votes"] = votes
```

`_phase_vote` now reads the candidate from state rather than recomputing it. That is the seam Task 3 needs: the synthesize phase rewrites `state["candidate"]["text"]` between the two, and the vote picks it up with no further change.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Some existing tests assert `phase == "vote"` on nomination events; update each to `"nominate"`. Find them with:

Run: `.venv/bin/python -m pytest -q 2>&1 | grep -E '^tests.*(FAILED|assert)' | head`

- [ ] **Step 7: Verify replay still agrees**

Run: `.venv/bin/python -m pytest tests/test_replay_differential.py -q`
Expected: PASS — this is what proves the candidate-reset key moved on both sides.

- [ ] **Step 8: Commit**

```bash
git add debatelab/protocol.py debatelab/orchestrator.py debatelab/replay.py tests/
git commit -m "refactor: make nomination a phase of its own

The compound vote phase ran two fanouts under one phase_started/
phase_completed bracket, and those brackets are the resume granularity: a
halt in the vote fanout re-ran every nomination. Free today, not free once
an expensive call joins the bracket."
```

---

### Task 3: The synthesize phase

**Files:**
- Modify: `debatelab/protocol.py:12` (`PHASES`)
- Modify: `debatelab/orchestrator.py` (`_reject_reasons`, `_ask_one`, `_phase_synthesize`, `_phase_critique`, `_phase_nominate`)
- Modify: `debatelab/replay.py` (`AUDIT_ONLY`, `_candidate`, `_synthesis`, `_FOLD`)
- Modify: `tests/test_orchestrator.py`, `tests/test_replay.py`, `tests/test_replay_differential.py`

**Interfaces:**
- Consumes: `prompts.synthesize_prompt`, `prompts.SYNTHESIS_HEADER`, `retry.call_with_retry`, `Orchestrator._record_call`, `MockAgent(..., synthesis=...)`
- Produces:
  - `protocol.PHASES == ("propose", "critique", "revise", "nominate", "synthesize", "vote")`
  - `Orchestrator._ask_one(debate_id, state, phase, name, prompt, task) -> tuple[str | None, str | None]` — returns `(text, None)` or `(None, error_message)`
  - `Orchestrator._reject_reasons(state) -> dict[str, str]` — static
  - `Orchestrator._phase_synthesize(debate_id, state, problem) -> None`
  - `state["candidate"] == {"agent": str, "text": str, "synthesized": bool}`
  - Events `synthesis` (folds) and `synthesis_failed` (audit-only)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orchestrator.py`:

```python
def _run_one_round(tmp_path, agents):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, agents).run(did, max_rounds=1)
    return store, did


def test_the_candidate_is_the_synthesis_not_a_proposal(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a"), happy_agent("b"), happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["candidate"] == {
        "agent": "a", "text": "synthesis from a", "synthesized": True,
    }
    synth = [e for e in store.read_events(did) if e["type"] == "synthesis"]
    assert len(synth) == 1
    assert synth[0]["phase"] == "synthesize"
    assert synth[0]["agent"] == "a"
    assert synth[0]["content"] == "synthesis from a"


def test_the_synthesis_carries_forward_as_the_winners_proposal(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a"), happy_agent("b"), happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["proposals"]["a"] == "synthesis from a"
    assert state["proposals"]["b"] == "revised proposal from b"


def test_only_the_winner_is_asked_to_synthesize(tmp_path):
    agents = [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    _run_one_round(tmp_path, agents)
    asked = {
        ag.name: sum(1 for p in ag.prompts if prompts.SYNTHESIS_HEADER in p)
        for ag in agents
    }
    assert asked == {"a": 1, "b": 0, "c": 0}


def test_the_vote_is_cast_on_the_synthesis(tmp_path):
    agents = [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    _run_one_round(tmp_path, agents)
    vote_prompts = [
        p for ag in agents for p in ag.prompts if "VOTE: accept" in p
    ]
    assert vote_prompts
    for p in vote_prompts:
        assert "synthesis from a" in p
        assert "revised proposal from a" not in p


def test_synthesis_error_falls_back_to_the_verbatim_proposal(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a", synthesis=AgentError("boom")),
        happy_agent("b"),
        happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["candidate"] == {
        "agent": "a", "text": "revised proposal from a", "synthesized": False,
    }
    assert state["proposals"]["a"] == "revised proposal from a"
    failed = [e for e in store.read_events(did) if e["type"] == "synthesis_failed"]
    assert len(failed) == 1
    assert failed[0]["reason"] == "agent_error"
    assert failed[0]["agent"] == "a"
    assert state["status"] == "awaiting_human", "the vote must still happen"


def test_synthesis_empty_reply_falls_back(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a", synthesis="   \n  "),
        happy_agent("b"),
        happy_agent("c"),
    ])
    state = store.read_state(did)
    assert state["candidate"]["text"] == "revised proposal from a"
    assert state["candidate"]["synthesized"] is False
    failed = [e for e in store.read_events(did) if e["type"] == "synthesis_failed"]
    assert [e["reason"] for e in failed] == ["empty"]


def test_synthesis_failure_never_halts_the_debate(tmp_path):
    store, did = _run_one_round(tmp_path, [
        happy_agent("a", synthesis=AgentError("boom")),
        happy_agent("b"),
        happy_agent("c"),
    ])
    events = store.read_events(did)
    assert not [e for e in events if e["type"] == "error"]
    assert store.read_state(did)["status"] != "error"


def test_a_pre_synthesis_checkpoint_resumes_into_nominate(tmp_path):
    """spec §9: a state.json written before this cycle stops at revise. The
    new protocol runs the superset nominate -> synthesize -> vote rather than
    jumping straight to vote. No migration, no special case."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    state = store.read_state(did)
    state.update({
        "status": "running",
        "round": 1,
        "last_completed_phase": "revise",
        "roster": ["a", "b", "c"],
        "proposals": {"a": "a1", "b": "b1", "c": "c1"},
        "critiques": {"a": "ca", "b": "cb", "c": "cc"},
        "candidate": None,
    })
    store.write_state(did, state)
    agents = [
        MockAgent(n, ["NOMINATE: a\nbest one", "VOTE: accept\nagreed"])
        for n in ("a", "b", "c")
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    started = [
        e["phase"] for e in store.read_events(did) if e["type"] == "phase_started"
    ]
    assert started == ["nominate", "synthesize", "vote"]
    assert store.read_state(did)["candidate"]["synthesized"] is True


def test_synthesize_prompt_gets_reject_reasons_from_the_last_round(tmp_path):
    """Round 2's synthesis must see why round 1's answer was rejected."""
    def rejecting(name):
        return MockAgent(name, [
            f"proposal from {name}", f"critique from {name}",
            f"revised proposal from {name}", "NOMINATE: a\nbest one",
            "VOTE: reject\ntoo vague",
            f"critique from {name}", f"revised proposal from {name}",
            "NOMINATE: a\nbest one", "VOTE: accept\nfine now",
        ])
    a = MockAgent("a", [
        "proposal from a", "critique from a", "revised proposal from a",
        "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        "critique from a", "revised proposal from a",
        "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
    ])
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [a, rejecting("b"), rejecting("c")]).run(did, max_rounds=2)
    synth_prompts = [p for p in a.prompts if prompts.SYNTHESIS_HEADER in p]
    assert len(synth_prompts) == 2
    assert "too vague" in synth_prompts[1]
    assert "too vague" not in synth_prompts[0]
```

Append to `tests/test_replay.py`, reusing that file's existing `genesis()` and `ev()` helpers (`tests/test_replay.py:10-20`):

```python
def test_synthesis_folds_into_the_candidate_and_the_proposals():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a",
           content="a's proposal"),
        ev("phase_completed", round=1, phase="nominate"),
        ev("phase_started", round=1, phase="synthesize"),
        ev("synthesis", round=1, phase="synthesize", agent="a",
           content="merged"),
        ev("phase_completed", round=1, phase="synthesize"),
    ])
    assert state["candidate"] == {
        "agent": "a", "text": "merged", "synthesized": True,
    }
    assert state["proposals"]["a"] == "merged"


def test_synthesis_failed_changes_no_state():
    base = [
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a",
           content="a's proposal"),
        ev("phase_completed", round=1, phase="nominate"),
    ]
    before = replay.replay(base)
    after = replay.replay(base + [
        ev("phase_started", round=1, phase="synthesize"),
        ev("synthesis_failed", round=1, phase="synthesize", agent="a",
           content="boom", reason="agent_error"),
    ])
    assert after["candidate"] == before["candidate"]
    assert after["candidate"]["synthesized"] is False
    assert after["proposals"] == before["proposals"]


def test_the_candidate_event_is_never_marked_synthesized():
    state = replay.replay([
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a",
           content="a's proposal"),
    ])
    assert state["candidate"]["synthesized"] is False


def test_the_candidate_resets_on_nominate_not_vote():
    """The reset moved with the phase split; a vote phase must not clear the
    candidate it is voting on."""
    base = [
        genesis(),
        ev("phase_started", round=1, phase="nominate"),
        ev("candidate", round=1, phase="nominate", agent="a", content="x"),
        ev("phase_completed", round=1, phase="nominate"),
    ]
    assert replay.replay(
        base + [ev("phase_started", round=1, phase="vote")]
    )["candidate"] is not None
    assert replay.replay(
        base + [ev("phase_started", round=2, phase="nominate")]
    )["candidate"] is None
```

Append to `tests/test_replay_differential.py`:

```python
def test_agrees_after_a_synthesised_debate(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    ).run(did, max_rounds=1)
    assert store.read_state(did)["candidate"]["synthesized"] is True
    assert_agrees(store, did)


def test_agrees_after_a_synthesis_fallback(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [
        happy_agent("a", synthesis=AgentError("boom")),
        happy_agent("b"),
        happy_agent("c"),
    ]).run(did, max_rounds=1)
    assert store.read_state(did)["candidate"]["synthesized"] is False
    assert_agrees(store, did)


def test_agrees_across_a_carried_forward_synthesis(tmp_path):
    """Two rounds: round 1's synthesis becomes a's proposal, which round 2
    critiques and revises. The carry-forward is the state update most likely
    to drift between the two implementations."""
    def rejecting(name):
        return MockAgent(name, [
            f"proposal from {name}", f"critique from {name}",
            f"revised proposal from {name}", "NOMINATE: a\nbest one",
            "VOTE: reject\ntoo vague",
            f"critique from {name}", f"revised proposal from {name}",
            "NOMINATE: a\nbest one", "VOTE: accept\nfine now",
        ])
    a = MockAgent("a", [
        "proposal from a", "critique from a", "revised proposal from a",
        "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        "critique from a", "revised proposal from a",
        "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
    ])
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [a, rejecting("b"), rejecting("c")]).run(did, max_rounds=2)
    assert_agrees(store, did)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py tests/test_replay.py tests/test_replay_differential.py -q`
Expected: FAIL — candidate has no `synthesized` key and no synthesis is ever requested.

- [ ] **Step 3: Add `synthesize` to `PHASES`**

`debatelab/protocol.py:12`:

```python
PHASES = ("propose", "critique", "revise", "nominate", "synthesize", "vote")
```

- [ ] **Step 4: Extract `_reject_reasons` and add `_ask_one`**

In `debatelab/orchestrator.py`, add both methods next to `_reask`:

```python
    @staticmethod
    def _reject_reasons(state):
        """Why the roster rejected the previous round's answer."""
        return {
            name: v["reason"]
            for name, v in state.get("votes", {}).items()
            if v["vote"] == "reject"
        }

    def _ask_one(self, debate_id, state, phase, name, prompt, task):
        """Ask a single agent, with retry and telemetry. -> (text, error).

        _fanout raises DebateHalted under 2 responders, a rule that is
        meaningless for a one-agent call: it would halt every synthesis. The
        synthesize phase degrades instead of halting, so the error comes back
        as a value. See specs/2026-07-15-synthesis-phase-design.md §5.
        """
        try:
            text = retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
        except AgentError as e:
            return None, str(e)
        return text, None
```

Rewrite `_phase_critique`'s opening (`orchestrator.py:261-267`) to use the helper:

```python
    def _phase_critique(self, debate_id, state, problem):
        proposals = state["proposals"]
        reject_reasons = self._reject_reasons(state)
```

- [ ] **Step 5: Add `_phase_synthesize`**

Insert between `_phase_nominate` and `_phase_vote` in `debatelab/orchestrator.py`:

```python
    def _phase_synthesize(self, debate_id, state, problem):
        """The nomination winner merges the roster's work into one answer.

        Cannot halt: a synthesis that does not arrive leaves the verbatim
        candidate _phase_nominate already set, which is the protocol this
        tool ran before synthesis existed. See spec §5.
        """
        winner = state["candidate"]["agent"]
        text, error = self._ask_one(
            debate_id, state, "synthesize", winner,
            prompts.synthesize_prompt(
                winner, problem, state["proposals"], state["critiques"],
                self._reject_reasons(state) or None,
            ),
            models.DEEP,
        )
        if error is not None:
            self._synthesis_failed(debate_id, state, winner, error, "agent_error")
            return
        if not text.strip():
            # An unvalidatable reply has exactly two checks: it arrived, and
            # it is not blank. Letting a blank one stand would put an empty
            # answer in front of a human as a real one.
            self._synthesis_failed(debate_id, state, winner, text, "empty")
            return
        state["candidate"] = {
            "agent": winner, "text": text, "synthesized": True,
        }
        state["proposals"] = {**state["proposals"], winner: text}
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "synthesize", "agent": winner,
            "type": "synthesis", "content": text,
        })

    def _synthesis_failed(self, debate_id, state, winner, content, reason):
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "synthesize", "agent": winner,
            "type": "synthesis_failed", "content": content, "reason": reason,
        })
```

- [ ] **Step 6: Mark the pre-synthesis candidate**

In `_phase_nominate`, change the candidate assignment:

```python
        state["candidate"] = {
            "agent": winner, "text": proposals[winner], "synthesized": False,
        }
```

- [ ] **Step 7: Teach `replay` the same three facts**

`debatelab/replay.py` — add to `AUDIT_ONLY` (`replay.py:31-38`):

```python
    "synthesis_failed",
```

Replace `_candidate` (`replay.py:101-102`) and add `_synthesis`:

```python
def _candidate(st, e):
    st["candidate"] = {
        "agent": e["agent"], "text": e["content"], "synthesized": False,
    }


def _synthesis(st, e):
    # Mirrors orchestrator._phase_synthesize: the merge is both the candidate
    # and, from now on, the winner's proposal -- which is what round N+1
    # critiques.
    st["candidate"] = {
        "agent": e["agent"], "text": e["content"], "synthesized": True,
    }
    st["proposals"][e["agent"]] = e["content"]
```

Register it in `_FOLD` (`replay.py:144-159`):

```python
    "synthesis": _synthesis,
```

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS. Existing tests that assert `state["candidate"] == {"agent": ..., "text": ...}` need `"synthesized"` added; existing tests asserting a candidate equals a revised proposal now get the synthesis.

- [ ] **Step 9: Verify the differential suite specifically**

Run: `.venv/bin/python -m pytest tests/test_replay_differential.py -q`
Expected: PASS — the load-bearing check. A failure here means `_phase_synthesize` and `_synthesis` disagree, which is exactly the bug class this file exists to catch.

- [ ] **Step 10: Commit**

```bash
git add debatelab/protocol.py debatelab/orchestrator.py debatelab/replay.py tests/
git commit -m "feat: have the nomination winner synthesize the answer

Four phases produced critique and revision and then voted on one agent's
verbatim proposal; nothing merged anything. The winner now drafts a merged
answer from every proposal and critique, and that carries forward as its
proposal so a rejected answer is what the next round critiques.

Synthesis cannot be parsed -- the reply is the answer -- so the only checks
are that the call returned and the reply is non-empty. Failing either falls
back to the verbatim proposal, which is the protocol that shipped before."
```

---

### Task 4: Provenance through `result.json`, `final.md`, and `summary.md`

**Files:**
- Modify: `debatelab/orchestrator.py:137-145` (the `consensus` event)
- Modify: `debatelab/result.py:20-27`, `:47-53`, `:112-136`
- Modify: `debatelab/store.py:314-333` (`render_summary`'s candidate headings)
- Modify: `tests/test_result.py`, `tests/test_store.py`

**Interfaces:**
- Consumes: the `synthesis` / `candidate` / `consensus` events from Task 3
- Produces:
  - `consensus` event carries `synthesized: bool`
  - `build_result(...)["candidate"] == {"agent": str, "round": int, "synthesized": bool}`
  - `render_final` prints `synthesized by **X**` vs `from **X**`
  - `render_summary` prints the same distinction in both candidate headings

**Why `render_summary` is in this task and not sharing `_credit`:** `summary.md` is the third view that credits a candidate, after `final.md` and the viewer's hero. `result.py` imports nothing and `store.py` does not import `result`, so a shared helper is possible — but the viewer must duplicate the wording in JavaScript regardless, so a `store` → `result` dependency would buy consistency in two of three places at the cost of a new coupling between the I/O module and the projection module. Inline the ternary instead.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_result.py`:

```python
def test_synthesized_survives_consensus_into_the_result():
    result = build_result([
        {"type": "debate_created", "id": "d", "title": "T",
         "max_rounds": 5, "quorum": "2/3"},
        {"type": "candidate", "round": 1, "agent": "a", "content": "verbatim"},
        {"type": "synthesis", "round": 1, "agent": "a", "content": "merged"},
        {"type": "consensus", "round": 1, "agent": "a", "content": "merged",
         "synthesized": True, "tally": {"accepts": 3, "rejects": 0,
         "abstains": 0, "roster_size": 3, "required": 2, "quorum": "2/3"}},
        {"type": "human_decision", "content": "approved", "ts": "2026-07-15T00:00:00",
         "note": "ship"},
    ])
    assert result["candidate"]["synthesized"] is True
    assert result["answer"] == "merged"


def test_a_fallback_candidate_is_not_marked_synthesized():
    result = build_result([
        {"type": "debate_created", "id": "d", "title": "T",
         "max_rounds": 5, "quorum": "2/3"},
        {"type": "candidate", "round": 1, "agent": "a", "content": "verbatim"},
        {"type": "synthesis_failed", "round": 1, "agent": "a",
         "content": "boom", "reason": "agent_error"},
        {"type": "consensus", "round": 1, "agent": "a", "content": "verbatim",
         "synthesized": False, "tally": {"accepts": 3, "rejects": 0,
         "abstains": 0, "roster_size": 3, "required": 2, "quorum": "2/3"}},
    ])
    assert result["candidate"]["synthesized"] is False


def test_render_final_credits_a_synthesis_as_synthesized():
    md = render_final({
        "id": "d", "title": "T", "status": "approved", "answer": "merged",
        "candidate": {"agent": "a", "round": 2, "synthesized": True},
        "tally": None, "decided_at": "2026-07-15T00:00:00", "note": "",
        "reason": None, "round": 2, "failed_phase": None,
    })
    assert "synthesized by **a**, round 2" in md
    assert "from **a**" not in md


def test_render_final_keeps_from_wording_for_a_verbatim_answer():
    md = render_final({
        "id": "d", "title": "T", "status": "approved", "answer": "verbatim",
        "candidate": {"agent": "a", "round": 2, "synthesized": False},
        "tally": None, "decided_at": "2026-07-15T00:00:00", "note": "",
        "reason": None, "round": 2, "failed_phase": None,
    })
    assert "from **a**, round 2" in md
    assert "synthesized" not in md


def test_render_final_credits_a_rejected_synthesis():
    md = render_final({
        "id": "d", "title": "T", "status": "rejected", "answer": None,
        "candidate": {"agent": "a", "round": 1, "synthesized": True},
        "tally": None, "decided_at": "2026-07-15T00:00:00", "note": "no",
        "reason": "no", "round": 1, "failed_phase": None,
    })
    assert "Candidate synthesized by **a** (round 1)" in md


def test_result_defaults_synthesized_on_a_legacy_snapshot():
    """no_consensus snapshots written before this cycle have no flag."""
    result = build_result([
        {"type": "debate_created", "id": "d", "title": "T",
         "max_rounds": 1, "quorum": "2/3"},
        {"type": "no_consensus", "round": 1, "content": "cap reached",
         "tally": {"accepts": 1, "rejects": 0, "abstains": 2,
                   "roster_size": 3, "required": 2, "quorum": "2/3"},
         "candidate": {"agent": "a", "text": "old"}},
    ])
    assert result["candidate"]["synthesized"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_result.py -q`
Expected: FAIL — `KeyError: 'synthesized'`, and `render_final` prints `from **a**` unconditionally.

- [ ] **Step 3: Carry the flag on the `consensus` event**

`debatelab/orchestrator.py:137-145` — add one key to the event dict:

```python
                    self.store.append_event(debate_id, {
                        "round": rnd, "phase": "vote",
                        "agent": state["candidate"]["agent"],
                        "type": "consensus",
                        "content": state["candidate"]["text"],
                        "synthesized": state["candidate"]["synthesized"],
                        "tally": protocol.tally(
                            state["votes"], roster_size, quorum_frac
                        ),
                    })
```

`build_result`'s `consensus` branch rebuilds the candidate dict rather than amending it, so without this key the flag is silently dropped between the `synthesis` event and the result. See spec §8.

- [ ] **Step 4: Read the flag in `build_result`**

`debatelab/result.py:20-27`:

```python
    def set_candidate(snapshot):
        nonlocal candidate, candidate_text
        if snapshot is None:
            candidate = None
            candidate_text = None
            return
        candidate = {
            "agent": snapshot.get("agent"),
            "round": round_,
            "synthesized": snapshot.get("synthesized", False),
        }
        candidate_text = snapshot.get("text")
```

`debatelab/result.py:47-53` — replace the `candidate` branch and add a `synthesis` branch before `consensus`:

```python
        elif event_type == "candidate":
            candidate = {
                "agent": event.get("agent"), "round": event.get("round"),
                "synthesized": False,
            }
            candidate_text = event.get("content")
        elif event_type == "synthesis":
            candidate = {
                "agent": event.get("agent"), "round": event.get("round"),
                "synthesized": True,
            }
            candidate_text = event.get("content")
        elif event_type == "consensus":
            candidate = {
                "agent": event.get("agent"), "round": event.get("round"),
                "synthesized": event.get("synthesized", False),
            }
            candidate_text = event.get("content")
```

Leave the rest of the `consensus` branch (`tally`, `round_`, `decided_at`, `note`, `failed_phase`, `status`, `reason`) exactly as it is.

- [ ] **Step 5: Split the provenance wording**

`debatelab/result.py` — add a helper beside `_tally_text` (`result.py:105-109`):

```python
def _credit(candidate: dict) -> str:
    """How the candidate came to be: a merge of the roster's work, or one
    agent's own text."""
    verb = "synthesized by" if candidate.get("synthesized") else "from"
    return f"{verb} **{candidate['agent']}**"
```

In `render_final`, rewrite the approved provenance (`result.py:114-121`):

```python
    if result["answer"] is not None:
        provenance = (
            f"Approved {result['decided_at']} · {_credit(result['candidate'])}, "
            f"round {result['candidate']['round']}"
        )
```

and the rejected branch (`result.py:126-133`):

```python
        candidate = result["candidate"]
        if candidate is not None:
            message = (
                f"Candidate {_credit(candidate)} (round {candidate['round']}) "
                "was **rejected**"
            )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_result.py -q`
Expected: PASS

- [ ] **Step 7: Make `summary.md` agree**

Add to `tests/test_store.py`:

```python
def test_summary_credits_a_synthesized_candidate():
    md = render_summary({
        "id": "d", "title": "T", "status": "awaiting_human", "round": 1,
        "max_rounds": 5, "quorum": "2/3", "roster": ["a", "b"],
        "last_completed_phase": "vote", "proposals": {}, "critiques": {},
        "candidate": {"agent": "a", "text": "merged", "synthesized": True},
        "votes": {}, "abstained": [], "human_decision": None,
    })
    assert "synthesized by a" in md
    assert "(from a)" not in md


def test_summary_keeps_from_wording_for_a_verbatim_candidate():
    md = render_summary({
        "id": "d", "title": "T", "status": "awaiting_human", "round": 1,
        "max_rounds": 5, "quorum": "2/3", "roster": ["a", "b"],
        "last_completed_phase": "vote", "proposals": {}, "critiques": {},
        "candidate": {"agent": "a", "text": "verbatim", "synthesized": False},
        "votes": {}, "abstained": [], "human_decision": None,
    })
    assert "(from a)" in md
    assert "synthesized" not in md
```

Run: `.venv/bin/python -m pytest tests/test_store.py -q -k summary_credits or summary_keeps`
Expected: FAIL — `render_summary` prints `(from a)` unconditionally.

`debatelab/store.py:314-333` — derive the credit once and use it in both headings:

```python
    decision = state.get("human_decision")
    candidate = state.get("candidate")
    credit = "synthesized by" if (candidate or {}).get("synthesized") else "from"
    if decision:
        lines += ["", f"## Final decision — {decision['decision'].upper()}", ""]
        if candidate:
            lines += [
                f"Candidate {credit} **{candidate['agent']}**:",
                "",
                candidate["text"],
                "",
            ]
        if decision.get("note"):
            lines += [f"> Human note: {decision['note']}", ""]
    elif candidate:
        lines += [
            "",
            f"## Current candidate ({credit} {candidate['agent']}) "
            "— pending human decision",
            "",
            candidate["text"],
```

Leave the remainder of the `elif` block exactly as it is.

Run: `.venv/bin/python -m pytest tests/test_store.py -q`
Expected: PASS

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 9: Commit**

```bash
git add debatelab/orchestrator.py debatelab/result.py debatelab/store.py tests/
git commit -m "feat: credit a synthesized answer to the merge, not the author

final.md said 'from a' whether a wrote the answer or merged everyone
else's work into it. The consensus event has to carry the flag explicitly:
build_result rebuilds the candidate dict from it rather than amending the
one the synthesis event produced."
```

---

### Task 5: The viewer's hero credits the merge

**Files:**
- Modify: `debatelab/viewer/index.html:279-292` (`renderHero`), `:425-427` (candidate heading)
- Modify: `tests/test_viewer_render.py`

**Interfaces:**
- Consumes: `render_js(expr)`, `needs_node` from `tests/test_viewer_render.py` (Task 1 of the viewer cycle); `result.json`'s `candidate.synthesized` from Task 4
- Produces: `renderHero` prints `synthesized by <strong>X</strong>` when `candidate.synthesized`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_viewer_render.py`:

```python
@needs_node
def test_hero_credits_a_synthesized_answer():
    out = render_js("""renderHero({
      answer: "merged", decided_at: "2026-07-15",
      candidate: {agent: "a", round: 2, synthesized: true},
      tally: null, reason: null, round: 2, failed_phase: null
    })""")
    assert "synthesized by <strong>a</strong>" in out
    assert "· from <strong>a</strong>" not in out


@needs_node
def test_hero_keeps_from_wording_for_a_verbatim_answer():
    out = render_js("""renderHero({
      answer: "verbatim", decided_at: "2026-07-15",
      candidate: {agent: "a", round: 2, synthesized: false},
      tally: null, reason: null, round: 2, failed_phase: null
    })""")
    assert "from <strong>a</strong>" in out
    assert "synthesized" not in out


@needs_node
def test_hero_treats_a_missing_flag_as_not_synthesized():
    """The four committed debates have no result.json; a legacy one written
    before this cycle has a candidate with no flag."""
    out = render_js("""renderHero({
      answer: "old", decided_at: "2026-07-15",
      candidate: {agent: "a", round: 1},
      tally: null, reason: null, round: 1, failed_phase: null
    })""")
    assert "from <strong>a</strong>" in out
    assert "synthesized" not in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q -k synthes`
Expected: FAIL — the hero prints `· from <strong>a</strong>` unconditionally.

- [ ] **Step 3: Split the wording in `renderHero`**

`debatelab/viewer/index.html:284-292` — replace the approved branch:

```javascript
  if (result.answer !== null && result.answer !== undefined) {
    const c = result.candidate || {};
    const credit = c.synthesized ? "synthesized by" : "from";
    let prov = `Approved ${esc(String(result.decided_at ?? ""))}` +
      ` · ${credit} <strong>${esc(c.agent)}</strong>, round ${esc(String(c.round))}`;
    if (result.tally) prov += ` · ${tallyText(result.tally)}`;
    return `<div class="card hero"><h2>Answer</h2>` +
      `<div class="md">${renderMarkdown(result.answer)}</div>` +
      `<p class="note">${prov}</p></div>`;
  }
```

- [ ] **Step 4: Match the unapproved candidate heading**

`debatelab/viewer/index.html:425-427` reads `state.json`, whose candidate also carries the flag now:

```javascript
  if (state.candidate) {
    const credit = state.candidate.synthesized ? "synthesized by" : "from";
    html += `<h2>Candidate answer (${credit} ${esc(state.candidate.agent)})</h2>
      <div class="card md">${renderMarkdown(state.candidate.text)}</div>`;
  }
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: PASS (or SKIPPED as a body if node is absent — that is the existing harness's contract)

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py
git commit -m "feat: credit the merge in the viewer's hero panel"
```

---

### Task 6: Integration proof and the README

**Files:**
- Modify: `tests/test_integration.py`
- Modify: `README.md:4`, `:48-51`, `:83-89`

**Interfaces:**
- Consumes: everything from Tasks 1-5
- Produces: no code interface; this task is the end-to-end proof and the docs

- [ ] **Step 1: Widen the test file's imports**

`tests/test_integration.py:1-11` currently imports `json`, `cli`, `registry`, `Orchestrator`, `DebateStore`, and `MockAgent` only. The tests below also need `prompts`, `happy_agent`, and `make_store`:

```python
from debatelab import cli, prompts
from debatelab.agents import registry
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent, happy_agent, make_store
```

- [ ] **Step 2: Write the failing integration tests**

Append to `tests/test_integration.py`:

```python
def test_the_answer_is_a_merge_and_carries_no_changelog(tmp_path):
    """The end-to-end shape of the cycle: the approved answer is the merged
    document, not the winning agent's revision with its diff notes on top."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    a = MockAgent("a", [
        "proposal from a",
        "critique from a",
        "Changes: tightened the intro\nrevised proposal from a",
        "NOMINATE: a\nbest one",
        "VOTE: accept\nagreed",
    ], synthesis="the merged answer")
    Orchestrator(store, [a, happy_agent("b"), happy_agent("c")]).run(
        did, max_rounds=1
    )
    cli.main(["approve", did, "-m", "ship it"])

    final = (store.root / did / "final.md").read_text()
    assert "the merged answer" in final
    assert "Changes:" not in final
    assert "synthesized by **a**" in final

    result = json.loads((store.root / did / "result.json").read_text())
    assert result["answer"] == "the merged answer"
    assert result["candidate"]["synthesized"] is True


def test_a_resumed_debate_does_not_synthesize_twice(tmp_path):
    """The phase split's whole justification: a halt after synthesize must
    not re-run the DEEP call on resume."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    dead_vote = [
        "proposal from {n}", "critique from {n}", "revised proposal from {n}",
        "NOMINATE: a\nbest one",
    ]

    def stops_before_voting(name):
        return MockAgent(name, [r.format(n=name) for r in dead_vote])

    a = MockAgent("a", [r.format(n="a") for r in dead_vote])
    Orchestrator(
        store, [a, stops_before_voting("b"), stops_before_voting("c")]
    ).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "error"
    assert store.read_state(did)["last_completed_phase"] == "synthesize"
    first = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["phase"] == "synthesize"
    ]
    assert len(first) == 1

    a2 = MockAgent("a", ["VOTE: accept\nagreed"])
    Orchestrator(store, [
        a2,
        MockAgent("b", ["VOTE: accept\nagreed"]),
        MockAgent("c", ["VOTE: accept\nagreed"]),
    ]).run(did)
    after = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["phase"] == "synthesize"
    ]
    assert len(after) == 1, "resume re-ran the synthesis"
    assert not [p for p in a2.prompts if prompts.SYNTHESIS_HEADER in p]
    assert store.read_state(did)["status"] == "awaiting_human"
```

`store.root` is `DebateStore`'s debates directory (`store.py:113-114`).

- [ ] **Step 3: Run the tests**

Run: `.venv/bin/python -m pytest tests/test_integration.py -q`
Expected: PASS immediately — Tasks 1-5 already implement this; these tests exist to prove the pieces compose end-to-end, not to drive new code. If either fails, the defect is real and belongs to the task that owns it; fix it there rather than weakening the assertion.

- [ ] **Step 4: Update the README's protocol section**

`README.md:83-89` — replace the paragraph:

```markdown
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
```

This paragraph had drifted from the code in three ways before this cycle, all inside the sentences being rewritten: it documented the **config-order tie-break** that `2026-07-14-protocol-correctness-design.md` replaced with a seeded draw, described consensus as **unanimous** after the same spec replaced it with a quorum, and said failed calls **"retry once"** after `2026-07-15-agent-reliability-design.md` gave them backoff. Fixing them is in scope because the sentences carrying them are being replaced anyway; do not go looking for drift elsewhere in the README.

- [ ] **Step 5: Fix the two stale lines outside that paragraph**

`README.md:4` — the one-line description:

```markdown
problem in a structured debate — propose, critique, revise, nominate,
synthesize, vote — until
```

`README.md:48-49` — "unanimous vote" is the same superseded rule:

```markdown
`run` resumes from the last completed phase if interrupted. If no consensus
is reached within `--max-rounds` (default 5), the debate ends
```

- [ ] **Step 6: Verify the README's claims against the code**

Run: `.venv/bin/python -c "from debatelab.protocol import PHASES; print(PHASES)"`
Expected: `('propose', 'critique', 'revise', 'nominate', 'synthesize', 'vote')`

Run: `grep -n 'config-order\|Unanimous\|unanimous\|retry once' README.md`
Expected: no output

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, single-digit seconds

- [ ] **Step 8: Commit**

```bash
git add tests/test_integration.py README.md
git commit -m "docs: document the six-phase protocol

Also corrects three claims the rewritten paragraph carried: the
config-order tie-break, unanimity, and the single instant retry were all
superseded by earlier cycles."
```

---

## Notes for the reviewer

**Where this plan is most likely to break.** Task 3, and the differential suite is the tell. `_phase_synthesize` and `replay._synthesis` are two independent expressions of the same three state updates (candidate text, `synthesized`, `proposals[winner]`). If they drift, `tests/test_replay_differential.py` fails and the fix belongs in whichever one is wrong — never by making the two share code, which would reduce `debate fsck` to a tautology (`replay.py:8-15`).

**What is deliberately not fixed.** On the fallback path the candidate is a verbatim revision, so `final.md` keeps its `Changes:` preamble. Synthesis makes that rare, not impossible. Closing it means parsing agent prose into sections, which the spec rejects. `test_the_answer_is_a_merge_and_carries_no_changelog` asserts the normal path only, on purpose.

**What this cycle makes worse.** `synthesize_prompt` embeds every proposal *and* every critique, making it the largest prompt in the protocol. Context budgeting was already the next item in the Reliability bucket; it is now more pressing. The spec's "Deferred" section records this.
