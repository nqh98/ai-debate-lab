# Workspace-Grounded Debates Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let CLI-backed debate agents read, grep, and run verification commands inside a disposable git-worktree checkout of the repository under debate, with opt-in (default: none) timeouts and live stall alerts replacing proactive cancellation.

**Architecture:** `debate new --repo PATH` pins `{source, commit}` into the debate record; `debate run` materializes `debates/<id>/workspace/` as a detached git worktree and constructs `CliAgent`s with `cwd` pointing at it plus per-platform `workspace_args` from `agents.yaml`. Prompts gain a grounding preamble (attached agents) or a no-access notice (API agents). A `LiveStatus` heartbeat thread prints elapsed/stall lines and rewrites an ephemeral `live.json` the viewer polls.

**Tech Stack:** Python 3.12 stdlib only (subprocess, threading, argparse), pytest, node for viewer render tests. No new dependencies.

**Spec:** `specs/2026-07-15-workspace-grounded-debates-design.md` — read it first; every task cites its sections.

## Global Constraints

- No proactive timeout cancellation by default: `timeout` absent ⇒ `subprocess.run(..., timeout=None)` / `urlopen(..., timeout=None)`. The old implicit 180s default is deliberately removed (spec §3).
- Model discovery (`models_command`, provider models endpoints) keeps a fixed 30s timeout (spec §3).
- `stall_after` defaults: deep 900s, fast 300s. A stall alert never cancels (spec §6).
- Heartbeat interval 60s; viewer treats `live.json` older than 2 intervals (120s) as "run process not responding" (spec §6).
- The `workspace` state key exists **only** in repo-grounded debates — never write it (even as null) for plain debates, or `debate fsck` diverges on every pre-existing debate.
- Transcript event vocabulary additions: `workspace_ready` (audit-only). Any new event type MUST be handled in `replay.py` (fold rule or `AUDIT_ONLY`) in the same commit that emits it.
- Run the full suite (`.venv/bin/python -m pytest -q`) before every commit; all 417 pre-existing tests must stay green.
- Commit messages follow repo style (`feat:`/`fix:`/`docs:` prefix), no AI-attribution trailers.

---

### Task 1: Opt-in per-task timeouts

**Files:**
- Modify: `debatelab/agents/registry.py` (AgentSpec.timeout, parser)
- Modify: `debatelab/agents/cli_agent.py` (per-task ceiling, discovery constant)
- Modify: `debatelab/agents/api_agent.py` (per-task ceiling, discovery constant)
- Test: `tests/test_registry_timeouts.py` (new), plus existing suites stay green

**Interfaces:**
- Consumes: nothing new.
- Produces: `AgentSpec.timeout: dict` normalized to `{"fast": int|None, "deep": int|None}`; `registry._parse_task_seconds(raw, *, field_name, path, name, default_fast, default_deep) -> dict`; `CliAgent(name, command, timeout=None, models_command=None)` where `timeout` is that dict (a bare int is normalized for back-compat); same for `ApiAgent(..., timeout=None)`; `cli_agent.MODELS_DISCOVERY_TIMEOUT = 30`, `api_agent.MODELS_DISCOVERY_TIMEOUT = 30`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_registry_timeouts.py`:

```python
"""Timeouts are opt-in ceilings: absent means run forever (spec §3)."""
import pytest

from debatelab.agents import registry


def write_config(tmp_path, timeout_yaml=""):
    body = "agents:\n  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
    if timeout_yaml:
        body += f"    timeout: {timeout_yaml}\n"
    p = tmp_path / "agents.yaml"
    p.write_text(body)
    return p


def test_absent_timeout_means_no_ceiling(tmp_path):
    spec = registry.load_agent_specs(write_config(tmp_path))[0]
    assert spec.timeout == {"fast": None, "deep": None}


def test_int_timeout_applies_to_both_tiers(tmp_path):
    spec = registry.load_agent_specs(write_config(tmp_path, "240"))[0]
    assert spec.timeout == {"fast": 240, "deep": 240}


def test_map_timeout_sets_tiers_independently(tmp_path):
    spec = registry.load_agent_specs(
        write_config(tmp_path, "{fast: 120, deep: null}")
    )[0]
    assert spec.timeout == {"fast": 120, "deep": None}


def test_map_timeout_missing_tier_defaults_to_none(tmp_path):
    spec = registry.load_agent_specs(write_config(tmp_path, "{fast: 120}"))[0]
    assert spec.timeout == {"fast": 120, "deep": None}


def test_unknown_timeout_key_is_config_error(tmp_path):
    with pytest.raises(registry.ConfigError):
        registry.load_agent_specs(write_config(tmp_path, "{slow: 5}"))


def test_non_numeric_timeout_is_config_error(tmp_path):
    with pytest.raises(registry.ConfigError):
        registry.load_agent_specs(write_config(tmp_path, "soon"))
```

Add to `tests/test_api_agent.py` (match its existing import style):

```python
def test_ask_uses_per_task_timeout(monkeypatch):
    """The deep ceiling reaches urlopen; None means block forever."""
    seen = {}

    class FakeResp:
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self):
            return b'{"choices":[{"message":{"content":"hi"}}]}'

    def fake_urlopen(req, timeout="MISSING"):
        seen["timeout"] = timeout
        return FakeResp()

    monkeypatch.setenv("K", "secret")
    monkeypatch.setattr(
        "debatelab.agents.api_agent.urllib.request.urlopen", fake_urlopen
    )
    agent = ApiAgent(
        "x", "openai", model="m", api_key_env="K",
        timeout={"fast": 7, "deep": None},
    )
    agent.ask("p", task=models.DEEP)
    assert seen["timeout"] is None
    agent.ask("p", task=models.FAST)
    assert seen["timeout"] == 7
```

Add to `tests/test_agent_error.py` (or wherever `CliAgent` construction is tested; use a real subprocess like neighboring tests):

```python
def test_cli_agent_picks_ceiling_by_task(monkeypatch):
    seen = {}
    real_run = subprocess.run

    def spy_run(cmd, **kw):
        seen["timeout"] = kw.get("timeout", "MISSING")
        return real_run(cmd, **kw)

    monkeypatch.setattr("debatelab.agents.cli_agent.subprocess.run", spy_run)
    agent = CliAgent("x", ["echo", "{prompt}"], timeout={"fast": 5, "deep": None})
    agent.ask("hi", task=models.DEEP)
    assert seen["timeout"] is None
    agent.ask("hi", task=models.FAST)
    assert seen["timeout"] == 5


def test_cli_agent_normalizes_int_timeout():
    agent = CliAgent("x", ["echo", "{prompt}"], timeout=9)
    assert agent.timeout == {"fast": 9, "deep": 9}
```

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_registry_timeouts.py tests/test_api_agent.py tests/test_agent_error.py -q`
Expected: new tests FAIL (`spec.timeout == 180`, `TypeError` on dict timeout).

- [ ] **Step 3: Implement**

`registry.py` — replace `timeout: int = 180` on `AgentSpec` and add the parser (needs `from dataclasses import dataclass, field`):

```python
@dataclass
class AgentSpec:
    # ... existing fields unchanged ...
    timeout: dict = field(
        default_factory=lambda: {"fast": None, "deep": None}
    )


def _parse_task_seconds(raw, *, field_name, path, name,
                        default_fast, default_deep):
    """Normalize an int-or-{fast,deep} YAML value to a per-tier dict.

    Absent -> the given defaults. A bare int applies to both tiers. A map
    may set either tier to an int or null."""
    if raw is None:
        return {"fast": default_fast, "deep": default_deep}
    if isinstance(raw, bool):
        raise ConfigError(
            f"{path}: agent '{name}': {field_name} must be a number or "
            f"a {{fast, deep}} map"
        )
    if isinstance(raw, int):
        return {"fast": raw, "deep": raw}
    if isinstance(raw, dict):
        unknown = set(raw) - {"fast", "deep"}
        if unknown:
            raise ConfigError(
                f"{path}: agent '{name}': {field_name} has unknown "
                f"key(s): {', '.join(sorted(unknown))}"
            )
        out = {}
        for tier, default in (("fast", default_fast), ("deep", default_deep)):
            value = raw.get(tier, default)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ConfigError(
                    f"{path}: agent '{name}': {field_name}.{tier} must be "
                    f"a number or null"
                )
            out[tier] = value
        return out
    raise ConfigError(
        f"{path}: agent '{name}': {field_name} must be a number or "
        f"a {{fast, deep}} map"
    )
```

In `load_agent_specs`, replace `timeout=int(entry.get("timeout", 180)),` with:

```python
                timeout=_parse_task_seconds(
                    entry.get("timeout"), field_name="timeout",
                    path=path, name=name,
                    default_fast=None, default_deep=None,
                ),
```

`cli_agent.py` — module constant, normalized constructor, per-task pick:

```python
MODELS_DISCOVERY_TIMEOUT = 30


def _normalize_timeout(timeout):
    if timeout is None:
        return {"fast": None, "deep": None}
    if isinstance(timeout, int):
        return {"fast": timeout, "deep": timeout}
    return dict(timeout)
```

Constructor: `timeout: dict | int | None = None`, store `self.timeout = _normalize_timeout(timeout)`. In `ask`:

```python
        ceiling = self.timeout.get(task)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=ceiling,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            raise AgentError(
                f"{self.name}: timed out after {ceiling}s",
                kind=ErrorKind.TIMEOUT,
            )
```

In `_discover_models`, replace `timeout=self.timeout` with `timeout=MODELS_DISCOVERY_TIMEOUT`.

`api_agent.py` — same `MODELS_DISCOVERY_TIMEOUT = 30` and `_normalize_timeout` (duplicate the 6-line helper; importing across adapters for this is not worth the coupling). Constructor `timeout=None`, store normalized. `_request` gains an explicit keyword: `def _request(self, url, headers, body=None, *, timeout):` and passes it to `urlopen(req, timeout=timeout)`. Call sites: in `ask`, `data = self._request(url, headers, body, timeout=self.timeout.get(task))`; both `_model_for` discovery calls pass `timeout=MODELS_DISCOVERY_TIMEOUT`.

`registry.build_agents` needs no change yet — `spec.timeout` is already the dict both constructors now accept.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. If a pre-existing test constructed `CliAgent`/`ApiAgent` with an int timeout, the normalizer covers it; if one asserts `spec.timeout == 180`, update that assertion to the new default `{"fast": None, "deep": None}` — the default change is the point of the task (spec §3 "Behavior change").

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/registry.py debatelab/agents/cli_agent.py debatelab/agents/api_agent.py tests/
git commit -m "feat: make agent timeouts opt-in per-task ceilings"
```

---

### Task 2: Workspace pin — `debate new --repo`, genesis record, replay

**Files:**
- Create: `debatelab/workspace.py` (pin only; lifecycle comes in Task 3)
- Modify: `debatelab/store.py` (`create` gains `workspace=`)
- Modify: `debatelab/replay.py` (genesis fold, `workspace_ready` audit-only)
- Modify: `debatelab/cli.py` (`--repo` on `new`)
- Test: `tests/test_workspace.py` (new), `tests/test_replay.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `workspace.WorkspaceError(Exception)`; `workspace.pin(source: str) -> dict` returning `{"source": <abs path str>, "commit": <full sha>}`; `workspace._git(args: list[str]) -> str` (shared by Task 3); `DebateStore.create(title, problem, context_texts=(), workspace=None)`; state/genesis key `"workspace"` present **only** when grounded.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workspace.py`:

```python
"""Workspace pinning: --repo resolves to {source, commit} (spec §1)."""
import subprocess

import pytest

from debatelab import workspace


def make_repo(tmp_path, name="src"):
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    (repo / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "init"],
        check=True,
    )
    return repo


def test_pin_resolves_source_and_head(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    assert ws["source"] == str(repo.resolve())
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert ws["commit"] == head


def test_pin_rejects_non_directory(tmp_path):
    with pytest.raises(workspace.WorkspaceError):
        workspace.pin(str(tmp_path / "missing"))


def test_pin_rejects_non_git_directory(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(workspace.WorkspaceError):
        workspace.pin(str(plain))
```

Add to `tests/test_store.py`:

```python
def test_create_with_workspace_records_pin(tmp_path):
    store = DebateStore(tmp_path / "debates")
    ws = {"source": "/some/repo", "commit": "a" * 40}
    did = store.create("t", "p", workspace=ws)
    assert store.read_state(did)["workspace"] == ws
    assert store.read_events(did)[0]["workspace"] == ws


def test_create_without_workspace_has_no_key(tmp_path):
    """Plain debates must stay byte-compatible: no workspace key at all,
    or fsck diverges on every debate created before this feature."""
    store = DebateStore(tmp_path / "debates")
    did = store.create("t", "p")
    assert "workspace" not in store.read_state(did)
    assert "workspace" not in store.read_events(did)[0]
```

Add to `tests/test_replay.py` (reuse its genesis-building helpers if present; the shape below is self-contained):

```python
def test_replay_folds_workspace_from_genesis():
    ws = {"source": "/some/repo", "commit": "a" * 40}
    events = [{
        "type": "debate_created", "round": 0, "phase": "create",
        "agent": None, "content": "t", "id": "d", "title": "t",
        "max_rounds": 5, "quorum": "2/3", "workspace": ws,
    }]
    assert replay.replay(events)["workspace"] == ws


def test_replay_without_workspace_key_stays_keyless():
    events = [{
        "type": "debate_created", "round": 0, "phase": "create",
        "agent": None, "content": "t", "id": "d", "title": "t",
        "max_rounds": 5, "quorum": "2/3",
    }]
    assert "workspace" not in replay.replay(events)


def test_workspace_ready_is_audit_only():
    assert "workspace_ready" in replay.AUDIT_ONLY
```

Add to `tests/test_cli.py` (match its existing invocation helper — it drives `cli.main([...])` with `tmp_path` as cwd):

```python
def test_new_with_repo_pins_workspace(tmp_path, monkeypatch, capsys):
    # at module top: from tests.test_workspace import make_repo
    repo = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["new", "problem text", "--repo", str(repo)])
    did = capsys.readouterr().out.strip().splitlines()[-1]
    state = json.loads((tmp_path / "debates" / did / "state.json").read_text())
    assert state["workspace"]["source"] == str(repo.resolve())
    assert len(state["workspace"]["commit"]) == 40


def test_new_with_bad_repo_exits_with_error(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        cli.main(["new", "problem text", "--repo", str(tmp_path / "nope")])
```

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workspace.py tests/test_store.py tests/test_replay.py tests/test_cli.py -q`
Expected: FAIL — `ModuleNotFoundError: debatelab.workspace`, `TypeError: create() got an unexpected keyword argument`.

- [ ] **Step 3: Implement**

Create `debatelab/workspace.py`:

```python
"""Disposable git-worktree workspaces for repo-grounded debates.

The pin ({source, commit}) is recorded at `debate new`; the checkout is
materialized at `debate run` and removed when a human decision lands.
See specs/2026-07-15-workspace-grounded-debates-design.md §1-2.
"""
import subprocess
from pathlib import Path

GIT_TIMEOUT = 60  # metadata/worktree commands, not agent work


class WorkspaceError(Exception):
    """The workspace could not be pinned or materialized."""


def _git(args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True,
            timeout=GIT_TIMEOUT, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise WorkspaceError("git is not on PATH")
    except subprocess.TimeoutExpired:
        raise WorkspaceError(f"git {' '.join(args)}: timed out")
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)}: {proc.stderr.strip()[:300]}"
        )
    return proc.stdout.strip()


def pin(source: str) -> dict:
    """Resolve a repo path to the {source, commit} record `debate new`
    stores. The commit is HEAD now: citations must survive later pushes."""
    src = Path(source).expanduser().resolve()
    if not src.is_dir():
        raise WorkspaceError(f"not a directory: {src}")
    commit = _git(["-C", str(src), "rev-parse", "HEAD"])
    return {"source": str(src), "commit": commit}
```

`store.py` — `create` signature becomes `def create(self, title, problem, context_texts=(), workspace=None) -> str:`. In the genesis `append_event` dict and the `write_state` dict, add the key conditionally (identical one-liner in both):

```python
            **({"workspace": workspace} if workspace else {}),
```

`replay.py` — add to `_debate_created`:

```python
    if "workspace" in e:
        st["workspace"] = e["workspace"]
```

and add `"workspace_ready",` to `AUDIT_ONLY` (emitted by Task 3; folding rule and emitter must land together per Global Constraints — the exemption landing one task early is harmless, an emitter without it is not). Do **not** touch `_initial()`.

`cli.py` — in `cmd_new`:

```python
def cmd_new(args):
    store = get_store()
    workspace = None
    if args.repo:
        from . import workspace as workspace_mod
        try:
            workspace = workspace_mod.pin(args.repo)
        except workspace_mod.WorkspaceError as e:
            sys.exit(f"--repo: {e}")
    contexts = []
    for f in args.context or []:
        p = Path(f)
        contexts.append((p.name, p.read_text()))
    title = args.problem.strip().splitlines()[0][:60]
    print(store.create(title, args.problem, contexts, workspace=workspace))
```

and in `main()` under the `new` subparser:

```python
    sp.add_argument(
        "--repo",
        help="ground the debate in this git repository (agents get a "
        "disposable checkout of its current HEAD)",
    )
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass — including the replay differential tests, which is the point of the genesis+fold landing together.

- [ ] **Step 5: Commit**

```bash
git add debatelab/workspace.py debatelab/store.py debatelab/replay.py debatelab/cli.py tests/
git commit -m "feat: pin a --repo workspace into the debate record"
```

---

### Task 3: Worktree lifecycle — materialize on run, remove on decision

**Files:**
- Modify: `debatelab/workspace.py` (materialize, remove)
- Modify: `debatelab/cli.py` (`cmd_run` materializes + halts; `cmd_decide` removes)
- Modify: `.gitignore` (workspace + live.json)
- Test: `tests/test_workspace.py`, `tests/test_cli.py`

**Interfaces:**
- Consumes: `workspace.pin`, `workspace._git`, `WorkspaceError` (Task 2).
- Produces: `workspace.materialize(workspace: dict, debate_dir: Path) -> tuple[Path, bool]` (path, created-now); `workspace.remove(workspace: dict, debate_dir: Path) -> str | None` (warning text or None); transcript event `type: "workspace_ready"` with `content: <commit sha>`; `cli._halt_workspace(store, debate_id, state, message)` (exits 3).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_workspace.py`:

```python
def test_materialize_creates_worktree_at_pinned_commit(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    # advance HEAD past the pin: the worktree must ignore the new commit
    (repo / "later.txt").write_text("later\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "later"],
        check=True,
    )
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    path, created = workspace.materialize(ws, debate_dir)
    assert created is True
    assert path == debate_dir / "workspace"
    assert (path / "hello.txt").exists()
    assert not (path / "later.txt").exists()


def test_materialize_reuses_existing_worktree(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    workspace.materialize(ws, debate_dir)
    (debate_dir / "workspace" / "scratch.txt").write_text("agent litter\n")
    path, created = workspace.materialize(ws, debate_dir)
    assert created is False
    assert (path / "scratch.txt").exists()


def test_materialize_missing_source_raises(tmp_path):
    ws = {"source": str(tmp_path / "gone"), "commit": "a" * 40}
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    with pytest.raises(workspace.WorkspaceError):
        workspace.materialize(ws, debate_dir)


def test_remove_deletes_worktree_and_registration(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    workspace.materialize(ws, debate_dir)
    assert workspace.remove(ws, debate_dir) is None
    assert not (debate_dir / "workspace").exists()
    listed = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "debate/workspace" not in listed


def test_remove_missing_workspace_is_noop(tmp_path):
    ws = {"source": str(tmp_path), "commit": "a" * 40}
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    assert workspace.remove(ws, debate_dir) is None


def test_remove_falls_back_when_source_is_gone(tmp_path):
    repo = make_repo(tmp_path)
    ws = workspace.pin(str(repo))
    debate_dir = tmp_path / "debate"
    debate_dir.mkdir()
    workspace.materialize(ws, debate_dir)
    shutil.rmtree(repo)
    warning = workspace.remove(ws, debate_dir)
    assert warning is not None and "prune" in warning
    assert not (debate_dir / "workspace").exists()
```

(add `import shutil` to the test module's imports.)

Add to `tests/test_cli.py` — run-path behavior, using scripted agents is unnecessary: assert on the halt path and on the decision cleanup, which need no live agents:

```python
def test_run_halts_when_workspace_source_is_gone(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["new", "problem text", "--repo", str(repo)])
    did = capsys.readouterr().out.strip().splitlines()[-1]
    shutil.rmtree(repo)
    with pytest.raises(SystemExit) as exc:
        cli.main(["run", did, "--config", write_two_agent_config(tmp_path)])
    assert exc.value.code == 3
    state = json.loads((tmp_path / "debates" / did / "state.json").read_text())
    assert state["status"] == "error"
    events = [
        json.loads(line)
        for line in (tmp_path / "debates" / did / "transcript.jsonl")
        .read_text().splitlines()
    ]
    assert events[-1]["type"] == "error"


def test_decision_removes_workspace(tmp_path, monkeypatch, capsys):
    """approve on a grounded debate tears the worktree down (spec §2)."""
    repo = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["new", "problem text", "--repo", str(repo)])
    did = capsys.readouterr().out.strip().splitlines()[-1]
    debate_dir = tmp_path / "debates" / did
    # put the debate into a decidable state without running agents
    state = json.loads((debate_dir / "state.json").read_text())
    state["status"] = "awaiting_human"
    (debate_dir / "state.json").write_text(json.dumps(state))
    ws = workspace.materialize(state["workspace"], debate_dir)[0]
    assert ws.exists()
    cli.main(["approve", did, "-m", "ok"])
    assert not ws.exists()
```

`write_two_agent_config(tmp_path)` — if `tests/test_cli.py` does not already have a config-writing helper, add one that writes an `agents.yaml` with two `echo`-command cli agents and returns its path (the run halts on the workspace before ever calling them).

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workspace.py tests/test_cli.py -q`
Expected: FAIL — `AttributeError: module 'debatelab.workspace' has no attribute 'materialize'`.

- [ ] **Step 3: Implement**

`workspace.py` — append (plus `import shutil` at top):

```python
def materialize(workspace: dict, debate_dir: Path) -> tuple[Path, bool]:
    """Ensure debates/<id>/workspace/ is a checkout of the pinned commit.

    Returns (path, created_now). An existing directory is reused as-is:
    agent scratch (caches, build output) is harmless in a disposable tree,
    and recreating mid-debate would discard nothing of record (spec §2)."""
    target = Path(debate_dir) / "workspace"
    if target.exists():
        return target, False
    source = workspace["source"]
    if not Path(source).is_dir():
        raise WorkspaceError(f"workspace source repo is gone: {source}")
    _git([
        "-C", source, "worktree", "add", "--detach",
        str(target), workspace["commit"],
    ])
    return target, True


def remove(workspace: dict, debate_dir: Path) -> str | None:
    """Tear the worktree down; best-effort, never an error (spec §2).

    Returns a warning string when only the local directory could be
    removed and the source repo keeps a stale registration."""
    target = Path(debate_dir) / "workspace"
    if not target.exists():
        return None
    try:
        _git([
            "-C", workspace["source"], "worktree", "remove", "--force",
            str(target),
        ])
        return None
    except WorkspaceError as e:
        shutil.rmtree(target, ignore_errors=True)
        return (
            f"workspace deleted, but source cleanup failed ({e}); "
            f"run `git worktree prune` in {workspace['source']}"
        )
```

`cli.py` — top-level import `from . import workspace as workspace_mod` (replace Task 2's local import in `cmd_new`). Add the halt helper:

```python
def _halt_workspace(store, debate_id, state, message):
    """Workspace failure is a halt, same contract as under-quorum: an
    error event (which replay folds to status error), the checkpoint,
    and exit 3 — mirroring cmd_run's status == "error" path."""
    state["status"] = "error"
    store.append_event(debate_id, {
        "round": state["round"], "phase": "run", "agent": None,
        "type": "error", "content": message,
    })
    store.write_state(debate_id, state)
    store.rebuild_index()
    print(f"workspace error: {message}", flush=True)
    print("final status: error")
    sys.exit(3)
```

In `cmd_run`, inside the lock, before the roster loop:

```python
            state = store.read_state(args.id)
            workspace = state.get("workspace")
            workdir = None
            if workspace:
                try:
                    workdir, created = workspace_mod.materialize(
                        workspace, store.path(args.id)
                    )
                except workspace_mod.WorkspaceError as e:
                    _halt_workspace(store, args.id, state, str(e))
                if created:
                    store.append_event(args.id, {
                        "round": state["round"], "phase": "run",
                        "agent": None, "type": "workspace_ready",
                        "content": workspace["commit"],
                    })
```

(`workdir` is unused until Task 4 wires it into `build_agents`; that is fine for one commit.)

In `cmd_decide`, after the `with store.debate_lock(...)` block succeeds (i.e., after `_decide_locked` returned without exiting), still inside the `try`:

```python
        state = store.read_state(args.id)
        workspace = state.get("workspace")
        if workspace:
            warning = workspace_mod.remove(workspace, store.path(args.id))
            if warning:
                print(warning, file=sys.stderr)
```

Place it inside the `with` suite, after the `_decide_locked(store, args, decision)` line — the lock must still be held while mutating the debate directory.

`.gitignore` — append:

```
debates/*/workspace/
debates/*/live.json
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add debatelab/workspace.py debatelab/cli.py .gitignore tests/
git commit -m "feat: materialize and tear down the debate worktree"
```

---

### Task 4: Agents run inside the workspace — cwd, workspace_args, banner

**Files:**
- Modify: `debatelab/agents/base.py` (class-level `workspace_attached`, `stall_after`)
- Modify: `debatelab/agents/registry.py` (spec fields, `build_agents(specs, workdir=None)`)
- Modify: `debatelab/agents/cli_agent.py` (`workdir`, `workspace_args`)
- Modify: `debatelab/cli.py` (`cmd_run` passes workdir, prints banner)
- Test: `tests/test_registry_timeouts.py` (rename-worthy? no — add a `tests/test_workspace_agents.py`), `tests/test_cli.py`

**Interfaces:**
- Consumes: `workdir` from Task 3's `cmd_run`; `_parse_task_seconds` from Task 1.
- Produces: `Agent.workspace_attached: bool = False` and `Agent.stall_after = {"fast": 300, "deep": 900}` class defaults (MockAgent inherits both); `AgentSpec.workspace_args: list | None`, `AgentSpec.stall_after: dict`; `CliAgent(..., workdir=None, workspace_args=None)`; `registry.build_agents(specs, workdir=None)` setting per-instance `workspace_attached`/`stall_after`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_workspace_agents.py`:

```python
"""CLI agents run inside the workspace with per-platform flags (spec §3)."""
import sys

from debatelab.agents import models, registry
from debatelab.agents.cli_agent import CliAgent


def test_cli_agent_runs_in_workdir(tmp_path):
    """The stub can only see marker.txt if cwd was the workdir — this is
    the wiring the whole feature hangs on (spec §8 integration)."""
    (tmp_path / "marker.txt").write_text("proof-of-cwd\n")
    agent = CliAgent(
        "x",
        [sys.executable, "-c",
         "print(open('marker.txt').read().strip())"],
        workdir=str(tmp_path),
    )
    assert agent.ask("ignored").text == "proof-of-cwd"


def test_workspace_args_appended_only_when_attached(tmp_path):
    attached = CliAgent(
        "x", ["echo", "{prompt}"], workdir=str(tmp_path),
        workspace_args=["--sandbox", "workspace-write"],
    )
    assert attached._build_command("hi", None) == [
        "echo", "hi", "--sandbox", "workspace-write",
    ]
    detached = CliAgent(
        "x", ["echo", "{prompt}"],
        workspace_args=["--sandbox", "workspace-write"],
    )
    assert detached._build_command("hi", None) == ["echo", "hi"]


def test_build_agents_marks_attachment(tmp_path):
    config = tmp_path / "agents.yaml"
    config.write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "    stall_after: {deep: 1200}\n"
    )
    specs = registry.load_agent_specs(config)
    attached = registry.build_agents(specs, workdir=str(tmp_path))[0]
    assert attached.workspace_attached is True
    assert attached.stall_after == {"fast": 300, "deep": 1200}
    detached = registry.build_agents(specs)[0]
    assert detached.workspace_attached is False


def test_workspace_args_must_be_a_list_of_strings(tmp_path):
    config = tmp_path / "agents.yaml"
    config.write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "    workspace_args: \"--oops\"\n"
    )
    try:
        registry.load_agent_specs(config)
    except registry.ConfigError:
        return
    raise AssertionError("expected ConfigError")
```

Add to `tests/test_cli.py`:

```python
def test_run_banner_names_attachment(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    cli.main(["new", "problem text", "--repo", str(repo)])
    did = capsys.readouterr().out.strip().splitlines()[-1]
    config = tmp_path / "agents.yaml"
    config.write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n"
        "    command: [\"echo\", \"{prompt}\"]\n"
        "    workspace_args: [\"--flag\"]\n"
        "  - name: b\n    backend: cli\n"
        "    command: [\"echo\", \"{prompt}\"]\n"
    )
    with pytest.raises(SystemExit):
        cli.main(["run", did, "--config", str(config), "--max-rounds", "1"])
    out = capsys.readouterr().out
    assert "agent 'a': workspace-attached (--flag)" in out
    assert "agent 'b': workspace-attached (no extra flags)" in out
```

(the run itself ends `no_consensus`/`error` with echo agents — the banner assertion is the target; `SystemExit` is expected either way.)

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_workspace_agents.py tests/test_cli.py -q`
Expected: FAIL — unexpected keyword `workdir`, missing attributes.

- [ ] **Step 3: Implement**

`base.py` — class attributes on `Agent` (below `__init__`'s class line, above it textually):

```python
class Agent(ABC):
    # Overridden per-instance by registry.build_agents; class-level defaults
    # keep every test double honest without boilerplate.
    workspace_attached: bool = False
    stall_after = {"fast": 300, "deep": 900}
```

`cli_agent.py` — constructor gains `workdir: str | None = None, workspace_args: list[str] | None = None`; store both. `subprocess.run` in `ask` gains `cwd=self.workdir`. `_build_command` ends with:

```python
        if self.workdir and self.workspace_args:
            cmd.extend(self.workspace_args)
        return cmd
```

`registry.py` — `AgentSpec` gains:

```python
    workspace_args: list | None = None
    stall_after: dict = field(
        default_factory=lambda: {"fast": 300, "deep": 900}
    )
```

In `load_agent_specs`, populate both (with validation for `workspace_args`):

```python
        workspace_args = entry.get("workspace_args")
        if workspace_args is not None and (
            not isinstance(workspace_args, list)
            or not all(isinstance(t, str) for t in workspace_args)
        ):
            raise ConfigError(
                f"{path}: agent '{name}': workspace_args must be a "
                f"list of strings"
            )
```

and in the `AgentSpec(...)` call:

```python
                workspace_args=workspace_args,
                stall_after=_parse_task_seconds(
                    entry.get("stall_after"), field_name="stall_after",
                    path=path, name=name,
                    default_fast=300, default_deep=900,
                ),
```

`build_agents` becomes:

```python
def build_agents(specs: list[AgentSpec], workdir: str | None = None) -> list[Agent]:
    agents = []
    for spec in specs:
        if not spec.enabled:
            continue
        problem = spec_problem(spec)
        if problem:
            raise ConfigError(f"agent '{spec.name}': {problem}")
        if resolve_backend(spec) == "cli":
            agent = CliAgent(
                spec.name, spec.command, spec.timeout, spec.models_command,
                workdir=workdir, workspace_args=spec.workspace_args,
            )
            agent.workspace_attached = workdir is not None
        else:
            agent = ApiAgent(
                spec.name, spec.provider, spec.model, spec.api_key_env,
                spec.base_url, spec.timeout,
            )
            agent.workspace_attached = False
        agent.stall_after = dict(spec.stall_after)
        agents.append(agent)
    return agents
```

`cli.py` `cmd_run` — replace `agents = registry.build_agents(ready)` with:

```python
            if workspace:
                for spec in ready:
                    if registry.resolve_backend(spec) == "cli":
                        extra = " ".join(spec.workspace_args or [])
                        print(
                            f"agent '{spec.name}': workspace-attached "
                            f"({extra or 'no extra flags'})",
                            flush=True,
                        )
                    else:
                        print(
                            f"agent '{spec.name}': no repo access "
                            f"(api backend)",
                            flush=True,
                        )
            agents = registry.build_agents(
                ready, workdir=str(workdir) if workdir else None
            )
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/ debatelab/cli.py tests/
git commit -m "feat: run cli agents inside the debate workspace"
```

---

### Task 5: Grounding preambles in every phase prompt

**Files:**
- Modify: `debatelab/prompts.py` (preamble builders)
- Modify: `debatelab/orchestrator.py` (per-agent problem dict)
- Test: `tests/test_prompts.py` (or create it), `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `state["workspace"]` (Task 2), `agent.workspace_attached` (Task 4).
- Produces: `prompts.workspace_preamble(commit: str) -> str`; `prompts.UNATTACHED_NOTICE: str`; `prompts.ground_problem(problem: str, attached: bool, commit: str) -> str`. Inside the orchestrator, the `problem` value passed to `_phase_*` changes from `str` to `dict[agent_name, str]` — every phase indexes `problem[name]`.

- [ ] **Step 1: Write the failing tests**

Add (to `tests/test_prompts.py` if it exists, else create with plain imports):

```python
from debatelab import prompts


def test_ground_problem_attached_cites_commit():
    out = prompts.ground_problem("The problem.", True, "abc123")
    assert "abc123" in out
    assert "file:line" in out
    assert out.endswith("The problem.")


def test_ground_problem_unattached_warns():
    out = prompts.ground_problem("The problem.", False, "abc123")
    assert "you cannot" in out
    assert "abc123" not in out
    assert out.endswith("The problem.")
```

Add to `tests/test_orchestrator.py` (uses `conftest.happy_agent` / `make_store`):

```python
def test_grounded_debate_prefaces_prompts_per_agent(tmp_path):
    store = make_store(tmp_path)
    ws = {"source": "/repo", "commit": "c0ffee"}
    did = store.create("t", "the problem", workspace=ws)
    a, b = happy_agent("a", nominee="b"), happy_agent("b", nominee="a")
    a.workspace_attached = True
    b.workspace_attached = False   # e.g. an api-backed voter
    orchestrator.Orchestrator(store, [a, b]).run(did)
    assert all("c0ffee" in p for p in a.prompts)
    assert all("you cannot" in p for p in b.prompts)


def test_plain_debate_prompts_are_unprefaced(tmp_path):
    store = make_store(tmp_path)
    did = store.create("t", "the problem")
    a, b = happy_agent("a", nominee="b"), happy_agent("b", nominee="a")
    orchestrator.Orchestrator(store, [a, b]).run(did)
    for agent in (a, b):
        assert all("you cannot" not in p for p in agent.prompts)
        assert all("working directory" not in p for p in agent.prompts)
```

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompts.py tests/test_orchestrator.py -q`
Expected: FAIL — `AttributeError: module 'debatelab.prompts' has no attribute 'ground_problem'`.

- [ ] **Step 3: Implement**

`prompts.py` — add near the top, after the existing constants:

```python
UNATTACHED_NOTICE = (
    "The other agents in this debate can read the repository under "
    "discussion and run its tests; you cannot. Weigh their file:line "
    "citations and test results as evidence. Do not assert facts about "
    "file contents you have not seen quoted.\n\n"
)


def workspace_preamble(commit: str) -> str:
    return (
        "The repository under discussion is checked out at your current "
        f"working directory (commit {commit}). Read the code and run "
        "verification commands (tests, linters) to check claims — yours "
        "and the other agents' — before asserting them. Cite evidence "
        "as file:line.\n\n"
    )


def ground_problem(problem: str, attached: bool, commit: str) -> str:
    """Preface the problem for one agent of a repo-grounded debate."""
    preface = workspace_preamble(commit) if attached else UNATTACHED_NOTICE
    return preface + problem
```

`orchestrator.py` — in `run()`, replace `problem = self.store.read_problem(debate_id)` with:

```python
        raw_problem = self.store.read_problem(debate_id)
        workspace = state.get("workspace")
        if workspace:
            problem = {
                name: prompts.ground_problem(
                    raw_problem,
                    self.agents[name].workspace_attached,
                    workspace["commit"],
                )
                for name in self.order
            }
        else:
            problem = {name: raw_problem for name in self.order}
```

Then make every phase index it — the complete set of edits:

- `_phase_propose`: `lambda name: prompts.propose_prompt(name, problem[name])`
- `_phase_critique` `prompt_for`: `prompts.critique_prompt(name, problem[name], others, reject_reasons or None)`
- `_phase_revise` `prompt_for`: `prompts.revise_prompt(name, problem[name], own, state["critiques"])`
- `_phase_nominate`: both the fanout lambda and the `_reask` call site: `prompts.nominate_prompt(name, problem[name], proposals, names)`
- `_phase_synthesize`: `prompts.synthesize_prompt(winner, problem[winner], ...)`
- `_phase_vote`: both the fanout lambda and the `_reask` call site: `prompts.vote_prompt(name, problem[name], winner, candidate_text)`

Update the `_phase_*` docstring-free signatures' param name only if you must; keeping `problem` (now a dict) is fine — the phases are internal.

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. `MockAgent` routes synthesis on `SYNTHESIS_HEADER`, which the preamble does not disturb (it prefixes the problem, not the header).

- [ ] **Step 5: Commit**

```bash
git add debatelab/prompts.py debatelab/orchestrator.py tests/
git commit -m "feat: ground every phase prompt in the workspace"
```

---

### Task 6: Live status — heartbeat, stall alerts, live.json

**Files:**
- Create: `debatelab/livestatus.py`
- Modify: `debatelab/store.py` (`write_live`, `delete_live`)
- Modify: `debatelab/orchestrator.py` (`live=` param, `_call_agent` helper, `set_phase`)
- Modify: `debatelab/cli.py` (`cmd_run` starts/stops it)
- Test: `tests/test_livestatus.py` (new)

**Interfaces:**
- Consumes: `agent.stall_after` (Task 4), `store._atomic_write` pattern.
- Produces: `livestatus.HEARTBEAT_INTERVAL = 60`; `LiveStatus(store, debate_id, progress, interval=HEARTBEAT_INTERVAL, clock=time.monotonic)` with methods `set_phase(round_, phase)`, `call_started(agent, task, stall_after)`, `call_finished(agent)`, `tick()`, `start()`, `stop()`; `DebateStore.write_live(debate_id, payload)`, `DebateStore.delete_live(debate_id)`; `Orchestrator(..., live=None)`; `live.json` schema `{"updated", "round", "phase", "calls": [{"agent", "task", "started", "elapsed_s", "stalled"}]}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_livestatus.py`:

```python
"""Heartbeats and stall alerts: visibility instead of cancellation (spec §6)."""
import json

from debatelab.livestatus import LiveStatus
from debatelab.store import DebateStore


class FakeClock:
    def __init__(self):
        self.t = 0.0
    def __call__(self):
        return self.t
    def advance(self, seconds):
        self.t += seconds


def make(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("t", "p")
    lines = []
    clock = FakeClock()
    live = LiveStatus(store, did, progress=lines.append, clock=clock)
    return store, did, lines, clock, live


def test_tick_writes_live_json_with_elapsed(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=900)
    clock.advance(240)
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["phase"] == "propose"
    [call] = payload["calls"]
    assert call["agent"] == "claude"
    assert call["elapsed_s"] == 240
    assert call["stalled"] is False
    assert any("claude" in line and "4m" in line for line in lines)


def test_stall_alert_fires_once_with_bell(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=900)
    clock.advance(1020)
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["calls"][0]["stalled"] is True
    bells = [line for line in lines if "\a" in line]
    assert len(bells) == 1
    assert "stall threshold" in bells[0]
    live.tick()
    assert len([line for line in lines if "\a" in line]) == 1  # no re-ring


def test_no_stall_when_threshold_is_none(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=None)
    clock.advance(10_000)
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["calls"][0]["stalled"] is False


def test_finished_call_leaves_live_json(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.set_phase(1, "propose")
    live.call_started("claude", "deep", stall_after=900)
    live.call_finished("claude")
    live.tick()
    payload = json.loads((store.path(did) / "live.json").read_text())
    assert payload["calls"] == []


def test_stop_deletes_live_json_and_joins_thread(tmp_path):
    store, did, lines, clock, live = make(tmp_path)
    live.start()
    live.set_phase(1, "propose")
    live.tick()
    assert (store.path(did) / "live.json").exists()
    live.stop()
    assert not (store.path(did) / "live.json").exists()
```

Add to `tests/test_orchestrator.py`:

```python
def test_orchestrator_reports_calls_to_live(tmp_path):
    class RecordingLive:
        def __init__(self):
            self.events = []
        def set_phase(self, rnd, phase):
            self.events.append(("phase", rnd, phase))
        def call_started(self, agent, task, stall_after):
            self.events.append(("start", agent, task, stall_after))
        def call_finished(self, agent):
            self.events.append(("end", agent))

    store = make_store(tmp_path)
    did = store.create("t", "p")
    live = RecordingLive()
    orchestrator.Orchestrator(
        store,
        [happy_agent("a", nominee="b"), happy_agent("b", nominee="a")],
        live=live,
    ).run(did)
    starts = [e for e in live.events if e[0] == "start"]
    ends = [e for e in live.events if e[0] == "end"]
    assert len(starts) == len(ends) > 0
    assert ("phase", 1, "propose") in live.events
    # stall thresholds flow from the agent (Agent class default: deep 900)
    assert ("start", "a", "deep", 900) in starts
```

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_livestatus.py tests/test_orchestrator.py -q`
Expected: FAIL — `ModuleNotFoundError: debatelab.livestatus`, unexpected keyword `live`.

- [ ] **Step 3: Implement**

Create `debatelab/livestatus.py`:

```python
"""Live run status: heartbeat lines, stall alerts, and live.json.

With no proactive cancellation (spec §3), visibility does the job
cancellation used to: a background thread ticks every HEARTBEAT_INTERVAL
seconds, prints one line per in-flight agent call, rings the terminal
bell once when a call crosses its stall threshold, and atomically
rewrites debates/<id>/live.json for the viewer. Nothing here cancels
anything, ever (spec §6)."""
import threading
import time
from datetime import datetime, timezone

HEARTBEAT_INTERVAL = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _minutes(seconds: float) -> str:
    return f"{int(seconds // 60)}m"


class LiveStatus:
    def __init__(self, store, debate_id, progress,
                 interval=HEARTBEAT_INTERVAL, clock=time.monotonic):
        self.store = store
        self.debate_id = debate_id
        self.progress = progress
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._calls = {}
        self._round = None
        self._phase = None
        self._stop = threading.Event()
        self._thread = None

    def set_phase(self, round_, phase):
        with self._lock:
            self._round, self._phase = round_, phase

    def call_started(self, agent, task, stall_after):
        with self._lock:
            self._calls[agent] = {
                "task": task,
                "started_iso": _now_iso(),
                "started": self.clock(),
                "stall_after": stall_after,
                "alerted": False,
            }

    def call_finished(self, agent):
        with self._lock:
            self._calls.pop(agent, None)

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None
        self.store.delete_live(self.debate_id)

    def _loop(self):
        while not self._stop.wait(self.interval):
            self.tick()

    def tick(self):
        with self._lock:
            now = self.clock()
            calls, lines = [], []
            for agent in sorted(self._calls):
                c = self._calls[agent]
                elapsed = now - c["started"]
                threshold = c["stall_after"]
                stalled = threshold is not None and elapsed >= threshold
                calls.append({
                    "agent": agent,
                    "task": c["task"],
                    "started": c["started_iso"],
                    "elapsed_s": int(elapsed),
                    "stalled": stalled,
                })
                if stalled and not c["alerted"]:
                    c["alerted"] = True
                    lines.append(
                        f"\a⚠ {agent} · {self._phase} · {_minutes(elapsed)}"
                        f" — exceeded stall threshold "
                        f"({_minutes(threshold)}); still waiting.\n"
                        "  Ctrl-C interrupts; `debate run` resumes from "
                        "the last completed phase."
                    )
                elif stalled:
                    lines.append(
                        f"⚠ {agent} · {self._phase} · {_minutes(elapsed)}"
                        f" — still waiting"
                    )
                else:
                    lines.append(
                        f"⏳ {agent} · {self._phase} · {_minutes(elapsed)}"
                    )
            payload = {
                "updated": _now_iso(),
                "round": self._round,
                "phase": self._phase,
                "calls": calls,
            }
        self.store.write_live(self.debate_id, payload)
        for line in lines:
            self.progress(line)
```

`store.py` — add beside `write_result`:

```python
    def write_live(self, debate_id, payload: dict) -> None:
        """Ephemeral in-flight run status for the viewer; never read on
        resume, deleted on run exit. The transcript stays sole truth."""
        _atomic_write(
            self.path(debate_id) / "live.json",
            json.dumps(payload, indent=2),
        )

    def delete_live(self, debate_id) -> None:
        (self.path(debate_id) / "live.json").unlink(missing_ok=True)
```

`orchestrator.py` — constructor gains `live=None`, stored as `self.live`. Add the shared call wrapper and route all three call sites through it:

```python
    def _call_agent(self, debate_id, state, phase, name, prompt, task):
        """One retried agent call, reported to live status while in flight."""
        if self.live:
            self.live.call_started(
                name, task, self.agents[name].stall_after.get(task)
            )
        try:
            return retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
        finally:
            if self.live:
                self.live.call_finished(name)
```

- `_reask`: body becomes `reply = self._call_agent(debate_id, state, phase, name, prompts.reask(prompt, required), task)` inside the existing `try/except AgentError`.
- `_ask_one`: same substitution.
- `_fanout.call`: `reply = self._call_agent(debate_id, state, phase, name, prompt_for(name), task)` (hoist the `prompt_for(name)` call; drop the local `prompt` variable).

In `run()`'s loop, right after `self.progress(f"round {rnd}/...")`:

```python
                if self.live:
                    self.live.set_phase(rnd, phase)
```

`cli.py` `cmd_run` — wrap the orchestrator run:

```python
            from .livestatus import LiveStatus
            live = LiveStatus(
                store, args.id, progress=lambda m: print(m, flush=True)
            )
            try:
                orch = Orchestrator(
                    store,
                    agents,
                    progress=lambda m: print(m, flush=True),
                    live=live,
                )
            except ValueError as e:
                sys.exit(str(e))
            live.start()
            try:
                status = orch.run(
                    args.id, max_rounds=args.max_rounds, quorum=args.quorum
                )
            finally:
                live.stop()
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass. Watch `tests/test_orchestrator.py` for assumptions about `_ask_one`/`_fanout` internals; the refactor keeps their signatures and event output identical.

- [ ] **Step 5: Commit**

```bash
git add debatelab/livestatus.py debatelab/store.py debatelab/orchestrator.py debatelab/cli.py tests/
git commit -m "feat: heartbeat live status with stall alerts and live.json"
```

---

### Task 7: Viewer — running/stalled badges, dead-run detection, tab title

**Files:**
- Modify: `debatelab/viewer/index.html` (render layer + page wiring + CSS)
- Test: `tests/test_viewer_render.py`

**Interfaces:**
- Consumes: `live.json` schema from Task 6; viewer's existing `esc`, `fetchJSONOptional`, `showDebate` poll loop (3s).
- Produces: pure functions in `<script id="render">`: `liveState(live, nowMs, staleAfterMs)` → `"none" | "stale" | "stalled" | "running"`; `renderLive(live, nowMs, staleAfterMs)` → HTML string. Page constant `LIVE_STALE_MS = 120000` (2 × 60s heartbeat, spec §6).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_viewer_render.py`:

```python
LIVE = (
    '{"updated": "2026-07-15T12:00:00+00:00", "round": 1,'
    ' "phase": "propose", "calls": [{"agent": "claude", "task": "deep",'
    ' "started": "2026-07-15T11:43:00+00:00", "elapsed_s": 1020,'
    ' "stalled": true}]}'
)
NOW_FRESH = 'Date.parse("2026-07-15T12:00:30+00:00")'
NOW_STALE = 'Date.parse("2026-07-15T12:05:00+00:00")'


@needs_node
def test_livestate_classifies():
    assert render_js(
        f"liveState(JSON.parse('{LIVE}'), {NOW_FRESH}, 120000)"
    ) == "stalled"
    assert render_js(
        f"liveState(JSON.parse('{LIVE}'), {NOW_STALE}, 120000)"
    ) == "stale"
    assert render_js("liveState(null, 0, 120000)") == "none"
    running = LIVE.replace('"stalled": true', '"stalled": false')
    assert render_js(
        f"liveState(JSON.parse('{running}'), {NOW_FRESH}, 120000)"
    ) == "running"


@needs_node
def test_renderlive_badges_stalled_call():
    out = render_js(f"renderLive(JSON.parse('{LIVE}'), {NOW_FRESH}, 120000)")
    assert "claude" in out
    assert "stalled 17m" in out
    assert 'class="badge stalled"' in out


@needs_node
def test_renderlive_reports_dead_run_distinctly():
    out = render_js(f"renderLive(JSON.parse('{LIVE}'), {NOW_STALE}, 120000)")
    assert "run process not responding" in out
    assert "stalled 17m" not in out


@needs_node
def test_renderlive_escapes_agent_names():
    hostile = LIVE.replace("claude", "<img src=x>")
    out = render_js(f"renderLive(JSON.parse('{hostile}'), {NOW_FRESH}, 120000)")
    assert "<img" not in out
    assert "&lt;img" in out


@needs_node
def test_renderlive_none_is_empty():
    assert render_js("renderLive(null, 0, 120000)") == ""
```

- [ ] **Step 2: Run the new tests, verify they fail**

Run: `.venv/bin/python -m pytest tests/test_viewer_render.py -q`
Expected: new tests FAIL — `liveState is not defined`.

- [ ] **Step 3: Implement**

Inside `<script id="render">` in `debatelab/viewer/index.html` (pure — no `document`/`window`/`fetch`, test 1 of that file enforces it):

```javascript
function liveState(live, nowMs, staleAfterMs) {
  if (!live) return "none";
  if (nowMs - Date.parse(live.updated) > staleAfterMs) return "stale";
  if ((live.calls || []).some(c => c.stalled)) return "stalled";
  return "running";
}

function liveMinutes(seconds) {
  return Math.floor(seconds / 60) + "m";
}

function renderLive(live, nowMs, staleAfterMs) {
  const state = liveState(live, nowMs, staleAfterMs);
  if (state === "none") return "";
  if (state === "stale") {
    return '<div class="live"><span class="badge stale">' +
      "run process not responding</span></div>";
  }
  const badges = (live.calls || []).map(c => {
    const cls = c.stalled ? "badge stalled" : "badge running";
    const verb = c.stalled ? "stalled" : "running";
    return `<span class="${cls}">${esc(c.agent)} · ` +
      `${verb} ${liveMinutes(c.elapsed_s)}</span>`;
  });
  const phase = esc(live.phase) + " · round " + esc(live.round);
  return `<div class="live">⏱ ${phase} ${badges.join(" ")}</div>`;
}
```

In the page script (outside the render block): a constant and two wiring changes in `showDebate`:

```javascript
const LIVE_STALE_MS = 120000; // 2 × the run's 60s heartbeat (spec §6)
```

- fetch: `const live = await fetchJSONOptional(`/${id}/live.json`);`
- inject `renderLive(live, Date.now(), LIVE_STALE_MS)` into the debate header container (next to where `state.title`/status renders);
- title: `document.title = (liveState(live, Date.now(), LIVE_STALE_MS) === "stalled" ? "⚠ " : "") + "AI Debate Lab";`

CSS (append to the existing `<style>`):

```css
.live { margin: .5em 0; }
.badge { border-radius: 9px; padding: 2px 8px; font-size: .85em; margin-right: 4px; }
.badge.running { background: #1e3a5f; color: #cbe1ff; }
.badge.stalled { background: #6b4a00; color: #ffe1a1; }
.badge.stale   { background: #5f1e1e; color: #ffc9c9; }
```

(match the existing palette scheme if it differs — check neighboring rules and keep contrast in both existing themes if the file has them.)

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: all pass, including `test_render_layer_is_pure` (the new render functions touch no page global).

- [ ] **Step 5: Manual check + commit**

Run: `.venv/bin/debate serve --port 8099` and open a debate; without a run in flight no badge should appear.

```bash
git add debatelab/viewer/index.html tests/test_viewer_render.py
git commit -m "feat: viewer badges for in-flight, stalled, and dead runs"
```

---

### Task 8: Docs, config comments, end-to-end verification

**Files:**
- Modify: `README.md`, `agents.yaml`
- No new tests (behavioral coverage landed in Tasks 1-7)

- [ ] **Step 1: README**

Update three places:

1. "Run a debate" — add after the `debate new` line:

```markdown
Add `--repo PATH` to ground the debate in a git repository: CLI-backed
agents run inside a disposable checkout of its current HEAD
(`debates/<id>/workspace/`, removed after approve/reject), where they can
read the code and run verification commands before answering. API-backed
agents still debate but cannot inspect the repo and are told so.
```

2. A new "Timeouts and stalls" subsection after the exit-code paragraph:

```markdown
Agent calls have **no timeout by default** — difficult problems may
legitimately need hours. Set per-agent ceilings in `agents.yaml`
(`timeout: 900` or `timeout: {fast: 180, deep: null}`) to opt in.
While a run is in flight it prints a heartbeat line per busy agent every
minute and rings the terminal bell once when a call exceeds its
`stall_after` threshold (default: 15m deep, 5m fast) — a heads-up, not a
cancellation. Ctrl-C interrupts safely: `debate run` resumes from the
last completed phase. The viewer shows the same in-flight state from
`debates/<id>/live.json` and reports a run process that stopped
heartbeating.
```

3. The storage table — add rows:

```markdown
| `workspace/` | Disposable git worktree of the debated repo (grounded debates only) |
| `live.json` | Ephemeral in-flight run status for the viewer (deleted on run exit) |
```

- [ ] **Step 2: agents.yaml comments**

Append commented examples to the header comment block:

```yaml
# Repo-grounded debates (debate new --repo): per-agent extras.
#   workspace_args: extra argv appended only when a workspace is attached —
#     put each platform's sandbox flags here, e.g. for codex:
#       workspace_args: ["--sandbox", "workspace-write"]
#   timeout: opt-in ceiling in seconds; absent = no limit (calls may run
#     for hours). Int or per-tier: {fast: 180, deep: null}
#   stall_after: soft alert threshold (never cancels); default
#     {fast: 300, deep: 900}
```

- [ ] **Step 3: End-to-end smoke test**

```bash
.venv/bin/python -m pytest -q                      # full suite green
cd "$(mktemp -d)" && git init -q demo && cd demo
echo "def f(): return 1" > lib.py && git add . && git -c user.email=t@t -c user.name=t commit -qm init
cd /home/bossbaby/Desktop/fix-me/ai-debate-lab
.venv/bin/debate new "Demo: is lib.py correct?" --repo <that demo path>
.venv/bin/debate run <printed id> --max-rounds 1   # watch banner + heartbeats
.venv/bin/debate approve <printed id> -m "smoke"   # workspace/ disappears
```

Expected: banner names each agent's attachment, agents cite `lib.py`, `debates/<id>/workspace/` exists during the run and is gone after approve, `debate fsck <id>` prints `ok`.

- [ ] **Step 4: Commit**

```bash
git add README.md agents.yaml
git commit -m "docs: document workspace grounding, opt-in timeouts, stall alerts"
```
