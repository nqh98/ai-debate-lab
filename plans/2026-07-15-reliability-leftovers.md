# Reliability Leftovers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serialize every process that mutates a debate's shared state, and record which model answered every agent call.

**Architecture:** Five tasks in two independent halves. Tasks 1–3 are locking: Task 1 generalizes the existing directory-flock helper and puts a root lock inside `rebuild_index`; Task 2 renames `run.lock` to `debate.lock` and teaches it which command holds it; Task 3 puts `approve`/`reject` under that lock. Tasks 4–5 are telemetry: Task 4 changes `Agent.ask` to return a `Reply(text, model)` value object and updates both adapters; Task 5 threads the reply through `retry.call_with_retry`'s `on_attempt` hook into the `agent_call` event. The halves touch disjoint code and could be done in either order; within each half the order is strict.

**Tech Stack:** Python ≥ 3.10, stdlib only (`fcntl`, `contextlib`, `dataclasses`, `threading`), pytest.

**Spec:** `specs/2026-07-15-reliability-leftovers-design.md`

## Global Constraints

- **No new runtime dependencies.** Runtime deps stay PyYAML only. `fcntl` is stdlib and already imported by `store.py`.
- **`protocol.py`, `prompts.py`, `replay.py`, and `result.py` must not be modified by any task in this plan.** The spec's scope line is explicit. If a task appears to need a change there, stop — that is a spec violation, not a plan gap.
- **No state-shape change.** `state.json` keeps its existing keys. Nothing in this plan adds, removes, or renames one.
- **`agent_call` on a successful attempt always carries the `model` key, even when the value is `null`.** `null` is the assertion "the backend routed itself" (spec §3). Only a *failed* attempt omits the key. A task that makes `model` conditional on being non-null has broken the field's one meaning.
- **`tokens` must never appear on any event.** `tests/test_orchestrator.py:583` already enforces this and must stay green; the spec reaffirms the decision (spec §3).
- **flock, never the `O_EXCL` PID protocol, for `index.json`** (spec §1). No staleness check, no `--force`, on the root lock.
- Commit messages: conventional style (`feat:`, `fix:`, `refactor:`, `test:`), **no attribution trailers of any kind**.
- All commands run from repo root `/home/bossbaby/Desktop/fix-me/ai-debate-lab`; Python is `.venv/bin/python`.
- Baseline before starting: `.venv/bin/python -m pytest -q` ⇒ **398 passed** in ~7.4s. The suite must stay single-digit seconds.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `debatelab/store.py:98-109` | `_lock_transition` → `_dir_lock`, generalized to any directory | 1 |
| `debatelab/store.py:240-253` | `rebuild_index`: mkdir hoisted above the lock, scan+write inside it | 1 |
| `debatelab/store.py:255-305` | `_acquire_lock` message gains the holder's command; `run_lock` → `debate_lock` | 2 |
| `debatelab/cli.py:50-89` | `cmd_run` passes `command="run"` | 2 |
| `debatelab/cli.py:215-269` | `cmd_decide` split into a lock wrapper + `_decide_locked` | 3 |
| `debatelab/cli.py:386-394` | `approve`/`reject` parsers gain `--force` | 3 |
| `debatelab/agents/base.py` | `Reply` dataclass; `Agent.ask` returns it | 4 |
| `debatelab/agents/cli_agent.py:22-61` | `ask` returns `Reply`; `_build_command` takes a model, not a task | 4 |
| `debatelab/agents/api_agent.py:134-152` | `ask` returns `Reply` | 4 |
| `debatelab/retry.py:16-47` | `on_attempt` gains a 4th argument: the result | 5 |
| `debatelab/orchestrator.py:180-196` | `_record_call` writes `model` on successful attempts | 5 |
| `debatelab/orchestrator.py:196-278` | `_reask`, `_ask_one`, `_fanout` unwrap `.text` | 5 |
| `tests/test_store.py` | Root-lock serialization, mkdir hoist, lock independence | 1 |
| `tests/test_lock.py` | `debate.lock` rename, `command` field | 2, 3 |
| `tests/test_cli.py` | approve/reject refusal under lock; the `no_consensus` race | 3 |
| `tests/test_cli_agent.py`, `tests/test_api_agent.py` | `.text` unwrapping; `Reply.model` resolution | 4 |
| `tests/test_retry.py` | `on_attempt` 4-arg signature | 5 |
| `tests/test_orchestrator.py` | `model` on `agent_call` | 5 |
| `tests/conftest.py:14-49` | `MockAgent` returns `Reply`; gains an optional `model` | 5 |

**Two facts that will bite whoever skips them:**

1. `_dir_lock` opens the directory to get an fd, so **the directory must exist before the lock is taken**. `rebuild_index` is reachable with no `debates/` at all (`list_ids` returns `[]` when the root is missing, `store.py:229-230`), and today's `mkdir` runs *after* the scan (`store.py:252`). Wrapping the body verbatim raises `FileNotFoundError` on a fresh checkout. Task 1 Step 1 pins this with a test.
2. `main()` does **not** catch `LockError` (`cli.py:410-417`) — `cmd_run` catches it locally (`cli.py:78-79`). `cmd_decide` must do the same or a locked debate dumps a traceback.

---

### Task 1: A root lock for `index.json`

**Files:**
- Modify: `debatelab/store.py:98-109` (rename `_lock_transition` → `_dir_lock`, generalize docstring)
- Modify: `debatelab/store.py:240-253` (`rebuild_index`)
- Modify: `debatelab/store.py:257`, `debatelab/store.py:279` (the two `_lock_transition` call sites)
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_dir_lock(path: Path)` — module-level context manager in `store.py`, flocks any directory. Replaces `_lock_transition`; no other name survives. `DebateStore.rebuild_index()` keeps its signature and gains serialization.

- [ ] **Step 1: Write the tests**

Append to `tests/test_store.py` (the file already imports `json`, `threading`, and `store as store_mod`):

```python
def test_rebuild_index_creates_the_root_when_it_does_not_exist(tmp_path):
    """_dir_lock needs an fd, so the mkdir must happen before the lock rather
    than after the scan. Passes today; it exists to fail loudly if the lock is
    wrapped around the whole body verbatim."""
    store = DebateStore(tmp_path / "debates")
    store.rebuild_index()
    assert json.loads((tmp_path / "debates" / "index.json").read_text()) == []


def test_rebuild_index_serializes_concurrent_rebuilds(tmp_path, monkeypatch):
    """Two rebuilds must not overlap: one that scans while another is between
    its own scan and write will write a stale index over a fresh one."""
    store = DebateStore(tmp_path / "debates")
    store.create("A", "problem")

    inside = threading.Event()
    release = threading.Event()
    real_list_ids = store.list_ids
    calls = []

    def list_ids_first_call_blocks():
        ids = real_list_ids()
        calls.append(1)
        if len(calls) == 1:  # only the first rebuild stalls in the section
            inside.set()
            assert release.wait(5), "test deadlocked waiting to release"
        return ids

    monkeypatch.setattr(store, "list_ids", list_ids_first_call_blocks)

    first = threading.Thread(target=store.rebuild_index)
    first.start()
    assert inside.wait(5), "first rebuild never entered the critical section"

    second = threading.Thread(target=store.rebuild_index)
    second.start()
    second.join(timeout=0.3)
    blocked = second.is_alive()

    release.set()
    first.join(5)
    second.join(5)
    assert blocked, "second rebuild ran while the first held the root lock"


def test_root_lock_does_not_block_on_an_unrelated_debate_lock(tmp_path):
    """The root and a debate directory are different inodes, so the two locks
    cannot form an ordering cycle. This is spec section 1's argument, executable."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("A", "problem")
    done = threading.Event()

    def rebuild():
        store.rebuild_index()
        done.set()

    with store_mod._dir_lock(store.path(did)):
        t = threading.Thread(target=rebuild)
        t.start()
        assert done.wait(5), "rebuild_index blocked on an unrelated debate lock"
        t.join(5)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_store.py -q -k "rebuild_index_creates_the_root or serializes_concurrent or unrelated_debate_lock"`

Expected:
- `test_rebuild_index_creates_the_root_when_it_does_not_exist` — **PASSES** (guard for Step 3's mkdir hoist).
- `test_rebuild_index_serializes_concurrent_rebuilds` — **FAILS**: `AssertionError: second rebuild ran while the first held the root lock`.
- `test_root_lock_does_not_block_on_an_unrelated_debate_lock` — **FAILS**: `AttributeError: module 'debatelab.store' has no attribute '_dir_lock'`.

- [ ] **Step 3: Rename the helper and lock the rebuild**

In `debatelab/store.py`, replace `_lock_transition` (lines 98-109) with:

```python
@contextlib.contextmanager
def _dir_lock(path: Path):
    """Serialize a check-then-act against everything under `path`.

    flock on a directory fd. The kernel releases it when the holder dies, so
    unlike the debate lock there is nothing stale to detect and nothing to
    force: this guards sections measured in milliseconds, where a competing
    process should block rather than be refused.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
```

Update both call sites — `store.py:257` in `_acquire_lock` and `store.py:279` in `_release_lock` — from `with _lock_transition(path.parent):` to:

```python
        with _dir_lock(path.parent):
```

Replace `rebuild_index` (lines 240-253) with:

```python
    def rebuild_index(self):
        # mkdir above the lock, not inside it: _dir_lock opens the directory
        # to get an fd, and rebuild_index is reachable before debates/ exists.
        self.root.mkdir(parents=True, exist_ok=True)
        with _dir_lock(self.root):
            entries = []
            for did in self.list_ids():
                state = self.read_state(did)
                entries.append(
                    {
                        "id": did,
                        "title": state["title"],
                        "status": state["status"],
                        "round": state["round"],
                    }
                )
            _atomic_write(
                self.root / "index.json", json.dumps(entries, indent=2)
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_store.py -q`
Expected: PASS, no failures.

Run the full suite: `.venv/bin/python -m pytest -q`
Expected: **401 passed** (398 + 3 new), still single-digit seconds.

- [ ] **Step 5: Commit**

```bash
git add debatelab/store.py tests/test_store.py
git commit -m "fix: serialize index.json rebuilds with a root lock

rebuild_index read every state then wrote the whole index with nothing
held, so two runs on different debates could each scan and then both
write, the second clobbering the first with a pre-scan view.

Generalizes _lock_transition into _dir_lock and takes it on the root for
the scan-and-write. flock, not the O_EXCL PID protocol the debate lock
uses: a millisecond section wants blocking, not staleness detection.

Closes the known limitation in the protocol-correctness spec's section 6."
```

---

### Task 2: `run.lock` becomes `debate.lock` and names its holder

**Files:**
- Modify: `debatelab/store.py:255-276` (`_acquire_lock`'s refusal message)
- Modify: `debatelab/store.py:283-305` (`run_lock` → `debate_lock`)
- Modify: `debatelab/cli.py:55` (`cmd_run`)
- Modify: `debatelab/cli.py:358-362` (`run --force` help text)
- Test: `tests/test_lock.py`

**Interfaces:**
- Consumes: `_dir_lock` (Task 1).
- Produces: `DebateStore.debate_lock(debate_id: str, *, command: str, force: bool = False)` — context manager yielding the holder `info` dict. `command` is keyword-only and required; it is the verb (`"run"`, `"approve"`, `"reject"`). Lock file is `debates/<id>/debate.lock`; payload is `{pid, host, started_at, run_id, command}`. `run_lock` no longer exists. Task 3 consumes this.

- [ ] **Step 1: Update the existing tests and add the new ones**

In `tests/test_lock.py`, change the helper (lines 12-15) to name the new file:

```python
def make(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")
    return store, did, tmp_path / "debates" / did / "debate.lock"
```

Then mechanically update every `store.run_lock(...)` call in the file to `store.debate_lock(..., command="run")`, and rename each test's `run_lock` prefix to `debate_lock`. The eight existing tests become:

```python
def test_debate_lock_writes_holder_info_and_removes_it_on_exit(tmp_path):
    store, did, lock = make(tmp_path)
    with store.debate_lock(did, command="run"):
        info = json.loads(lock.read_text())
        assert info["pid"] == os.getpid()
        assert info["host"] == socket.gethostname()
        assert info["started_at"] and info["run_id"]
    assert not lock.exists()


def test_debate_lock_is_released_when_the_run_raises(tmp_path):
    store, did, lock = make(tmp_path)
    with pytest.raises(RuntimeError):
        with store.debate_lock(did, command="run"):
            raise RuntimeError("boom")
    assert not lock.exists()


def test_debate_lock_refuses_a_second_holder(tmp_path):
    store, did, _ = make(tmp_path)
    with store.debate_lock(did, command="run"):
        with pytest.raises(LockError, match="locked by pid"):
            with store.debate_lock(did, command="run"):
                pass


def test_original_holder_exit_does_not_release_forced_replacement(tmp_path):
    store, did, lock = make(tmp_path)
    original = store.debate_lock(did, command="run")
    replacement = store.debate_lock(did, command="run", force=True)
    original.__enter__()
    replacement.__enter__()
    replacement_info = json.loads(lock.read_text())

    try:
        original.__exit__(None, None, None)

        assert json.loads(lock.read_text())["run_id"] == replacement_info["run_id"]
        with pytest.raises(LockError, match="locked by pid"):
            with store.debate_lock(did, command="run"):
                pass
    finally:
        replacement.__exit__(None, None, None)


def test_debate_lock_breaks_a_stale_same_host_lock(tmp_path):
    store, did, lock = make(tmp_path)
    lock.write_text(json.dumps({
        "pid": dead_pid(), "host": socket.gethostname(),
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
        "command": "run",
    }))
    with store.debate_lock(did, command="run"):
        assert json.loads(lock.read_text())["pid"] == os.getpid()
    assert not lock.exists()


def test_debate_lock_refuses_a_foreign_host_lock_unless_forced(tmp_path):
    store, did, lock = make(tmp_path)
    holder = json.dumps({
        "pid": 1, "host": "some-other-host",
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
        "command": "run",
    })
    lock.write_text(holder)
    with pytest.raises(LockError, match="--force"):
        with store.debate_lock(did, command="run"):
            pass
    lock.write_text(holder)
    with store.debate_lock(did, command="run", force=True):
        assert json.loads(lock.read_text())["pid"] == os.getpid()


def test_debate_lock_refuses_an_unreadable_lock_rather_than_guessing(tmp_path):
    """A half-written lock must not read as stale: breaking it would let two
    runs proceed. Refusing is the safe direction to err."""
    store, did, lock = make(tmp_path)
    lock.write_text("not json at all")
    with pytest.raises(LockError):
        with store.debate_lock(did, command="run"):
            pass


def test_debate_lock_reports_a_missing_debate_clearly(tmp_path):
    store = DebateStore(tmp_path / "debates")
    store.create("T", "problem")  # creates the root
    with pytest.raises(FileNotFoundError, match="no such debate"):
        with store.debate_lock("20260714-nope", command="run"):
            pass
```

Update the CLI test (lines 106-122) to patch the new name:

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

    monkeypatch.setattr(DebateStore, "debate_lock", locked)
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", did])
    assert "locked by pid 999" in str(exc.value)
```

Add two new tests at the end of the file:

```python
def test_debate_lock_records_the_command_that_holds_it(tmp_path):
    store, did, lock = make(tmp_path)
    with store.debate_lock(did, command="approve"):
        assert json.loads(lock.read_text())["command"] == "approve"


def test_lock_refusal_names_the_holders_command(tmp_path):
    """A refusal that can only say a PID leaves the operator asking 'doing
    what?'. The command is the answer."""
    store, did, _ = make(tmp_path)
    with store.debate_lock(did, command="run"):
        with pytest.raises(LockError, match="running `run`"):
            with store.debate_lock(did, command="approve"):
                pass


def test_lock_refusal_survives_a_holder_that_names_no_command(tmp_path):
    """A debate.lock written by a version that predates the command field must
    still produce a refusal, not a KeyError."""
    store, did, lock = make(tmp_path)
    lock.write_text(json.dumps({
        "pid": 1, "host": "some-other-host",
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
    }))
    with pytest.raises(LockError, match="running `\\?`"):
        with store.debate_lock(did, command="run"):
            pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_lock.py -q`
Expected: FAIL — `AttributeError: 'DebateStore' object has no attribute 'debate_lock'` on every test.

- [ ] **Step 3: Rename the lock and record the command**

In `debatelab/store.py`, update the refusal message inside `_acquire_lock` (lines 261-267):

```python
            except FileExistsError:
                holder = _read_lock(path)
                if not force and not _is_stale(holder):
                    raise LockError(
                        f"debate is locked by pid {holder.get('pid')} on "
                        f"{holder.get('host')} running "
                        f"`{holder.get('command') or '?'}` since "
                        f"{holder.get('started_at')}; "
                        "use --force if that process is dead"
                    )
```

Replace `run_lock` (lines 283-305) with:

```python
    @contextlib.contextmanager
    def debate_lock(self, debate_id: str, *, command: str, force: bool = False):
        """Hold debates/<id>/debate.lock for the duration of a mutation.

        The lock's subject is the debate, not the verb: run, approve, and
        reject all write state.json, so they must exclude each other. The
        status gate cannot do this job — state.json lags a live run by a
        whole phase (orchestrator.py:69 sets "running" in memory,
        orchestrator.py:149 first writes it).

        `command` is recorded so a refusal can name what holds the lock.
        """
        d = self.path(debate_id)
        if not (d / "state.json").exists():
            raise FileNotFoundError(f"no such debate: {debate_id}")
        path = d / "debate.lock"
        info = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _now(),
            "run_id": uuid.uuid4().hex,
            "command": command,
        }
        self._acquire_lock(path, info, force)
        try:
            yield info
        finally:
            self._release_lock(path, info["run_id"])
```

In `debatelab/cli.py`, update `cmd_run` line 55:

```python
        with store.debate_lock(args.id, command="run", force=args.force):
```

And the `run` parser's `--force` help (lines 358-362):

```python
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing debate lock (use only if that process is dead)",
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_lock.py -q`
Expected: PASS (11 tests: 8 renamed + 3 new).

Run the full suite: `.venv/bin/python -m pytest -q`
Expected: **404 passed**.

Confirm no caller of the old name survives:
Run: `grep -rn "run_lock\|run\.lock" debatelab/ tests/`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add debatelab/store.py debatelab/cli.py tests/test_lock.py
git commit -m "refactor: rename run.lock to debate.lock and record its holder

The lock's subject is the debate, not the verb that happens to hold it.
approve and reject write state.json too and are about to take the same
lock, at which point run.lock names the wrong thing.

The payload gains a command field so a refusal can say what it is
refusing for; a PID alone leaves the operator asking 'doing what?'.

A run.lock from a previous version is ignored, not migrated: reaching
that window means upgrading during a live run. See the spec's section 2."
```

---

### Task 3: `approve` and `reject` take the debate lock

**Files:**
- Modify: `debatelab/cli.py:215-269` (`cmd_decide` → lock wrapper + `_decide_locked`)
- Modify: `debatelab/cli.py:386-394` (`approve`/`reject` parsers gain `--force`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `DebateStore.debate_lock(id, *, command, force)` (Task 2).
- Produces: `cmd_decide(args, decision)` unchanged from the parser's point of view; `args.force` is now read. Module-level `_DECISION_COMMANDS: dict[str, str]` maps `"approved"→"approve"`, `"rejected"→"reject"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli.py`:

```python
def test_reject_refuses_while_a_run_holds_the_lock(workdir):
    """The spec's headline race. A run marks itself 'running' in memory
    (orchestrator.py:69) but does not checkpoint until its first phase ends
    (orchestrator.py:149), so state.json still reads no_consensus for minutes
    while the run is live. Without the lock the status gate lets the human
    through and the run's first checkpoint then overwrites the decision,
    leaving it in the transcript and not in the checkpoint."""
    store = DebateStore(workdir / "debates")
    debate_id = store.create("T", "problem")
    state = store.read_state(debate_id)
    state["status"] = "no_consensus"
    store.write_state(debate_id, state)

    with store.debate_lock(debate_id, command="run"):
        before = len(store.read_events(debate_id))
        with pytest.raises(SystemExit) as exc:
            cli.main(["reject", debate_id, "-m", "not convincing"])

    assert "locked by pid" in str(exc.value)
    assert "running `run`" in str(exc.value)
    assert store.read_state(debate_id)["status"] == "no_consensus"
    assert len(store.read_events(debate_id)) == before


def test_approve_refuses_while_a_run_holds_the_lock(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    with store.debate_lock(debate_id, command="run"):
        with pytest.raises(SystemExit) as exc:
            cli.main(["approve", debate_id, "-m", "looks right"])
    assert "locked by pid" in str(exc.value)
    assert store.read_state(debate_id)["status"] == "awaiting_human"


def test_approve_force_breaks_a_stale_lock(workdir, capsys):
    """A refusal a human cannot override is a wedged debate."""
    store, debate_id = _make_awaiting(workdir, capsys)
    lock = workdir / "debates" / debate_id / "debate.lock"
    lock.write_text(json.dumps({
        "pid": 1, "host": "some-other-host",
        "started_at": "2026-07-14T00:00:00+00:00", "run_id": "old",
        "command": "run",
    }))
    cli.main(["approve", debate_id, "-m", "ok", "--force"])
    assert store.read_state(debate_id)["status"] == "approved"


def test_approve_releases_the_lock_when_it_finishes(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    cli.main(["approve", debate_id, "-m", "ok"])
    assert not (workdir / "debates" / debate_id / "debate.lock").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q -k "holds_the_lock or force_breaks_a_stale or releases_the_lock"`

Expected: FAIL.
- `test_reject_refuses_while_a_run_holds_the_lock` — `Failed: DID NOT RAISE <class 'SystemExit'>`. This failure **is** the bug: the reject succeeded against a debate a run was holding.
- `test_approve_force_breaks_a_stale_lock` — `SystemExit: unrecognized arguments: --force`.

- [ ] **Step 3: Put the decide path under the lock**

In `debatelab/cli.py`, add the mapping next to the other module-level constants (above `cmd_decide`):

```python
# `decision` is state.json's vocabulary ("approved"); `command` is the verb the
# human typed ("approve"). The lock records the verb.
_DECISION_COMMANDS = {"approved": "approve", "rejected": "reject"}
```

Replace `cmd_decide`'s opening (lines 215-217) so the lock covers the read — a lock taken after `read_state` protects nothing, because the stale read is the defect:

```python
def cmd_decide(args, decision):
    store = get_store()
    try:
        with store.debate_lock(
            args.id, command=_DECISION_COMMANDS[decision], force=args.force
        ):
            _decide_locked(store, args, decision)
    except LockError as e:
        # main() does not catch LockError (cli.py:410-417); cmd_run catches it
        # locally for the same reason.
        sys.exit(str(e))


def _decide_locked(store, args, decision):
    state = store.read_state(args.id)
```

Everything from the old line 218 (`note = args.message or ""`) through the old line 269 moves verbatim into `_decide_locked`, **at exactly its current indentation** — it was already one level deep inside `cmd_decide` and it is one level deep inside `_decide_locked`. Not one character of that body changes; only its enclosing `def` and the two lines above it do. The `store = get_store()` line does not move: `cmd_decide` keeps it and passes `store` in.

Verify `LockError` is imported in `cli.py`. It is (used by `cmd_run` at line 78); no import change needed.

Add `--force` to both parsers (lines 386-394):

```python
    sp = sub.add_parser("approve", help="approve the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", default="")
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing debate lock (use only if that process is dead)",
    )
    sp.set_defaults(fn=lambda a: cmd_decide(a, "approved"))

    sp = sub.add_parser("reject", help="reject the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
    sp.add_argument(
        "--force",
        action="store_true",
        help="break an existing debate lock (use only if that process is dead)",
    )
    sp.set_defaults(fn=lambda a: cmd_decide(a, "rejected"))
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli.py -q`
Expected: PASS.

Run the full suite: `.venv/bin/python -m pytest -q`
Expected: **408 passed**.

- [ ] **Step 5: Commit**

```bash
git add debatelab/cli.py tests/test_cli.py
git commit -m "fix: hold the debate lock across approve and reject

cmd_decide read state.json, mutated it, and wrote it back with nothing
held. The protocol-correctness spec deferred locking these on the ground
that they are gated on a status a run does not produce concurrently.

The gate is real but reads the wrong thing: a run sets 'running' in
memory and does not checkpoint until its first phase ends, so a
no_consensus debate stays approvable for minutes while a run is live.
The run's first checkpoint then overwrites the human decision, leaving
it in the transcript and absent from the checkpoint -- the divergence
fsck exists to report.

approve and reject gain --force: a refusal a human cannot override is a
wedged debate."
```

---

### Task 4: `Reply` and the adapters

**Files:**
- Modify: `debatelab/agents/base.py` (add `Reply`, change `Agent.ask`'s contract)
- Modify: `debatelab/agents/cli_agent.py:22-61`
- Modify: `debatelab/agents/api_agent.py:134-152`
- Test: `tests/test_cli_agent.py`, `tests/test_api_agent.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `debatelab.agents.base.Reply` — a frozen dataclass with `text: str` and `model: str | None = None`. `Agent.ask(prompt: str, task: str = models.DEEP) -> Reply` is abstract and every adapter returns one. `CliAgent._build_command(prompt: str, model: str | None) -> list[str]` now takes a resolved model rather than a task. Task 5 consumes `Reply`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_agent.py`:

```python
def test_cli_agent_reply_carries_no_model_when_it_routes_itself(tmp_path):
    """None is a fact, not a hole: it says the CLI picked its own model."""
    script = make_script(tmp_path, 'echo "reply"')
    agent = CliAgent("stub", [script, "{prompt}"])
    reply = agent.ask("hello")
    assert reply.text == "reply"
    assert reply.model is None


def test_cli_agent_reply_carries_the_resolved_model(tmp_path):
    script = make_script(tmp_path, 'echo "reply"')
    lister = make_script(tmp_path, 'echo "gemini-3-pro"; echo "gemini-3-flash"')
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    assert agent.ask("hello", DEEP).model == "gemini-3-pro"


def test_cli_agent_routes_deep_and_fast_to_different_models(tmp_path):
    """The assertion the model field exists for: choose_model's DEEP/FAST
    routing is otherwise unverifiable from a transcript."""
    script = make_script(tmp_path, 'echo "reply"')
    lister = make_script(tmp_path, 'echo "gemini-3-pro"; echo "gemini-3-flash"')
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    deep = agent.ask("hello", DEEP).model
    fast = agent.ask("hello", FAST).model
    assert deep != fast
    assert {deep, fast} == {"gemini-3-pro", "gemini-3-flash"}


def test_reply_is_immutable():
    from dataclasses import FrozenInstanceError

    from debatelab.agents.base import Reply

    reply = Reply(text="hi", model="m")
    with pytest.raises(FrozenInstanceError):
        reply.model = "other"
```

Append to `tests/test_api_agent.py`:

```python
def test_api_agent_reply_carries_the_pinned_model(monkeypatch):
    """A pinned model needs no discovery, and the reply reports what was sent."""
    monkeypatch.setenv("KEY", "k")
    agent = ApiAgent("a", "openai", model="gpt-5", api_key_env="KEY")
    monkeypatch.setattr(
        ApiAgent,
        "_request",
        lambda self, url, headers, body=None: {
            "choices": [{"message": {"content": "hi"}}]
        },
    )
    reply = agent.ask("prompt")
    assert reply.text == "hi"
    assert reply.model == "gpt-5"
```

Then unwrap every existing assertion in both files. Find them all:

```bash
grep -n "\.ask(" tests/test_cli_agent.py tests/test_api_agent.py
```

That is 42 call sites (17 and 25). The rule is mechanical: **a call site whose return value is used gains `.text`; a call site inside a `pytest.raises` block does not change**, because it never touches the return value. Work top-to-bottom through the grep output so none is missed — a skipped one shows up as `AttributeError: 'Reply' object has no attribute ...` or a comparison against a `Reply`, not as a silent pass.

For example, `tests/test_cli_agent.py:27`:

```python
def test_cli_agent_returns_stripped_stdout(tmp_path):
    script = make_script(tmp_path, 'echo "reply to: $1"')
    agent = CliAgent("stub", [script, "{prompt}"])
    assert agent.ask("hello").text == "reply to: hello"
```


- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_agent.py tests/test_api_agent.py -q`
Expected: FAIL — `AttributeError: 'str' object has no attribute 'text'` and `ImportError: cannot import name 'Reply' from 'debatelab.agents.base'`.

- [ ] **Step 3: Add `Reply` and return it from both adapters**

In `debatelab/agents/base.py`, add the import and the dataclass, and change the abstract method:

```python
"""Minimal agent interface every backend adapter implements."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from . import models
```

```python
@dataclass(frozen=True)
class Reply:
    """One agent call's result.

    `model` is what the backend resolved for this call, or None when it was
    left to route itself (a CLI with no models_command). None means "we
    pinned nothing" and never "we forgot to look" -- which is why ask() is
    abstract and returns this, rather than the model being available through
    an optional accessor an adapter could decline to implement. A field with
    two meanings, one of them a lie, is the defect that keeps `tokens` out.

    Returning the model rather than exposing it also keeps it off any shared
    object: _fanout calls the roster concurrently, and a value on the
    caller's stack cannot be raced.
    """

    text: str
    model: str | None = None


class Agent(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        """Send a prompt, return the agent's reply. Raises AgentError on
        failure. `task` (models.DEEP or models.FAST) lets the backend pick
        the most appropriate model for the work."""
```

In `debatelab/agents/cli_agent.py`, resolve the model in `ask` and pass it down, so the value that was already computed can be reported:

```python
from .base import Agent, AgentError, ErrorKind, Reply
```

```python
    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        model = self._model_for(task)
        cmd = self._build_command(prompt, model)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise AgentError(
                f"{self.name}: timed out after {self.timeout}s",
                kind=ErrorKind.TIMEOUT,
            )
        except FileNotFoundError:
            raise AgentError(
                f"{self.name}: command not found: {cmd[0]}",
                kind=ErrorKind.NOT_FOUND,
            )
        if proc.returncode != 0:
            raise AgentError(
                f"{self.name}: exit {proc.returncode}: {proc.stderr.strip()[:500]}",
                kind=ErrorKind.UNKNOWN,
            )
        return Reply(text=proc.stdout.strip(), model=model)

    def _build_command(self, prompt: str, model: str | None) -> list[str]:
        """Substitute {prompt} and {model}. A token containing {model} is
        dropped entirely when no model was selected, so the CLI falls back
        to its own default routing."""
        cmd = []
        for part in self.command:
            if "{model}" in part:
                if model is None:
                    continue
                part = part.replace("{model}", model)
            cmd.append(part.replace("{prompt}", prompt))
        return cmd
```

In `debatelab/agents/api_agent.py`:

```python
from .base import Agent, AgentError, ErrorKind, Reply
```

```python
    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise AgentError(
                f"{self.name}: env var {self.api_key_env} is not set",
                kind=ErrorKind.AUTH,
            )
        build, parse = DRIVERS[self.provider]
        model = self._model_for(task, api_key)
        url, headers, body = build(model, self.base_url, api_key, prompt)
        data = self._request(url, headers, body)
        try:
            return Reply(text=parse(data).strip(), model=model)
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise AgentError(
                f"{self.name}: unexpected response shape: {e!r}",
                kind=ErrorKind.BAD_RESPONSE,
            )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_agent.py tests/test_api_agent.py -q`
Expected: PASS.

The orchestrator and `conftest.py` are still on the old contract, so the full suite fails until Task 5. Confirm the failures are only that:
Run: `.venv/bin/python -m pytest -q 2>&1 | tail -5`
Expected: failures in `test_orchestrator.py` / `test_integration.py` / `test_cli.py` from `MockAgent` returning `str`. This is the one point in the plan where the suite is red between tasks; Task 5 closes it.

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/base.py debatelab/agents/cli_agent.py \
        debatelab/agents/api_agent.py tests/test_cli_agent.py \
        tests/test_api_agent.py
git commit -m "feat: have ask() return a Reply carrying the resolved model

The agent-reliability spec deferred the model field because telemetry
would have to pull it out of the agent: a public accessor that can do
network I/O and raise inside the telemetry path, or a last_model
attribute mutated on an object _fanout shares across threads.

Both objections assume the pull. A return value adds no I/O -- both
adapters already resolve the model on the call's own path -- and rides
the caller's stack, so no thread can race it.

ask() is abstract rather than an optional ask_detailed() so that
model=None means exactly one thing: the backend routed itself.

The orchestrator still expects a str; the next commit moves it."
```

---

### Task 5: `model` on `agent_call`

**Files:**
- Modify: `debatelab/retry.py:16-47` (`on_attempt` gains the result)
- Modify: `debatelab/orchestrator.py:180-196` (`_record_call`)
- Modify: `debatelab/orchestrator.py:196-216` (`_reask`), `:227-246` (`_ask_one`), `:248-278` (`_fanout`)
- Modify: `tests/conftest.py:14-49` (`MockAgent`)
- Test: `tests/test_retry.py`, `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `Reply` (Task 4).
- Produces: `on_attempt(attempt: int, duration_ms: int, error: AgentError | None, result)` — exactly one of `error`/`result` is `None`. `agent_call` events gain `"model"` on successful attempts. `MockAgent(name, responses, synthesis=None, model=None)` returns `Reply(text=..., model=self.model)`.

- [ ] **Step 1: Write the failing tests**

Update the two `on_attempt` lambdas in `tests/test_retry.py` (lines 166 and 179) to the 4-argument signature:

```python
        on_attempt=lambda attempt, ms, err, result: seen.append((attempt, err)),
```

```python
        on_attempt=lambda attempt, ms, err, result: seen.append(ms),
```

Append to `tests/test_retry.py`:

```python
def test_on_attempt_receives_the_result_on_success_and_none_on_failure():
    """The event is built in on_attempt, so whatever the call returned has to
    reach it. Exactly one of error/result is set."""
    seen = []
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("boom", kind=ErrorKind.SERVER_ERROR)
        return "the reply"

    retry.call_with_retry(
        flaky,
        rng=random.Random(0),
        sleep=Clock(),
        on_attempt=lambda attempt, ms, err, result: seen.append((err, result)),
    )
    assert len(seen) == 2
    assert seen[0][0] is not None and seen[0][1] is None
    assert seen[1][0] is None and seen[1][1] == "the reply"
```

Append to `tests/test_orchestrator.py`:

```python
def test_agent_call_events_carry_the_resolved_model(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b")]
    for agent in agents:
        agent.model = "test-model-1"
    Orchestrator(store, agents).run(did, max_rounds=1)
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["ok"]
    ]
    assert calls
    assert all(e["model"] == "test-model-1" for e in calls)


def test_agent_call_records_a_null_model_rather_than_dropping_the_key(tmp_path):
    """null is the claim 'the backend routed itself'. An absent key would make
    that indistinguishable from 'this adapter never reported', which is the
    ambiguity the field is built to avoid."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["ok"]
    ]
    assert calls
    assert all("model" in e for e in calls)
    assert all(e["model"] is None for e in calls)


def test_failed_agent_call_omits_the_model_key(tmp_path):
    """AgentError does not know the model, and guessing one would be worse
    than the absence. This is the only case where absence is correct."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), MockAgent("c", [])]
    ).run(did, max_rounds=1)
    failed = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and not e["ok"]
    ]
    assert failed
    assert all("model" not in e for e in failed)
```

No import change is needed: `tests/test_orchestrator.py:11` already reads `from .conftest import MockAgent, happy_agent, make_store`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_retry.py tests/test_orchestrator.py -q`
Expected: FAIL — `TypeError: <lambda>() missing 1 required positional argument: 'result'` from `retry.py`, and `KeyError: 'model'` / `AttributeError: 'str' object has no attribute 'text'` from the orchestrator.

- [ ] **Step 3: Thread the reply through**

In `debatelab/retry.py`, pass the outcome to the hook (lines 30-47):

```python
        try:
            result = fn()
        except AgentError as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if on_attempt is not None:
                on_attempt(attempt, elapsed_ms, e, None)
            if not e.retryable or attempt == max_attempts:
                raise
            if e.retry_after is not None:
                delay = max(0.0, min(e.retry_after, cap))
            else:
                delay = backoff_delay(retry_index, rng, base, cap)
            sleep(delay)
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if on_attempt is not None:
                on_attempt(attempt, elapsed_ms, None, result)
            return result
```

Update the docstring's first line to name the contract:

```python
    """Call fn() until it returns or its AgentError is not worth retrying.

    on_attempt(attempt, duration_ms, error, result) fires once per attempt;
    exactly one of error/result is None.
    """
```

In `debatelab/orchestrator.py`, `_record_call` (lines 180-196):

```python
    def _record_call(self, debate_id, state, phase, name, task):
        """Build the on_attempt hook that logs one agent_call per attempt."""
        def on_attempt(attempt, duration_ms, error, reply):
            event = {
                "round": state["round"], "phase": phase, "agent": name,
                "type": "agent_call", "task": task, "attempt": attempt,
                "duration_ms": duration_ms, "ok": error is None,
                "content": "",
            }
            if error is not None:
                # No model: AgentError does not carry one, and attaching it at
                # the raise sites in _request would be instrumenting the error
                # path for a question nobody has asked. `kind` covers why.
                event["kind"] = error.kind.value
                event["content"] = str(error)
            else:
                # Always set, even when None: null is the claim "the backend
                # routed itself", not an absence.
                event["model"] = reply.model
            self.store.append_event(debate_id, event)

        return on_attempt
```

`_reask` (lines 203-216):

```python
        try:
            reply = retry.call_with_retry(
                lambda: self.agents[name].ask(
                    prompts.reask(prompt, required), task
                ),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
        except AgentError:
            return None, None
        return parse(reply.text), reply.text
```

`_ask_one` (lines 235-246):

```python
        try:
            reply = retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
        except AgentError as e:
            return None, str(e)
        return reply.text, None
```

`_fanout`'s inner `call` (lines 253-262) — unwrap here so `results[name]` stays the reply text and nothing downstream changes:

```python
        def call(name):
            prompt = prompt_for(name)
            reply = retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
            return reply.text
```

In `tests/conftest.py`, `MockAgent` returns `Reply`:

```python
from debatelab.agents.base import Agent, AgentError, Reply
```

```python
    def __init__(self, name, responses, synthesis=None, model=None):
        super().__init__(name)
        self.responses = list(responses)
        self.synthesis = synthesis
        self.model = model
        self.prompts = []
        self.tasks = []

    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        self.prompts.append(prompt)
        self.tasks.append(task)
        if prompts.SYNTHESIS_HEADER in prompt:
            item = self.synthesis
            if item is None:
                item = f"synthesis from {self.name}"
            if isinstance(item, Exception):
                raise item
            return Reply(text=item, model=self.model)
        if not self.responses:
            raise AgentError(f"{self.name}: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Reply(text=item, model=self.model)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: **417 passed**, still single-digit seconds. The arithmetic, so a mismatch is diagnosable rather than mysterious:

| After | Added | Total |
|---|---|---|
| baseline | — | 398 |
| Task 1 | +3 | 401 |
| Task 2 | +3 (the 8 existing tests are renamed, not added) | 404 |
| Task 3 | +4 | 408 |
| Task 4 | +5 (4 in `test_cli_agent.py`, 1 in `test_api_agent.py`) | 413 |
| Task 5 | +4 (the 2 `on_attempt` lambdas are edited, not added) | 417 |

If the count differs, reconcile before committing — a silently skipped test is not a pass.

Confirm the reaffirmed decision still holds:
Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -q -k "never_claim_a_token_count"`
Expected: PASS.

Confirm no adapter still returns a bare string:
Run: `grep -rn "return proc.stdout\|return parse(data)" debatelab/agents/`
Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add debatelab/retry.py debatelab/orchestrator.py tests/conftest.py \
        tests/test_retry.py tests/test_orchestrator.py
git commit -m "feat: record the resolved model on agent_call events

choose_model routes DEEP to the strongest model and FAST to the cheapest,
and agents.yaml is built entirely around that promise. Nothing recorded
which model was actually chosen, so the routing either worked or silently
did not and no transcript could tell the two apart.

on_attempt gains the result so the event can be built from it. A
successful attempt always carries model, including when it is null: null
is the claim 'the backend routed itself'. Failed attempts omit the key --
AgentError does not know the model and kind already covers why.

tokens stays out: a subprocess reports no usage, and the default roster is
CLI-backed, so the column would read near-zero while money was spent."
```

---

## Verification

After Task 5, confirm the whole spec landed:

```bash
.venv/bin/python -m pytest -q                    # 415 passed
grep -rn "run_lock\|run\.lock" debatelab/ tests/  # no output
grep -rn "_lock_transition" debatelab/            # no output
git log --oneline -5
```

Then drive the real thing once — the suite uses `MockAgent`, which cannot show that a real CLI's model resolution reaches the transcript:

```bash
.venv/bin/python -m debatelab.cli new "Does the model field reach the transcript?"
# then, with the printed id:
.venv/bin/python -m debatelab.cli run <id> --max-rounds 1
grep '"type": "agent_call"' debates/<id>/transcript.jsonl | head -3
```

Expect every successful `agent_call` to carry a `model` key: `null` for `claude` (no `models_command`), a resolved name for `antigravity`. A missing key on a successful call means Task 5's `else` branch was made conditional — the one mistake this plan's constraints exist to prevent.
