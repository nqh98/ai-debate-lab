# Protocol Correctness and Write Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the debate mechanism incapable of reporting a verdict no agent gave, and make its files incapable of being corrupted by a concurrent run or a torn read.

**Architecture:** Seven independent tasks against the existing `debatelab` package. Tasks 1–4 harden the protocol (strict parsers → seeded selection → quorum → re-ask); Tasks 5–7 harden writes (atomic writes → run lock → serve scope). `protocol.py` stays a pure module — the seed and roster size are passed *in*, so it never learns what a debate id or a file is. Every task ends with the full suite green.

**Tech Stack:** Python ≥ 3.10, PyYAML, stdlib only (`fractions`, `random`, `os`, `socket`, `contextlib`, `subprocess`, `http.server`). pytest. Viewer is one static HTML file, vanilla JS, no build step.

**Spec:** `specs/2026-07-14-protocol-correctness-design.md`

## Global Constraints

- Python ≥ 3.10; runtime dependencies: **PyYAML only**. No new dependencies — every mechanism here is stdlib.
- **`protocol.py` must stay pure.** No file, clock, network, or debate-id knowledge. Callers pass `seed: str` and `roster_size: int`.
- **Quorum arithmetic uses `fractions.Fraction`, never floats.** `math.ceil(0.667 * 3) == 3` but `math.ceil(Fraction(2,3) * 3) == 2`. A float silently demands unanimity on the default 3-agent roster. Quorum is stored in `state.json` as the string `"2/3"`.
- **Parsers never guess.** No verdict or nomination may be inferred from prose. Unparseable ⇒ `None` ⇒ (after one re-ask) `abstained`.
- Default quorum **2/3**. Default `max_rounds` **5**.
- Consensus = **zero rejects AND accepts ≥ ceil(quorum × roster_size)**, where roster is the agent set the run started with.
- Transcript event schema: `{ts, round, phase, agent, type, content}`, extra keys allowed (`verdict`, `reason`, `tally`).
- Existing tests **encode the bugs** (`tests/test_prompts.py:50` asserts `parse_vote("I accept this fine answer") == "accept"`). Tasks that change behavior **rewrite those assertions** — never delete a test to make the suite pass.
- Commit messages: conventional style (`feat:`, `fix:`, `test:`), **no attribution trailers of any kind**.
- All commands run from repo root `/home/bossbaby/Desktop/fix-me/ai-debate-lab`; Python is `.venv/bin/python`.
- Baseline before starting: `.venv/bin/python -m pytest -q` ⇒ **117 passed**.

---

### Task 1: Strict parsers, no prose guessing, no self-nomination

**Files:**
- Modify: `debatelab/prompts.py:58-72` (`nominate_prompt`), `debatelab/prompts.py:91-107` (both parsers)
- Modify: `debatelab/orchestrator.py:164-195` (`_phase_vote` call sites), add `_abstain` helper
- Test: `tests/test_prompts.py:26-31,38-53` (rewrite), `tests/test_orchestrator.py` (add)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `prompts.parse_vote(text: str) -> str | None` — `'accept'` / `'reject'` / `None`. **Return type changed from `tuple[str, str]` to `str | None`.**
  - `prompts.parse_nomination(text: str, valid_names: list[str]) -> str | None` — strict only. Callers pass **all** agent names (including the nominator) so the orchestrator can tell a self-nomination from garbage.
  - `Orchestrator._abstain(debate_id, state, phase, name, content, reason)` — records an `abstained` event and adds the agent to `state["abstained"]`.

- [ ] **Step 1: Rewrite the parser tests to assert the correct behavior**

Replace `tests/test_prompts.py:38-53` entirely:

```python
def test_parse_nomination_reads_the_marker_line():
    names = ["alpha", "beta"]
    assert prompts.parse_nomination("NOMINATE: beta\nbecause...", names) == "beta"
    assert prompts.parse_nomination("nominate:   alpha", names) == "alpha"


def test_parse_nomination_never_guesses_from_prose():
    names = ["alpha", "beta"]
    # Regression: the old fallback picked the first name mentioned anywhere,
    # so a sentence calling beta the WORST nominated beta.
    assert prompts.parse_nomination("I think beta's plan is weakest", names) is None
    assert prompts.parse_nomination("no idea", names) is None
    assert prompts.parse_nomination("NOMINATE: gamma", names) is None


def test_parse_nomination_returns_self_so_caller_can_drop_it():
    # valid_names includes the nominator; the orchestrator decides what to do.
    assert prompts.parse_nomination("NOMINATE: alpha", ["alpha", "beta"]) == "alpha"


def test_parse_vote_reads_the_marker_line():
    assert prompts.parse_vote("VOTE: accept\nlooks good") == "accept"
    assert prompts.parse_vote("vote: REJECT\nmissing X") == "reject"


def test_parse_vote_never_infers_a_verdict_from_prose():
    # Regression: the old fallback read any "accept" in the first line as
    # accept, so an explicit refusal was recorded as agreement.
    assert prompts.parse_vote("I cannot accept this") is None
    assert prompts.parse_vote("I do not accept") is None
    assert prompts.parse_vote("I accept this fine answer") is None
    assert prompts.parse_vote("hmm not sure about this") is None
    assert prompts.parse_vote("") is None
```

Replace `tests/test_prompts.py:26-31`:

```python
def test_nominate_prompt_forbids_self_and_excludes_self_from_valid_names():
    p = prompts.nominate_prompt(
        "alpha", "Q", {"alpha": "A", "beta": "B"}, ["alpha", "beta"]
    )
    assert "NOMINATE:" in p
    assert "may NOT nominate your own" in p
    # own proposal still shown as context, but not offered as a valid choice
    assert "### alpha" in p
    assert "Valid agent names: beta" in p
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: FAIL — `parse_vote("I cannot accept this")` returns `("accept", ...)` not `None`; `Valid agent names: beta` not found.

- [ ] **Step 3: Make the parsers strict**

Replace `debatelab/prompts.py:91-107`:

```python
VOTE_REQUIRED = "'VOTE: accept' or 'VOTE: reject'"
NOMINATE_REQUIRED = "'NOMINATE: <agent-name>'"


def parse_nomination(text: str, valid_names: list[str]) -> str | None:
    """Return the nominated agent, or None when no NOMINATE: line names a
    valid agent. Never guesses from prose: a reply calling an agent the
    weakest must not nominate it. `valid_names` includes the nominator, so
    callers can distinguish a self-nomination from an unparseable reply."""
    match = re.search(r'NOMINATE:\s*"?([\w.-]+)', text, re.IGNORECASE)
    if match and match.group(1) in valid_names:
        return match.group(1)
    return None


def parse_vote(text: str) -> str | None:
    """Return 'accept' or 'reject', or None when no VOTE: line is present.
    Never infers a verdict from prose: "I cannot accept" is not an accept."""
    match = re.search(r"VOTE:\s*(accept|reject)", text, re.IGNORECASE)
    return match.group(1).lower() if match else None
```

- [ ] **Step 4: Forbid self-nomination in the prompt**

Replace `debatelab/prompts.py:58-72`:

```python
def nominate_prompt(
    name: str,
    problem: str,
    proposals: dict[str, str],
    names: list[str],
) -> str:
    others = [n for n in names if n != name]
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f"Current proposals:\n{format_blocks(proposals)}\n\n"
        "Which single proposal is closest to correct?\n"
        "You may NOT nominate your own proposal.\n"
        "Reply with exactly one line in this format, then one sentence of "
        "reasoning:\nNOMINATE: <agent-name>\n"
        f"Valid agent names: {', '.join(others)}"
    )
```

- [ ] **Step 5: Run the prompt tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: PASS

- [ ] **Step 6: Update the orchestrator call sites**

Add this helper to `Orchestrator` (place it directly after `_checkpoint`, around `debatelab/orchestrator.py:76`):

```python
    def _abstain(self, debate_id, state, phase, name, content, reason):
        """Record an agent as abstaining for this phase."""
        state["abstained"] = sorted(set(state["abstained"]) | {name})
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": phase, "agent": name,
            "type": "abstained", "content": content, "reason": reason,
        })
```

In `_phase_vote`, replace the nomination loop (`debatelab/orchestrator.py:164-172`):

```python
        nominations = {}
        for name, text in nom_raw.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "nomination", "content": text,
            })
            nominee = prompts.parse_nomination(text, names)
            if nominee == name:
                self.store.append_event(debate_id, {
                    "round": state["round"], "phase": "vote", "agent": name,
                    "type": "nomination_dropped", "content": text,
                    "reason": "self-nomination",
                })
                continue
            if nominee:
                nominations[name] = nominee
```

Replace the vote loop (`debatelab/orchestrator.py:187-195`):

```python
        votes = {}
        for name, text in vote_raw.items():
            verdict = prompts.parse_vote(text)
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

- [ ] **Step 7: Add orchestrator tests for the new behavior**

Append to `tests/test_orchestrator.py`:

```python
def test_unparseable_vote_abstains_instead_of_counting_as_accept(tmp_path):
    """Regression: 'I cannot accept' used to parse as accept and could
    manufacture a consensus that no agent gave."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["proposal a", "crit a", "rev a", "NOMINATE: b",
                        "I cannot accept this"]),
        MockAgent("b", ["proposal b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    types = [e["type"] for e in store.read_events(did)]
    assert "abstained" in types
    state = store.read_state(did)
    assert "a" not in state["votes"]
    assert state["abstained"] == ["a"]


def test_self_nomination_is_dropped_and_recorded(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["proposal a", "crit a", "rev a", "NOMINATE: a",
                        "VOTE: accept"]),
        MockAgent("b", ["proposal b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    dropped = [e for e in store.read_events(did)
               if e["type"] == "nomination_dropped"]
    assert [e["agent"] for e in dropped] == ["a"]
```

`tests/test_orchestrator.py` already imports everything these need (`pytest`, `models`, `AgentError`, `Orchestrator`, `DebateStore`, and `from .conftest import MockAgent` — note the **relative** import). No import changes needed. The file also has a `make_store(tmp_path)` helper you may use instead of constructing `DebateStore` inline.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS (all tests; count rises above 117)

- [ ] **Step 9: Commit**

```bash
git add debatelab/prompts.py debatelab/orchestrator.py tests/test_prompts.py tests/test_orchestrator.py
git commit -m "fix: never infer a vote or nomination from prose

parse_vote read any 'accept' in the first line as acceptance, so
'I cannot accept this' was recorded as an accept and could manufacture
consensus. parse_nomination picked the first agent name mentioned
anywhere, so calling an agent weakest nominated it. Both are now strict
and return None; unparseable votes abstain.

Also forbids self-nomination in the prompt and drops it in the
orchestrator with a nomination_dropped event."
```

---

### Task 2: Seeded, reproducible candidate selection

**Files:**
- Modify: `debatelab/protocol.py:1-24` (`select_candidate`)
- Modify: `debatelab/orchestrator.py:173-179` (call site)
- Test: `tests/test_protocol.py:19-30` (rewrite)

**Interfaces:**
- Consumes: Task 1's `parse_nomination`
- Produces: `protocol.select_candidate(nominations: dict[str, str], agent_order: list[str], seed: str) -> tuple[str, bool]` returning `(winner, was_fallback)`. **Signature and return type both changed.** Orchestrator passes `seed=f"{debate_id}:{round}"`.

- [ ] **Step 1: Rewrite the selection tests**

Replace `tests/test_protocol.py:19-30`:

```python
def test_select_candidate_plurality_wins():
    noms = {"a": "b", "b": "b", "c": "a"}
    assert protocol.select_candidate(noms, ["a", "b", "c"], "d:1") == ("b", False)


def test_select_candidate_tie_break_is_reproducible():
    """Same debate + round must always pick the same winner, so a debate
    stays verifiable by replaying its transcript."""
    noms = {"a": "c", "b": "b"}
    first = protocol.select_candidate(noms, ["a", "b", "c"], "d:1")
    assert first == protocol.select_candidate(noms, ["a", "b", "c"], "d:1")
    assert first[0] in ("b", "c")
    assert first[1] is False


def test_select_candidate_tie_break_is_not_config_order():
    """Regression: config order used to decide every tie, so the first agent
    in agents.yaml won structurally."""
    noms = {"a": "c", "b": "b"}
    winners = {
        protocol.select_candidate(noms, ["a", "b", "c"], f"d:{i}")[0]
        for i in range(20)
    }
    assert winners == {"b", "c"}


def test_select_candidate_no_nominations_is_a_flagged_fallback():
    winner, was_fallback = protocol.select_candidate({}, ["a", "b"], "d:1")
    assert winner in ("a", "b")
    assert was_fallback is True


def test_select_candidate_fallback_is_not_always_the_first_agent():
    winners = {
        protocol.select_candidate({}, ["a", "b"], f"d:{i}")[0] for i in range(20)
    }
    assert winners == {"a", "b"}
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -q`
Expected: FAIL — `select_candidate() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: Implement seeded selection**

Replace `debatelab/protocol.py:1-24`:

```python
"""Pure debate-protocol logic: phase sequencing, candidate selection, consensus.

This module is deliberately pure: no files, no clock, no network, no
knowledge of debate ids. Callers pass a `seed` string and a `roster_size`.
"""

import random
from collections import Counter

PHASES = ("propose", "critique", "revise", "vote")


def next_phase(round_num: int, last_completed: str | None) -> tuple[int, str]:
    """Return the round and phase to run next."""
    if round_num == 0:
        return 1, "propose"
    if last_completed == "vote":
        return round_num + 1, "critique"
    return round_num, PHASES[PHASES.index(last_completed) + 1]


def select_candidate(
    nominations: dict[str, str], agent_order: list[str], seed: str
) -> tuple[str, bool]:
    """Return (winner, was_fallback) for the plurality nominee.

    Ties and the zero-nomination fallback resolve with an RNG seeded from
    `seed`, so selection is unbiased across agents yet reproducible from the
    transcript alone. Config order deliberately does NOT decide ties: it made
    the first agent in agents.yaml win structurally.
    """
    rng = random.Random(seed)
    if not nominations:
        return rng.choice(sorted(agent_order)), True
    counts = Counter(nominations.values())
    best = max(counts.values())
    tied = sorted(name for name, count in counts.items() if count == best)
    return rng.choice(tied), False
```

- [ ] **Step 4: Run to verify the protocol tests pass**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -q`
Expected: PASS

- [ ] **Step 5: Update the orchestrator call site**

Replace `debatelab/orchestrator.py:173-179`:

```python
        order_with_proposals = [n for n in self.order if n in proposals]
        winner, was_fallback = protocol.select_candidate(
            nominations, order_with_proposals, f"{debate_id}:{state['round']}"
        )
        if was_fallback:
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": winner,
                "type": "fallback_candidate",
                "content": (
                    "no valid nominations; candidate chosen by seeded draw"
                ),
            })
        state["candidate"] = {"agent": winner, "text": proposals[winner]}
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "vote", "agent": winner,
            "type": "candidate", "content": proposals[winner],
        })
```

- [ ] **Step 6: Add a fallback-event test**

Append to `tests/test_orchestrator.py`:

```python
def test_zero_valid_nominations_emits_fallback_candidate(tmp_path):
    """Regression: this used to silently crown agent_order[0]."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["proposal a", "crit a", "rev a", "no idea",
                        "VOTE: accept"]),
        MockAgent("b", ["proposal b", "crit b", "rev b", "dunno",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    fallbacks = [e for e in store.read_events(did)
                 if e["type"] == "fallback_candidate"]
    assert len(fallbacks) == 1
```

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add debatelab/protocol.py debatelab/orchestrator.py tests/test_protocol.py tests/test_orchestrator.py
git commit -m "fix: de-bias candidate selection with a seeded tie-break

Ties were broken by config order, so combined with self-nomination the
first agent in agents.yaml won every round regardless of answer quality.
Ties and the zero-nomination fallback now draw from an RNG seeded with
debate id and round: unbiased across agents, still reproducible from the
transcript. Plain random was rejected because it would make selection
unverifiable on replay.

Zero valid nominations now emits fallback_candidate instead of silently
crowning agent_order[0]."
```

---

### Task 3: Quorum over the recorded roster

**Files:**
- Modify: `debatelab/protocol.py` (add `DEFAULT_QUORUM`, `required_accepts`, `tally`; rewrite `check_consensus`)
- Modify: `debatelab/store.py:59-75` (`create` seeds `roster`/`quorum`)
- Modify: `debatelab/orchestrator.py:24-62` (`run` signature, roster recording, consensus call)
- Modify: `debatelab/cli.py` (`--quorum` on `run`)
- Test: `tests/test_protocol.py:33-38` (rewrite), `tests/test_orchestrator.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 2's `select_candidate`
- Produces:
  - `protocol.DEFAULT_QUORUM: Fraction` = `Fraction(2, 3)`
  - `protocol.required_accepts(roster_size: int, quorum: Fraction) -> int`
  - `protocol.tally(votes: dict, roster_size: int, quorum: Fraction) -> dict` with keys `accepts, rejects, abstains, roster_size, required, quorum`
  - `protocol.check_consensus(votes: dict, roster_size: int, quorum: Fraction) -> bool` — **signature changed**
  - `cli.quorum_fraction(value: str) -> Fraction` (argparse type)
  - `Orchestrator.run(debate_id, max_rounds=None, quorum=None)` — **`quorum` param added**
  - `state["roster"]: list[str] | None`, `state["quorum"]: str` (e.g. `"2/3"`)

- [ ] **Step 1: Rewrite the consensus tests**

Replace `tests/test_protocol.py:33-38`:

```python
from fractions import Fraction

Q = Fraction(2, 3)
ACCEPT = {"vote": "accept", "reason": "r"}
REJECT = {"vote": "reject", "reason": "r"}


def test_required_accepts_uses_exact_fraction_arithmetic():
    # A float quorum of 0.667 would give ceil(0.667*3) == 3, silently
    # demanding unanimity on the default 3-agent roster.
    assert protocol.required_accepts(3, Q) == 2
    assert protocol.required_accepts(4, Q) == 3
    assert protocol.required_accepts(5, Q) == 4


def test_check_consensus_quorum_table():
    votes3 = {"a": ACCEPT, "b": ACCEPT}
    assert protocol.check_consensus(votes3, 3, Q) is True          # 2 of 3
    assert protocol.check_consensus({"a": ACCEPT}, 3, Q) is False   # 1 of 3
    assert protocol.check_consensus(votes3, 5, Q) is False          # 2 of 5
    five = {n: ACCEPT for n in "abcd"}
    assert protocol.check_consensus(five, 5, Q) is True             # 4 of 5


def test_check_consensus_any_reject_blocks_even_at_quorum():
    votes = {"a": ACCEPT, "b": ACCEPT, "c": REJECT}
    assert protocol.check_consensus(votes, 3, Q) is False


def test_check_consensus_no_votes_is_false():
    assert protocol.check_consensus({}, 3, Q) is False


def test_tally_derives_abstains_from_the_roster():
    """abstains is roster minus voters, not state['abstained'] — that list is
    per-round and mixes nominate- and vote-phase abstentions."""
    t = protocol.tally({"a": ACCEPT, "b": REJECT}, 5, Q)
    assert t == {
        "accepts": 1, "rejects": 1, "abstains": 3,
        "roster_size": 5, "required": 4, "quorum": "2/3",
    }
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -q`
Expected: FAIL — `module 'debatelab.protocol' has no attribute 'required_accepts'`

- [ ] **Step 3: Implement quorum in protocol.py**

Add to the imports at the top of `debatelab/protocol.py`:

```python
import math
import random
from collections import Counter
from fractions import Fraction
```

Add after `PHASES`:

```python
DEFAULT_QUORUM = Fraction(2, 3)
```

Replace `check_consensus` at the bottom of the file:

```python
def required_accepts(roster_size: int, quorum: Fraction) -> int:
    """Accepts needed for consensus. Fraction arithmetic is required: a float
    0.667 gives ceil(0.667 * 3) == 3, which would demand unanimity on a
    3-agent roster instead of the intended 2."""
    return math.ceil(quorum * roster_size)


def tally(votes: dict, roster_size: int, quorum: Fraction) -> dict:
    """Vote breakdown against the roster the debate started with.

    `abstains` is derived as roster_size - accepts - rejects rather than read
    from state["abstained"], which resets per round and accumulates both
    nominate- and vote-phase abstentions.
    """
    accepts = sum(1 for v in votes.values() if v["vote"] == "accept")
    rejects = sum(1 for v in votes.values() if v["vote"] == "reject")
    return {
        "accepts": accepts,
        "rejects": rejects,
        "abstains": roster_size - accepts - rejects,
        "roster_size": roster_size,
        "required": required_accepts(roster_size, quorum),
        "quorum": str(quorum),
    }


def check_consensus(votes: dict, roster_size: int, quorum: Fraction) -> bool:
    """Consensus = zero rejects AND accepts >= ceil(quorum * roster_size).

    The denominator is the roster the run started with, not the agents that
    happened to reply: unanimity among responders let 2 accepts out of 5
    configured agents report as unanimous consensus.
    """
    counts = tally(votes, roster_size, quorum)
    return counts["rejects"] == 0 and counts["accepts"] >= counts["required"]
```

- [ ] **Step 4: Run to verify the protocol tests pass**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -q`
Expected: PASS

- [ ] **Step 5: Seed roster and quorum in new debates**

In `debatelab/store.py`, inside the `write_state` dict in `create` (around line 61-74), add two keys after `"max_rounds": 5,`:

```python
                "quorum": "2/3",
                "roster": None,
```

`roster` is `None`, not `[]`: the orchestrator treats a non-None roster that differs from the current one as a change, and `[]` would fire a spurious `roster_changed` on the very first run.

- [ ] **Step 6: Record the roster and use the quorum in the orchestrator**

Add to `debatelab/orchestrator.py` imports:

```python
from fractions import Fraction
```

Replace `debatelab/orchestrator.py:24-31` (the head of `run`):

```python
    def run(self, debate_id: str, max_rounds: int | None = None,
            quorum: Fraction | None = None) -> str:
        state = self.store.read_state(debate_id)
        if state["status"] in ("awaiting_human", "approved", "rejected"):
            return state["status"]
        if max_rounds is not None:
            state["max_rounds"] = max_rounds
        if quorum is not None:
            state["quorum"] = str(quorum)
        state.setdefault("quorum", str(protocol.DEFAULT_QUORUM))
        recorded = state.get("roster")
        if recorded is not None and recorded != self.order:
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "run", "agent": None,
                "type": "roster_changed",
                "content": f"roster changed from {recorded} to {self.order}",
            })
        state["roster"] = list(self.order)
        state["status"] = "running"
        problem = self.store.read_problem(debate_id)
```

Replace the consensus check at `debatelab/orchestrator.py:52-59`:

```python
                quorum_frac = Fraction(state["quorum"])
                roster_size = len(state["roster"])
                if phase == "vote" and protocol.check_consensus(
                    state["votes"], roster_size, quorum_frac
                ):
                    state["status"] = "awaiting_human"
                    self.store.append_event(debate_id, {
                        "round": rnd, "phase": "vote",
                        "agent": state["candidate"]["agent"],
                        "type": "consensus",
                        "content": state["candidate"]["text"],
                        "tally": protocol.tally(
                            state["votes"], roster_size, quorum_frac
                        ),
                    })
```

- [ ] **Step 7: Add the `--quorum` flag**

Add to `debatelab/cli.py` imports:

```python
from fractions import Fraction
```

Add next to `positive_int` (around `debatelab/cli.py:13-17`):

```python
def quorum_fraction(value: str) -> Fraction:
    try:
        q = Fraction(value)
    except (ValueError, ZeroDivisionError):
        raise argparse.ArgumentTypeError(
            f"not a fraction: {value!r} (try 2/3)"
        )
    if not 0 < q <= 1:
        raise argparse.ArgumentTypeError("must be greater than 0 and at most 1")
    return q
```

In `cmd_run` (`debatelab/cli.py:57`), pass it through:

```python
    status = orch.run(args.id, max_rounds=args.max_rounds, quorum=args.quorum)
```

In the `run` subparser (after `debatelab/cli.py:219`):

```python
    sp.add_argument(
        "--quorum",
        type=quorum_fraction,
        default=None,
        help="fraction of the roster that must accept, e.g. 2/3 (default 2/3)",
    )
```

- [ ] **Step 8: Add orchestrator and CLI tests**

Append to `tests/test_orchestrator.py`:

```python
def test_two_accepts_of_a_five_agent_roster_is_not_consensus(tmp_path):
    """Regression, verified to reproduce against the pre-fix code: consensus
    was unanimity among agents that VOTED, and a phase needed only 2
    responders, so 5 configured agents with 3 network failures and 2 accepts
    returned 'awaiting_human'. It must now be no_consensus (2 < ceil(2/3*5)=4).

    A MockAgent with an empty script raises AgentError on every ask, which is
    how c/d/e abstain in every phase.
    """
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: accept"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
        MockAgent("c", []), MockAgent("d", []), MockAgent("e", []),
    ]
    status = Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["roster"] == ["a", "b", "c", "d", "e"]
    assert sorted(state["votes"]) == ["a", "b"]         # 2 accepts of 5
    assert status == "no_consensus"


def test_full_roster_accept_reaches_consensus_with_a_tally(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: accept"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
        MockAgent("c", ["prop c", "crit c", "rev c", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    assert store.read_state(did)["status"] == "awaiting_human"
    consensus = [e for e in store.read_events(did) if e["type"] == "consensus"]
    assert consensus[0]["tally"] == {
        "accepts": 3, "rejects": 0, "abstains": 0,
        "roster_size": 3, "required": 2, "quorum": "2/3",
    }


def test_roster_change_on_resume_is_recorded(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    state = store.read_state(did)
    state["roster"] = ["a", "b", "zz"]
    store.write_state(did, state)
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: reject"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: reject"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    changed = [e for e in store.read_events(did) if e["type"] == "roster_changed"]
    assert len(changed) == 1
    assert store.read_state(did)["roster"] == ["a", "b"]
```

These survive Task 4: `c`/`d`/`e` abstain via `AgentError`, which `_reask` catches and turns into `(None, None)` — the same abstention, one extra failed call.

Append to `tests/test_cli.py` (`pytest` and `cli` are already imported there; `argparse` and `Fraction` are imported inside the test to avoid touching the header):

```python
def test_quorum_fraction_accepts_fractions_and_rejects_junk():
    import argparse
    from fractions import Fraction

    assert cli.quorum_fraction("2/3") == Fraction(2, 3)
    assert cli.quorum_fraction("1") == Fraction(1)
    for bad in ("banana", "0", "5/4", "-1/2"):
        with pytest.raises(argparse.ArgumentTypeError):
            cli.quorum_fraction(bad)
```

Also append a backward-compatibility test to `tests/test_orchestrator.py`, since debates created before this change have a `state.json` with neither key:

```python
def test_state_predating_roster_and_quorum_still_runs(tmp_path):
    """Compatibility: old debates have no roster/quorum keys. They must
    default rather than raise, and must not emit a spurious roster_changed."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    state = store.read_state(did)
    del state["roster"]
    del state["quorum"]
    store.write_state(did, state)
    agents = [
        MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "VOTE: accept"]),
        MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["quorum"] == "2/3"
    assert state["roster"] == ["a", "b"]
    assert not [e for e in store.read_events(did) if e["type"] == "roster_changed"]
```

- [ ] **Step 9: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add debatelab/protocol.py debatelab/orchestrator.py debatelab/store.py debatelab/cli.py tests/
git commit -m "fix: require a quorum of the recorded roster for consensus

Consensus was unanimity among agents that replied, and a phase needed only
2 responders, so 2 accepts on a 5-agent roster with 3 network failures
reported as unanimous. Consensus now needs zero rejects and accepts >=
ceil(quorum * roster_size), default 2/3, configurable via --quorum.

The roster is recorded in state at run start so the denominator is
auditable and a mid-debate agents.yaml edit emits roster_changed rather
than silently moving the bar. Quorum is stored as '2/3' and computed with
Fraction: a float 0.667 gives ceil(0.667*3)==3, which would demand
unanimity on the default roster."
```

---

### Task 4: One structured re-ask before abstaining

**Files:**
- Modify: `debatelab/prompts.py` (add `reask`)
- Modify: `debatelab/orchestrator.py` (add `_reask`, wire into `_phase_vote`)
- Test: `tests/test_prompts.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: Task 1's `parse_vote`, `parse_nomination`, `VOTE_REQUIRED`, `NOMINATE_REQUIRED`, `_abstain`
- Produces:
  - `prompts.reask(original_prompt: str, required: str) -> str`
  - `Orchestrator._reask(name, prompt, parse, required, task) -> tuple[object | None, str | None]`

- [ ] **Step 1: Write the failing re-ask prompt test**

Append to `tests/test_prompts.py`:

```python
def test_reask_resends_the_whole_prompt():
    """CLI agents are stateless subprocesses with no session, so a re-ask
    cannot just say 'try again' — it must resend the original prompt."""
    original = prompts.vote_prompt("alpha", "Q", "beta", "the answer")
    r = prompts.reask(original, prompts.VOTE_REQUIRED)
    assert original in r
    assert "could not be parsed" in r
    assert "ONLY" in r
    assert prompts.VOTE_REQUIRED in r
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_prompts.py::test_reask_resends_the_whole_prompt -q`
Expected: FAIL — `module 'debatelab.prompts' has no attribute 'reask'`

- [ ] **Step 3: Implement `reask`**

Append to `debatelab/prompts.py`:

```python
def reask(original_prompt: str, required: str) -> str:
    """Re-ask an agent whose reply did not parse.

    The full original prompt is resent because CLI agents are one-shot
    subprocesses with no conversation state — there is nothing for a bare
    "try again" to refer to.
    """
    return (
        f"{original_prompt}\n\n"
        "Your previous reply could not be parsed. "
        f"Reply with ONLY the line {required}. No other text."
    )
```

- [ ] **Step 4: Run to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -q`
Expected: PASS

- [ ] **Step 5: Wire the re-ask into the orchestrator**

Add to `Orchestrator`, directly after `_abstain`:

```python
    def _reask(self, name, prompt, parse, required, task):
        """Ask one agent again after an unparseable reply. Returns
        (value, text); (None, None) when the agent errors out. Re-asks run
        serially: they are rare, cheap (FAST), and not worth the concurrency."""
        try:
            text = self.agents[name].ask(prompts.reask(prompt, required), task)
        except AgentError:
            return None, None
        return parse(text), text
```

In `_phase_vote`, the nomination loop becomes:

```python
        nominations = {}
        for name, text in nom_raw.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "nomination", "content": text,
            })
            nominee = prompts.parse_nomination(text, names)
            if nominee is None:
                nominee, retry = self._reask(
                    name,
                    prompts.nominate_prompt(name, problem, proposals, names),
                    lambda t: prompts.parse_nomination(t, names),
                    prompts.NOMINATE_REQUIRED,
                    models.FAST,
                )
                if retry is not None:
                    text = retry
            if nominee == name:
                self.store.append_event(debate_id, {
                    "round": state["round"], "phase": "vote", "agent": name,
                    "type": "nomination_dropped", "content": text,
                    "reason": "self-nomination",
                })
                continue
            if nominee:
                nominations[name] = nominee
```

And the vote loop becomes:

```python
        votes = {}
        for name, text in vote_raw.items():
            verdict = prompts.parse_vote(text)
            if verdict is None:
                verdict, retry = self._reask(
                    name,
                    prompts.vote_prompt(name, problem, winner, proposals[winner]),
                    prompts.parse_vote,
                    prompts.VOTE_REQUIRED,
                    models.FAST,
                )
                if retry is not None:
                    text = retry
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

Note: unparseable nominations do **not** halt the debate. All-unparseable simply yields zero nominations, which is exactly the `fallback_candidate` path from Task 2. `DebateHalted` stays reserved for agents that did not respond at all.

- [ ] **Step 6: Add re-ask orchestrator tests**

Append to `tests/test_orchestrator.py`:

```python
def test_unparseable_vote_is_reasked_once_and_then_counted(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "I cannot accept this", "VOTE: reject"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])
    Orchestrator(store, [a, b]).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["votes"]["a"]["vote"] == "reject"
    assert state["abstained"] == []
    # the re-ask resent the whole original prompt
    assert "could not be parsed" in a.prompts[-1]
    assert "Candidate final answer" in a.prompts[-1]


def test_vote_unparseable_twice_abstains(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "I cannot accept this", "still refusing to comply"])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])
    Orchestrator(store, [a, b]).run(did, max_rounds=1)
    state = store.read_state(did)
    assert "a" not in state["votes"]
    assert state["abstained"] == ["a"]


def test_agent_error_during_reask_abstains(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    a = MockAgent("a", ["prop a", "crit a", "rev a", "NOMINATE: b",
                        "garbage", AgentError("a: down")])
    b = MockAgent("b", ["prop b", "crit b", "rev b", "NOMINATE: a",
                        "VOTE: accept"])
    Orchestrator(store, [a, b]).run(did, max_rounds=1)
    assert store.read_state(did)["abstained"] == ["a"]
```

Add `from debatelab.agents.base import AgentError` to the imports of `tests/test_orchestrator.py` if not already present.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add debatelab/prompts.py debatelab/orchestrator.py tests/test_prompts.py tests/test_orchestrator.py
git commit -m "feat: re-ask once before recording an abstention

A formatting slip cost a real vote outright, and with quorum rules that
could turn into a false no_consensus. An unparseable vote or nomination
now gets one strict re-ask before abstaining. The re-ask resends the full
original prompt because CLI agents are one-shot subprocesses with no
conversation state.

Unparseable nominations still do not halt a debate: all-unparseable is
the zero-nomination fallback path, and DebateHalted stays reserved for
agents that did not respond at all."
```

---

### Task 5: Atomic writes for every derived artifact

**Files:**
- Modify: `debatelab/store.py:88-92` (`write_state`), `:100-101` (`write_summary`), `:119-132` (`rebuild_index`)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing
- Produces: `store._atomic_write(path: Path, text: str) -> None`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_store.py`:

```python
from debatelab import store as store_mod


def test_atomic_write_replaces_content_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "summary.md"
    store_mod._atomic_write(p, "a" * 100)
    store_mod._atomic_write(p, "b")
    assert p.read_text() == "b"
    assert list(tmp_path.iterdir()) == [p]


def test_atomic_write_tmp_keeps_the_full_target_name(tmp_path, monkeypatch):
    """with_suffix('.json.tmp') would turn summary.md into summary.json.tmp;
    the tmp file must sit beside the target so replace() is a same-filesystem
    rename, which is what makes it atomic."""
    seen = {}
    original = Path.replace

    def spy(self, target):
        seen["tmp"] = self.name
        seen["dir"] = self.parent
        return original(self, target)

    monkeypatch.setattr(Path, "replace", spy)
    store_mod._atomic_write(tmp_path / "summary.md", "x")
    assert seen["tmp"] == "summary.md.tmp"
    assert seen["dir"] == tmp_path


def test_write_summary_and_rebuild_index_leave_no_tmp_behind(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    store.write_summary(did, "# hi")
    store.rebuild_index()
    root = tmp_path / "debates"
    assert not (root / "index.json.tmp").exists()
    assert not (root / did / "summary.md.tmp").exists()
    assert not (root / did / "state.json.tmp").exists()
    assert (root / did / "summary.md").read_text() == "# hi"
```

`tests/test_store.py` currently imports only `json`, `pytest`, and `from debatelab.store import DebateStore, render_summary, slugify`. Add these two lines to its header:

```python
from pathlib import Path

from debatelab import store as store_mod
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_store.py -q`
Expected: FAIL — `module 'debatelab.store' has no attribute '_atomic_write'`

- [ ] **Step 3: Add the helper and route all three writers through it**

Add to `debatelab/store.py` after `_now()`:

```python
def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    `with_name`, not `with_suffix`: with_suffix('.json.tmp') only works when
    the target already ends in .json and would turn summary.md into
    summary.json.tmp. Same directory means replace() is a same-filesystem
    rename, which is what makes it atomic for readers.

    No fsync: the goal is that the polling viewer never sees a torn file, not
    that writes survive power loss.
    """
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
```

Replace `write_state` (`debatelab/store.py:88-92`):

```python
    def write_state(self, debate_id, state: dict):
        _atomic_write(
            self.path(debate_id) / "state.json",
            json.dumps(state, indent=2, ensure_ascii=False),
        )
```

Replace `write_summary` (`debatelab/store.py:100-101`):

```python
    def write_summary(self, debate_id, markdown: str):
        _atomic_write(self.path(debate_id) / "summary.md", markdown)
```

Replace the last two lines of `rebuild_index` (`debatelab/store.py:131-132`):

```python
        self.root.mkdir(exist_ok=True)
        _atomic_write(self.root / "index.json", json.dumps(entries, indent=2))
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add debatelab/store.py tests/test_store.py
git commit -m "fix: write summary.md and index.json atomically

write_state used tmp-then-replace but write_summary and rebuild_index used
bare write_text, so the viewer polling index.json every few seconds could
read it mid-write. The inconsistency was the tell: the pattern was known
and applied in one of three places.

Extracts _atomic_write and routes all three through it, using with_name so
the temp file keeps the target's full name and stays in its directory
(making replace a same-filesystem rename).

Known limitation: index.json is root-level, so the per-debate lock cannot
protect it and two runs on different debates can still race. Each rebuild
is a full fresh scan, so it self-heals; a root-level lock is deferred."
```

---

### Task 6: Per-debate run lock

**Files:**
- Modify: `debatelab/store.py` (add `LockError`, `run_lock`, helpers)
- Modify: `debatelab/cli.py:34-63` (`cmd_run`), `:217-221` (`run` subparser), `:257-265` (`main` error handling)
- Test: `tests/test_lock.py` (create)

**Interfaces:**
- Consumes: Task 3's `Orchestrator.run(..., quorum=...)`
- Produces:
  - `store.LockError(Exception)`
  - `DebateStore.run_lock(debate_id: str, force: bool = False)` — context manager yielding the lock info dict
  - `debate run --force`

- [ ] **Step 1: Write the failing lock tests**

Create `tests/test_lock.py`:

```python
import json
import os
import socket
import subprocess
import sys

import pytest

from debatelab.store import DebateStore, LockError


def make(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    return store, did, tmp_path / "debates" / did / "run.lock"


def dead_pid():
    """A PID that is certainly not running: spawn and reap a process."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_run_lock_writes_holder_info_and_removes_it_on_exit(tmp_path):
    store, did, lock = make(tmp_path)
    with store.run_lock(did):
        info = json.loads(lock.read_text())
        assert info["pid"] == os.getpid()
        assert info["host"] == socket.gethostname()
        assert info["started_at"] and info["run_id"]
    assert not lock.exists()


def test_run_lock_is_released_when_the_run_raises(tmp_path):
    store, did, lock = make(tmp_path)
    with pytest.raises(RuntimeError):
        with store.run_lock(did):
            raise RuntimeError("boom")
    assert not lock.exists()


def test_run_lock_refuses_a_second_holder(tmp_path):
    store, did, _ = make(tmp_path)
    with store.run_lock(did):
        with pytest.raises(LockError, match="locked by pid"):
            with store.run_lock(did):
                pass


def test_run_lock_breaks_a_stale_same_host_lock(tmp_path):
    store, did, lock = make(tmp_path)
    lock.write_text(json.dumps({
        "pid": dead_pid(), "host": socket.gethostname(),
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
    }))
    with store.run_lock(did):
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert not lock.exists()


def test_run_lock_refuses_a_foreign_host_lock_unless_forced(tmp_path):
    store, did, lock = make(tmp_path)
    holder = json.dumps({
        "pid": 1, "host": "some-other-host",
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
    })
    lock.write_text(holder)
    with pytest.raises(LockError, match="--force"):
        with store.run_lock(did):
            pass
    lock.write_text(holder)
    with store.run_lock(did, force=True):
        assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_run_lock_refuses_an_unreadable_lock_rather_than_guessing(tmp_path):
    """A half-written lock must not read as stale: breaking it would let two
    runs proceed. Refusing is the safe direction to err."""
    store, did, lock = make(tmp_path)
    lock.write_text("not json at all")
    with pytest.raises(LockError):
        with store.run_lock(did):
            pass
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_lock.py -q`
Expected: FAIL — `ImportError: cannot import name 'LockError' from 'debatelab.store'`

- [ ] **Step 3: Implement the lock**

Add to `debatelab/store.py` imports:

```python
import contextlib
import json
import os
import re
import socket
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
```

Add after `_atomic_write`:

```python
class LockError(Exception):
    """Another process holds this debate's run lock."""


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # exists, owned by someone else
    return True


def _read_lock(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def _is_stale(holder: dict) -> bool:
    """Only a live-PID check on THIS host can prove staleness.

    Anything unknowable — a foreign host, a missing or half-written lock —
    is treated as held. Erring toward a spurious refusal (resolvable with
    --force) beats erring toward two concurrent runs shredding a transcript.
    Inherits the usual PID-reuse race: a recycled PID reads as live.
    """
    if holder.get("host") != socket.gethostname():
        return False
    pid = holder.get("pid")
    if not isinstance(pid, int):
        return False
    return not _pid_alive(pid)
```

Add these methods to `DebateStore`:

```python
    def _acquire_lock(self, path: Path, force: bool) -> int:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            return os.open(path, flags)
        except FileExistsError:
            pass
        holder = _read_lock(path)
        if not force and not _is_stale(holder):
            raise LockError(
                f"debate is locked by pid {holder.get('pid')} on "
                f"{holder.get('host')} since {holder.get('started_at')}; "
                "use --force if that run is dead"
            )
        why = "forced" if force else "stale"
        print(
            f"breaking {why} lock from pid {holder.get('pid')}",
            file=sys.stderr,
        )
        path.unlink(missing_ok=True)
        try:
            return os.open(path, flags)
        except FileExistsError:
            raise LockError("lock was re-acquired by another process; retry")

    @contextlib.contextmanager
    def run_lock(self, debate_id: str, force: bool = False):
        """Hold debates/<id>/run.lock for the duration of a run.

        The original design listed concurrent runs of one debate as a
        non-goal but never enforced it: two `debate run` processes both
        append to transcript.jsonl and race state.json.
        """
        d = self.path(debate_id)
        if not (d / "state.json").exists():
            raise FileNotFoundError(f"no such debate: {debate_id}")
        path = d / "run.lock"
        fd = self._acquire_lock(path, force)
        info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _now(),
            "run_id": uuid.uuid4().hex,
        }
        with os.fdopen(fd, "w") as f:
            json.dump(info, f)
        try:
            yield info
        finally:
            path.unlink(missing_ok=True)
```

- [ ] **Step 4: Run the lock tests**

Run: `.venv/bin/python -m pytest tests/test_lock.py -q`
Expected: PASS

- [ ] **Step 5: Hold the lock in `cmd_run`**

The lock lives at the CLI boundary, not in `Orchestrator`: process coordination is a property of the process, and `debate run` is the only entry point that appends phase events. This also keeps the existing orchestrator tests free of lock lifecycle.

The lock is taken **first**, before `load_agent_specs`. A locked debate should be refused before doing any config or agent work, and this ordering is what the Step 6 test pins.

In `debatelab/cli.py`, change the import:

```python
from .store import DebateStore, LockError, render_summary
```

Replace the whole body of `cmd_run` (`debatelab/cli.py:34-63`):

```python
def cmd_run(args):
    from .orchestrator import Orchestrator

    store = get_store()
    try:
        with store.run_lock(args.id, force=args.force):
            specs = registry.load_agent_specs(args.config)
            ready = []
            for spec in specs:
                if not spec.enabled:
                    continue
                problem = registry.spec_problem(spec)
                if problem:
                    print(f"skipping agent '{spec.name}': {problem}", flush=True)
                    continue
                ready.append(spec)
            agents = registry.build_agents(ready)
            try:
                orch = Orchestrator(
                    store,
                    agents,
                    progress=lambda m: print(m, flush=True),
                )
            except ValueError as e:
                sys.exit(str(e))
            status = orch.run(
                args.id, max_rounds=args.max_rounds, quorum=args.quorum
            )
    except LockError as e:
        sys.exit(str(e))
    print(f"final status: {status}")
    if status in ("awaiting_human", "no_consensus"):
        print(
            f"review with `debate show {args.id}`, then "
            f"`debate approve {args.id}` or `debate reject {args.id} -m ...`"
        )
```

Add to the `run` subparser:

```python
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing run lock (use only if that run is dead)",
    )
```

- [ ] **Step 6: Add CLI-level lock tests**

Append to `tests/test_lock.py`:

```python
def test_cli_run_exits_cleanly_when_the_debate_is_locked(tmp_path, monkeypatch):
    """The lock must be checked before any config or agent work, so a locked
    debate is refused with the holder's details rather than a config error."""
    from debatelab import cli

    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    monkeypatch.setattr(cli, "get_store", lambda: store)
    monkeypatch.chdir(tmp_path)

    def locked(*a, **k):
        raise LockError("debate is locked by pid 999 on host-x since then")

    monkeypatch.setattr(DebateStore, "run_lock", locked)
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", did])
    assert "locked by pid 999" in str(exc.value)


def test_run_lock_reports_a_missing_debate_clearly(tmp_path):
    store = DebateStore(tmp_path / "debates")
    store.create("T", "problem")          # creates the root
    with pytest.raises(FileNotFoundError, match="no such debate"):
        with store.run_lock("20260714-nope"):
            pass
```

Note there is no `--config` argument in the first test: reaching `load_agent_specs` at all would mean the lock was taken too late, and the test would fail with a config error instead of the lock message. That is the point of the test.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add debatelab/store.py debatelab/cli.py tests/test_lock.py
git commit -m "feat: guard each debate with a run lock

The original design listed concurrent runs of one debate as a non-goal but
nothing enforced it: two 'debate run' processes both append to
transcript.jsonl and race state.json, interleaving rounds into one log.

Adds debates/<id>/run.lock via O_CREAT|O_EXCL holding pid, host,
started_at and run_id. A dead PID on this host is broken automatically; a
foreign host or an unreadable lock is refused and needs --force, since
breaking a half-written lock would permit the very thing the lock
prevents.

The lock sits at the CLI boundary rather than in Orchestrator: process
coordination belongs to the process, and 'debate run' is the only entry
that appends phase events."
```

---

### Task 7: Serve only the debates directory

**Files:**
- Modify: `debatelab/cli.py:194-199` (`cmd_serve`)
- Modify: `debatelab/viewer/index.html:91,131,132` (fetch URLs)
- Test: `tests/test_serve.py:12-19` (fixture), `:28-42` (URLs), add exposure test

**Interfaces:**
- Consumes: nothing
- Produces: `make_server(port, directory)` unchanged in signature; `cmd_serve` now passes the debates root

**Note:** moving the server root from the CWD to `debates/` changes the viewer's own URL space — `/debates/index.json` becomes `/index.json`. The viewer and its tests must move in the same commit or the viewer breaks.

- [ ] **Step 1: Update the serve tests to the new root and URLs**

In `tests/test_serve.py`, replace the fixture (lines 12-19):

```python
@pytest.fixture
def running_server(tmp_path):
    store = DebateStore(tmp_path / "debates")
    store.create("Viewer test", "problem text")
    # a secret sitting next to debates/, exactly like the real repo root
    (tmp_path / "agents.yaml").write_text("agents: [{name: claude}]\n")
    srv = make_server(0, str(tmp_path / "debates"))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
```

Replace the three URL-based tests (lines 28-42):

```python
def test_debates_index_served(running_server):
    status, body = get(running_server + "/index.json")
    assert status == 200
    entries = json.loads(body)
    assert entries[0]["title"] == "Viewer test"


def test_debate_state_served(running_server):
    _, body = get(running_server + "/index.json")
    debate_id = json.loads(body)[0]["id"]
    status, body = get(f"{running_server}/{debate_id}/state.json")
    assert status == 200
    assert json.loads(body)["status"] == "created"


def test_files_outside_debates_are_not_served(running_server):
    """Regression: serve passed Path.cwd(), so agents.yaml and any .env in
    the folder were served to anyone who could reach the port."""
    with pytest.raises(urllib.error.HTTPError) as exc:
        get(running_server + "/agents.yaml")
    assert exc.value.code == 404
```

Add `import urllib.error` to the imports of `tests/test_serve.py`.

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_serve.py -q`
Expected: FAIL — `/index.json` returns 404 (the fixture still serves the debates dir but the old tests expected `/debates/...`; `test_files_outside_debates_are_not_served` is the meaningful new one)

- [ ] **Step 3: Serve the debates root**

Replace `debatelab/cli.py:194-199`:

```python
def cmd_serve(args):
    root = get_store().root
    root.mkdir(parents=True, exist_ok=True)
    srv = make_server(args.port, str(root))
    print(
        f"viewer at http://127.0.0.1:{srv.server_address[1]}/ "
        "(Ctrl-C to stop)"
    )
```

- [ ] **Step 4: Point the viewer at the new URL space**

In `debatelab/viewer/index.html`, line 91:

```javascript
  try { entries = await fetchJSON("/index.json"); }
```

Lines 131-132:

```javascript
    state = await fetchJSON(`/${id}/state.json`);
    events = await fetchJSONL(`/${id}/transcript.jsonl`);
```

- [ ] **Step 5: Run the serve tests**

Run: `.venv/bin/python -m pytest tests/test_serve.py -q`
Expected: PASS

- [ ] **Step 6: Verify the viewer actually loads (not just that tests pass)**

```bash
.venv/bin/python -m debatelab.cli serve --port 8099 &
sleep 1
curl -s http://127.0.0.1:8099/ | grep -c "AI Debate Lab"
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/index.json
curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:8099/agents.yaml
kill %1
```

Expected: `1`, then `200`, then `404`. The `404` on `agents.yaml` is the fix.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add debatelab/cli.py debatelab/viewer/index.html tests/test_serve.py
git commit -m "fix: serve only debates/ instead of the whole working directory

cmd_serve passed Path.cwd() to the static handler, so the viewer served
agents.yaml, any .env, and everything else in the folder to anyone who
could reach the port.

Serving the debates root moves the viewer's URL space from /debates/... to
/..., so the viewer's fetches move with it in the same commit."
```

---

## Verification

After all seven tasks:

```bash
.venv/bin/python -m pytest -q
```

Expected: all green, comfortably above the 117 baseline.

Manual end-to-end check that the headline bug is dead:

```bash
.venv/bin/python - <<'PY'
from debatelab import prompts, protocol
from fractions import Fraction

# The bug that started this: an explicit refusal read as agreement.
assert prompts.parse_vote("I cannot accept this") is None
# The quorum that let 2-of-5 report as unanimous.
accept = {"vote": "accept", "reason": ""}
assert protocol.check_consensus({"a": accept, "b": accept}, 5, Fraction(2, 3)) is False
assert protocol.check_consensus({"a": accept, "b": accept}, 3, Fraction(2, 3)) is True
print("protocol correctness verified")
PY
```

## Out of scope

Tracked in the spec's deferred roadmap; do **not** build these here: synthesis phase, prompt anonymization, `replay`/`fsck`, retry backoff and error classification, telemetry, context budgeting, `result.json`/`final.md`, `debate result`, richer `debate status`, viewer markdown rendering and hero panel, root-level lock for `index.json`, locking `approve`/`reject`.
