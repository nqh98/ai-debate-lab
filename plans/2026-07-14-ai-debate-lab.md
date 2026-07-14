# AI Debate Lab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Python CLI (`debate`) that runs structured multi-round debates between pluggable AI agents (CLI- or API-backed) until unanimous consensus, storing every artifact as git-friendly files, gated by explicit human approval, with a static web viewer.

**Architecture:** Single package `debatelab`. Agents implement a tiny `Agent.ask(prompt) -> str` interface with two adapters (subprocess CLI, raw HTTP API). A synchronous orchestrator fans out per-phase calls with `ThreadPoolExecutor`, appends every event to `transcript.jsonl` (source of truth), rewrites `state.json` after each phase (enables resume), and regenerates `summary.md`. Protocol per round: propose (round 1 only) → critique → revise → vote; unanimous accept = consensus → `awaiting_human`; `debate approve/reject` records the final human decision.

**Tech Stack:** Python ≥ 3.10, PyYAML, stdlib only otherwise (`subprocess`, `urllib.request`, `http.server`, `concurrent.futures`). pytest for tests. Viewer is one static HTML file, vanilla JS, no build step.

## Global Constraints

- Python ≥ 3.10; runtime dependencies: **PyYAML only**. No provider SDKs, no httpx/requests, no web framework.
- HTTP calls via stdlib `urllib.request`.
- Agent call timeout default **180 s**; failed calls retried **once**, then the agent **abstains** for that phase; a phase needs **≥ 2** responders or the debate halts with status `error`.
- Debate statuses: `created → running → awaiting_human | no_consensus → approved | rejected`, plus `error`.
- Default `max_rounds` = **5**.
- Consensus = unanimous `accept` among agents that actually voted.
- Candidate tie-break: lowest index in config order.
- API keys only via env vars named in `agents.yaml` (`api_key_env`); never stored in files.
- Transcript event schema: `{ts, round, phase, agent, type, content}` (extra keys allowed, e.g. `verdict`).
- Commit messages: conventional style (`feat:`, `test:`, `docs:`), **no attribution trailers of any kind**.
- All commands below run from the repo root: `/home/bossbaby/Desktop/fix-me/ai-debate-lab`.

---

### Task 1: Project scaffolding

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `debatelab/__init__.py`
- Create: `debatelab/agents/__init__.py`
- Create: `tests/__init__.py`
- Test: `tests/test_package.py`

**Interfaces:**
- Consumes: nothing
- Produces: installable package `debatelab` with `debatelab.__version__: str`, console script `debate` (entry point `debatelab.cli:main` — implemented in Task 9), venv at `.venv/`

- [ ] **Step 1: Create pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "debatelab"
version = "0.1.0"
description = "Multi-agent AI debate orchestrator with human-approved consensus"
requires-python = ">=3.10"
dependencies = ["PyYAML>=6.0"]

[project.optional-dependencies]
dev = ["pytest>=8"]

[project.scripts]
debate = "debatelab.cli:main"

[tool.setuptools.packages.find]
include = ["debatelab*"]

[tool.setuptools.package-data]
debatelab = ["viewer/*.html"]
```

- [ ] **Step 2: Create .gitignore**

```gitignore
.venv/
__pycache__/
*.egg-info/
.pytest_cache/
```

- [ ] **Step 3: Create package files**

`debatelab/__init__.py`:

```python
__version__ = "0.1.0"
```

`debatelab/agents/__init__.py` and `tests/__init__.py`: empty files.

- [ ] **Step 4: Write the smoke test**

`tests/test_package.py`:

```python
import debatelab


def test_version():
    assert debatelab.__version__ == "0.1.0"
```

- [ ] **Step 5: Create venv, install, run test**

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/test_package.py -v
```

Expected: `1 passed`. (The `debate` console script will fail to import until Task 9 — that's fine, don't invoke it yet.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml .gitignore debatelab/ tests/
git commit -m "feat: scaffold debatelab package"
```

---

### Task 2: Agent base + CliAgent

**Files:**
- Create: `debatelab/agents/base.py`
- Create: `debatelab/agents/cli_agent.py`
- Test: `tests/test_cli_agent.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `debatelab.agents.base.Agent` — ABC with `self.name: str` (set by `__init__(self, name)`) and abstract `ask(self, prompt: str) -> str`
  - `debatelab.agents.base.AgentError(Exception)` — raised on any agent call failure
  - `debatelab.agents.cli_agent.CliAgent(name: str, command: list[str], timeout: int = 180)` — substitutes `{prompt}` inside each command arg, returns stripped stdout

- [ ] **Step 1: Write the failing tests**

`tests/test_cli_agent.py`:

```python
import pytest

from debatelab.agents.base import Agent, AgentError
from debatelab.agents.cli_agent import CliAgent


def make_script(tmp_path, body):
    p = tmp_path / "stub.sh"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def test_cli_agent_is_an_agent(tmp_path):
    agent = CliAgent("stub", ["echo", "{prompt}"])
    assert isinstance(agent, Agent)
    assert agent.name == "stub"


def test_cli_agent_returns_stripped_stdout(tmp_path):
    script = make_script(tmp_path, 'echo "reply to: $1"')
    agent = CliAgent("stub", [script, "{prompt}"])
    assert agent.ask("hello") == "reply to: hello"


def test_cli_agent_substitutes_prompt_inside_arg(tmp_path):
    script = make_script(tmp_path, 'echo "$1"')
    agent = CliAgent("stub", [script, "prefix {prompt} suffix"])
    assert agent.ask("MID") == "prefix MID suffix"


def test_cli_agent_nonzero_exit_raises(tmp_path):
    script = make_script(tmp_path, 'echo "boom" >&2; exit 3')
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError, match="exit 3"):
        agent.ask("hello")


def test_cli_agent_timeout_raises(tmp_path):
    script = make_script(tmp_path, "sleep 5")
    agent = CliAgent("stub", [script, "{prompt}"], timeout=1)
    with pytest.raises(AgentError, match="timed out"):
        agent.ask("hello")


def test_cli_agent_missing_binary_raises():
    agent = CliAgent("stub", ["/no/such/binary", "{prompt}"])
    with pytest.raises(AgentError, match="not found"):
        agent.ask("hello")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.agents.base'`

- [ ] **Step 3: Implement base.py**

`debatelab/agents/base.py`:

```python
"""Minimal agent interface every backend adapter implements."""
from abc import ABC, abstractmethod


class AgentError(Exception):
    """An agent call failed (bad exit, timeout, HTTP error, missing key)."""


class Agent(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def ask(self, prompt: str) -> str:
        """Send a prompt, return the agent's text reply. Raises AgentError on failure."""
```

- [ ] **Step 4: Implement cli_agent.py**

`debatelab/agents/cli_agent.py`:

```python
"""Adapter for locally installed AI CLIs (claude, codex, gemini, ...)."""
import subprocess

from .base import Agent, AgentError


class CliAgent(Agent):
    def __init__(self, name: str, command: list[str], timeout: int = 180):
        super().__init__(name)
        self.command = command
        self.timeout = timeout

    def ask(self, prompt: str) -> str:
        cmd = [part.replace("{prompt}", prompt) for part in self.command]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            raise AgentError(f"{self.name}: timed out after {self.timeout}s")
        except FileNotFoundError:
            raise AgentError(f"{self.name}: command not found: {cmd[0]}")
        if proc.returncode != 0:
            raise AgentError(
                f"{self.name}: exit {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        return proc.stdout.strip()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli_agent.py -v`
Expected: `6 passed`

- [ ] **Step 6: Commit**

```bash
git add debatelab/agents/ tests/test_cli_agent.py
git commit -m "feat: agent interface and CLI subprocess adapter"
```

---

### Task 3: ApiAgent (openai / anthropic / google drivers)

**Files:**
- Create: `debatelab/agents/api_agent.py`
- Test: `tests/test_api_agent.py`

**Interfaces:**
- Consumes: `Agent`, `AgentError` from `debatelab.agents.base`
- Produces:
  - `debatelab.agents.api_agent.ApiAgent(name: str, provider: str, model: str, api_key_env: str, base_url: str | None = None, timeout: int = 180)`
  - `debatelab.agents.api_agent.DRIVERS: dict[str, tuple]` — keys `"openai"`, `"anthropic"`, `"google"` (registry validates `provider` against this)

- [ ] **Step 1: Write the failing tests**

`tests/test_api_agent.py`:

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from debatelab.agents.api_agent import ApiAgent, DRIVERS
from debatelab.agents.base import AgentError


class Recorder(BaseHTTPRequestHandler):
    calls = []
    payload = {}
    status = 200

    def do_POST(self):
        length = int(self.headers["Content-Length"])
        body = json.loads(self.rfile.read(length))
        Recorder.calls.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        data = json.dumps(Recorder.payload).encode()
        self.send_response(Recorder.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    Recorder.calls = []
    Recorder.status = 200
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_known_drivers():
    assert set(DRIVERS) == {"openai", "anthropic", "google"}


def test_openai_driver_roundtrip(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    Recorder.payload = {"choices": [{"message": {"content": " hi there "}}]}
    agent = ApiAgent("gpt", "openai", "gpt-5", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "hi there"
    call = Recorder.calls[0]
    assert call["path"] == "/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer sk-test"
    assert call["body"]["model"] == "gpt-5"
    assert call["body"]["messages"] == [{"role": "user", "content": "hello"}]


def test_anthropic_driver_roundtrip(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-ant")
    Recorder.payload = {"content": [{"type": "text", "text": "claude says hi"}]}
    agent = ApiAgent("cl", "anthropic", "claude-fable-5", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "claude says hi"
    call = Recorder.calls[0]
    assert call["path"] == "/v1/messages"
    assert call["headers"]["X-api-key"] == "sk-ant"
    assert call["body"]["max_tokens"] == 4096


def test_google_driver_roundtrip(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "g-key")
    Recorder.payload = {
        "candidates": [{"content": {"parts": [{"text": "gemini says hi"}]}}]
    }
    agent = ApiAgent("gm", "google", "gemini-pro", "TEST_KEY", base_url=server)
    assert agent.ask("hello") == "gemini says hi"
    call = Recorder.calls[0]
    assert call["path"] == "/v1beta/models/gemini-pro:generateContent"
    assert call["headers"]["X-goog-api-key"] == "g-key"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    agent = ApiAgent("x", "openai", "gpt-5", "NOPE_KEY")
    with pytest.raises(AgentError, match="NOPE_KEY is not set"):
        agent.ask("hello")


def test_http_error_raises(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "k")
    Recorder.status = 500
    Recorder.payload = {"error": "boom"}
    agent = ApiAgent("x", "openai", "gpt-5", "TEST_KEY", base_url=server)
    with pytest.raises(AgentError, match="HTTP 500"):
        agent.ask("hello")


def test_unknown_provider_rejected():
    with pytest.raises(ValueError, match="unknown provider"):
        ApiAgent("x", "mystery", "m", "KEY")
```

Note: `BaseHTTPRequestHandler` headers are case-insensitive but `dict(self.headers)` preserves the sent casing title-cased — hence `X-api-key` / `X-goog-api-key` in assertions. If those two assertions fail on casing, match them to whatever `dict(self.headers)` produced; the semantic check is that the key arrives in the right header.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_api_agent.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.agents.api_agent'`

- [ ] **Step 3: Implement api_agent.py**

`debatelab/agents/api_agent.py`:

```python
"""Adapter for HTTP LLM APIs. Three thin drivers, no provider SDKs."""
import json
import os
import urllib.error
import urllib.request

from .base import Agent, AgentError


def _openai_request(model, base_url, api_key, prompt):
    url = f"{(base_url or 'https://api.openai.com/v1').rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    body = {"model": model, "messages": [{"role": "user", "content": prompt}]}
    return url, headers, body


def _openai_parse(data):
    return data["choices"][0]["message"]["content"]


def _anthropic_request(model, base_url, api_key, prompt):
    url = f"{(base_url or 'https://api.anthropic.com').rstrip('/')}/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
    }
    body = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    return url, headers, body


def _anthropic_parse(data):
    return "".join(b["text"] for b in data["content"] if b["type"] == "text")


def _google_request(model, base_url, api_key, prompt):
    root = (base_url or "https://generativelanguage.googleapis.com").rstrip("/")
    url = f"{root}/v1beta/models/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    return url, headers, body


def _google_parse(data):
    return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])


DRIVERS = {
    "openai": (_openai_request, _openai_parse),
    "anthropic": (_anthropic_request, _anthropic_parse),
    "google": (_google_request, _google_parse),
}


class ApiAgent(Agent):
    def __init__(self, name, provider, model, api_key_env, base_url=None, timeout=180):
        super().__init__(name)
        if provider not in DRIVERS:
            raise ValueError(f"unknown provider: {provider}")
        self.provider = provider
        self.model = model
        self.api_key_env = api_key_env
        self.base_url = base_url
        self.timeout = timeout

    def ask(self, prompt: str) -> str:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise AgentError(f"{self.name}: env var {self.api_key_env} is not set")
        build, parse = DRIVERS[self.provider]
        url, headers, body = build(self.model, self.base_url, api_key, prompt)
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise AgentError(f"{self.name}: HTTP {e.code}: {e.read().decode()[:500]}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise AgentError(f"{self.name}: request failed: {e}")
        try:
            return parse(data).strip()
        except (KeyError, IndexError, TypeError) as e:
            raise AgentError(f"{self.name}: unexpected response shape: {e!r}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_api_agent.py -v`
Expected: `8 passed`

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/api_agent.py tests/test_api_agent.py
git commit -m "feat: HTTP API adapter with openai/anthropic/google drivers"
```

---

### Task 4: Registry (agents.yaml loading, validation, build)

**Files:**
- Create: `debatelab/agents/registry.py`
- Test: `tests/test_registry.py`

**Interfaces:**
- Consumes: `CliAgent`, `ApiAgent`, `DRIVERS`, `Agent`
- Produces:
  - `debatelab.agents.registry.ConfigError(Exception)`
  - `debatelab.agents.registry.AgentSpec` — dataclass: `name, backend, enabled=True, command=None, provider=None, model=None, api_key_env=None, base_url=None, timeout=180`
  - `load_agent_specs(path) -> list[AgentSpec]` — raises `ConfigError` on structural problems
  - `spec_problem(spec: AgentSpec) -> str | None` — actionable reason the agent can't run right now (missing binary/env var/etc.), `None` if usable
  - `build_agents(specs: list[AgentSpec]) -> list[Agent]` — enabled specs only; raises `ConfigError` naming the agent if an enabled spec has a problem

- [ ] **Step 1: Write the failing tests**

`tests/test_registry.py`:

```python
import pytest

from debatelab.agents.cli_agent import CliAgent
from debatelab.agents.api_agent import ApiAgent
from debatelab.agents.registry import (
    AgentSpec,
    ConfigError,
    build_agents,
    load_agent_specs,
    spec_problem,
)

GOOD_YAML = """\
agents:
  - name: alpha
    backend: cli
    command: ["echo", "{prompt}"]
  - name: beta
    backend: api
    provider: openai
    model: gpt-5
    api_key_env: BETA_KEY
    base_url: https://api.x.ai/v1
    enabled: false
"""


def write(tmp_path, text):
    p = tmp_path / "agents.yaml"
    p.write_text(text)
    return p


def test_load_good_config(tmp_path):
    specs = load_agent_specs(write(tmp_path, GOOD_YAML))
    assert [s.name for s in specs] == ["alpha", "beta"]
    assert specs[0].backend == "cli" and specs[0].enabled is True
    assert specs[1].enabled is False and specs[1].base_url == "https://api.x.ai/v1"
    assert specs[0].timeout == 180


def test_load_rejects_missing_agents_key(tmp_path):
    with pytest.raises(ConfigError, match="agents"):
        load_agent_specs(write(tmp_path, "foo: bar\n"))


def test_load_rejects_bad_backend(tmp_path):
    with pytest.raises(ConfigError, match="backend"):
        load_agent_specs(
            write(tmp_path, "agents:\n  - name: x\n    backend: quantum\n")
        )


def test_load_rejects_duplicate_names(tmp_path):
    text = (
        "agents:\n"
        "  - name: x\n    backend: cli\n    command: [echo]\n"
        "  - name: x\n    backend: cli\n    command: [echo]\n"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_agent_specs(write(tmp_path, text))


def test_spec_problem_missing_binary():
    spec = AgentSpec(name="x", backend="cli", command=["/no/such/bin"])
    assert "not found" in spec_problem(spec)


def test_spec_problem_missing_env_var(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    spec = AgentSpec(
        name="x", backend="api", provider="openai", model="m", api_key_env="NOPE_KEY"
    )
    assert "NOPE_KEY" in spec_problem(spec)


def test_spec_problem_unknown_provider():
    spec = AgentSpec(
        name="x", backend="api", provider="mystery", model="m", api_key_env="K"
    )
    assert "unknown provider" in spec_problem(spec)


def test_spec_problem_none_when_usable(monkeypatch):
    monkeypatch.setenv("OK_KEY", "k")
    cli = AgentSpec(name="a", backend="cli", command=["echo", "{prompt}"])
    api = AgentSpec(
        name="b", backend="api", provider="openai", model="m", api_key_env="OK_KEY"
    )
    assert spec_problem(cli) is None
    assert spec_problem(api) is None


def test_build_agents_skips_disabled_and_builds_types(monkeypatch):
    monkeypatch.setenv("OK_KEY", "k")
    specs = [
        AgentSpec(name="a", backend="cli", command=["echo", "{prompt}"]),
        AgentSpec(
            name="b", backend="api", provider="openai", model="m", api_key_env="OK_KEY"
        ),
        AgentSpec(name="c", backend="cli", enabled=False, command=["echo"]),
    ]
    agents = build_agents(specs)
    assert [a.name for a in agents] == ["a", "b"]
    assert isinstance(agents[0], CliAgent)
    assert isinstance(agents[1], ApiAgent)


def test_build_agents_raises_on_broken_enabled_spec(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    specs = [
        AgentSpec(
            name="bad", backend="api", provider="openai", model="m",
            api_key_env="NOPE_KEY",
        )
    ]
    with pytest.raises(ConfigError, match="bad"):
        build_agents(specs)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.agents.registry'`

- [ ] **Step 3: Implement registry.py**

`debatelab/agents/registry.py`:

```python
"""Loads agents.yaml into specs and builds enabled Agent instances."""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .api_agent import ApiAgent, DRIVERS
from .base import Agent
from .cli_agent import CliAgent


class ConfigError(Exception):
    pass


@dataclass
class AgentSpec:
    name: str
    backend: str
    enabled: bool = True
    command: list | None = None
    provider: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: int = 180


def load_agent_specs(path) -> list[AgentSpec]:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("agents"), list):
        raise ConfigError(f"{path}: expected a top-level 'agents' list")
    specs, seen = [], set()
    for i, entry in enumerate(raw["agents"]):
        if not isinstance(entry, dict) or "name" not in entry:
            raise ConfigError(f"{path}: agents[{i}] needs a 'name'")
        name = entry["name"]
        if name in seen:
            raise ConfigError(f"{path}: duplicate agent name '{name}'")
        seen.add(name)
        backend = entry.get("backend")
        if backend not in ("cli", "api"):
            raise ConfigError(
                f"{path}: agent '{name}': backend must be 'cli' or 'api'"
            )
        specs.append(
            AgentSpec(
                name=name,
                backend=backend,
                enabled=bool(entry.get("enabled", True)),
                command=entry.get("command"),
                provider=entry.get("provider"),
                model=entry.get("model"),
                api_key_env=entry.get("api_key_env"),
                base_url=entry.get("base_url"),
                timeout=int(entry.get("timeout", 180)),
            )
        )
    return specs


def spec_problem(spec: AgentSpec) -> str | None:
    """Actionable reason this agent can't run right now, or None if usable."""
    if spec.backend == "cli":
        if not spec.command:
            return "cli agent needs a 'command' list"
        if shutil.which(spec.command[0]) is None:
            return f"command not found on PATH: {spec.command[0]}"
    else:
        if spec.provider not in DRIVERS:
            return (
                f"unknown provider '{spec.provider}' "
                f"(known: {', '.join(sorted(DRIVERS))})"
            )
        if not spec.model:
            return "api agent needs a 'model'"
        if not spec.api_key_env:
            return "api agent needs 'api_key_env'"
        if not os.environ.get(spec.api_key_env):
            return f"env var {spec.api_key_env} is not set"
    return None


def build_agents(specs: list[AgentSpec]) -> list[Agent]:
    agents = []
    for spec in specs:
        if not spec.enabled:
            continue
        problem = spec_problem(spec)
        if problem:
            raise ConfigError(f"agent '{spec.name}': {problem}")
        if spec.backend == "cli":
            agents.append(CliAgent(spec.name, spec.command, spec.timeout))
        else:
            agents.append(
                ApiAgent(
                    spec.name,
                    spec.provider,
                    spec.model,
                    spec.api_key_env,
                    spec.base_url,
                    spec.timeout,
                )
            )
    return agents
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_registry.py -v`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/registry.py tests/test_registry.py
git commit -m "feat: agent registry with yaml config, validation, readiness checks"
```

---

### Task 5: Store (debate folders, transcript, state, index, summary rendering)

**Files:**
- Create: `debatelab/store.py`
- Test: `tests/test_store.py`

**Interfaces:**
- Consumes: nothing (stdlib only)
- Produces:
  - `debatelab.store.slugify(title: str, max_len: int = 40) -> str`
  - `debatelab.store.DebateStore(root: Path)` with methods:
    - `path(debate_id) -> Path`
    - `create(title: str, problem: str, context_texts: list[tuple[str, str]] = ()) -> str` — returns new debate id `YYYYMMDD-slug` (suffix `-2`, `-3`… on collision); writes `problem.md`, initial `state.json`, empty `transcript.jsonl`; rebuilds index
    - `append_event(debate_id, event: dict)` — prepends `ts` (UTC ISO), appends JSONL line
    - `read_events(debate_id) -> list[dict]`
    - `write_state(debate_id, state: dict)` / `read_state(debate_id) -> dict` (atomic write via tmp+replace)
    - `read_problem(debate_id) -> str`
    - `write_summary(debate_id, markdown: str)` / `read_summary(debate_id) -> str` (empty string if missing)
    - `list_ids() -> list[str]`
    - `rebuild_index()` — writes `<root>/index.json`: list of `{id, title, status, round}`
  - `debatelab.store.render_summary(state: dict) -> str` — Markdown for `summary.md`
  - Initial state dict shape (all later tasks rely on these exact keys):

```python
{
    "id": debate_id, "title": title, "status": "created",
    "round": 0, "max_rounds": 5, "last_completed_phase": None,
    "proposals": {}, "critiques": {}, "candidate": None,
    "votes": {}, "abstained": [], "human_decision": None,
}
```

  - `candidate` when set: `{"agent": name, "text": proposal_text}`; `votes` entries: `{"vote": "accept"|"reject", "reason": str}`; `human_decision` when set: `{"decision": "approved"|"rejected", "note": str}`

- [ ] **Step 1: Write the failing tests**

`tests/test_store.py`:

```python
import json

from debatelab.store import DebateStore, render_summary, slugify


def test_slugify():
    assert slugify("Should we use Rust?!") == "should-we-use-rust"
    assert slugify("   ") == "debate"
    assert len(slugify("x" * 100)) <= 40


def test_create_makes_files_and_id(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("Pick a DB", "Which database should we use?")
    d = store.path(did)
    assert did.endswith("-pick-a-db")
    assert (d / "problem.md").read_text().startswith("# Pick a DB")
    assert (d / "transcript.jsonl").exists()
    state = store.read_state(did)
    assert state["status"] == "created"
    assert state["round"] == 0 and state["max_rounds"] == 5
    assert state["human_decision"] is None


def test_create_includes_context(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem", [("notes.md", "some context")])
    text = store.read_problem(did)
    assert "## Context: notes.md" in text
    assert "some context" in text


def test_create_collision_gets_suffix(tmp_path):
    store = DebateStore(tmp_path / "debates")
    a = store.create("Same title", "p")
    b = store.create("Same title", "p")
    assert a != b and b.endswith("-2")


def test_events_roundtrip_with_ts(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "p")
    store.append_event(did, {"round": 1, "phase": "propose", "agent": "a",
                             "type": "proposal", "content": "hello"})
    events = store.read_events(did)
    assert len(events) == 1
    assert events[0]["content"] == "hello"
    assert "ts" in events[0]


def test_state_roundtrip(tmp_path):
    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "p")
    state = store.read_state(did)
    state["status"] = "running"
    store.write_state(did, state)
    assert store.read_state(did)["status"] == "running"


def test_index_lists_debates(tmp_path):
    store = DebateStore(tmp_path / "debates")
    a = store.create("First", "p")
    b = store.create("Second", "p")
    index = json.loads((tmp_path / "debates" / "index.json").read_text())
    assert {e["id"] for e in index} == {a, b}
    assert all(e["status"] == "created" for e in index)


def test_render_summary_pending_and_decided():
    state = {
        "id": "x", "title": "T", "status": "awaiting_human", "round": 2,
        "max_rounds": 5, "last_completed_phase": "vote",
        "proposals": {"a": "prop A"}, "critiques": {"b": "crit B"},
        "candidate": {"agent": "a", "text": "final answer"},
        "votes": {"a": {"vote": "accept", "reason": "r"},
                  "b": {"vote": "accept", "reason": "r"}},
        "abstained": ["c"], "human_decision": None,
    }
    md = render_summary(state)
    assert "pending human decision" in md
    assert "final answer" in md
    assert "| c | abstained |" in md

    state["human_decision"] = {"decision": "approved", "note": "ship it"}
    state["status"] = "approved"
    md = render_summary(state)
    assert "APPROVED" in md
    assert "ship it" in md
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.store'`

- [ ] **Step 3: Implement store.py**

`debatelab/store.py`:

```python
"""File-backed debate storage: transcript.jsonl is the source of truth,
state.json is the derived checkpoint, summary.md the human-readable view."""
import json
import re
from datetime import datetime, timezone
from pathlib import Path


def slugify(title: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:max_len].rstrip("-")
    return slug or "debate"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DebateStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, debate_id: str) -> Path:
        return self.root / debate_id

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
        self.write_state(debate_id, {
            "id": debate_id, "title": title, "status": "created",
            "round": 0, "max_rounds": 5, "last_completed_phase": None,
            "proposals": {}, "critiques": {}, "candidate": None,
            "votes": {}, "abstained": [], "human_decision": None,
        })
        self.rebuild_index()
        return debate_id

    def append_event(self, debate_id, event: dict):
        event = {"ts": _now(), **event}
        with (self.path(debate_id) / "transcript.jsonl").open("a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")

    def read_events(self, debate_id) -> list:
        text = (self.path(debate_id) / "transcript.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines() if line.strip()]

    def write_state(self, debate_id, state: dict):
        p = self.path(debate_id) / "state.json"
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        tmp.replace(p)

    def read_state(self, debate_id) -> dict:
        return json.loads((self.path(debate_id) / "state.json").read_text())

    def read_problem(self, debate_id) -> str:
        return (self.path(debate_id) / "problem.md").read_text()

    def write_summary(self, debate_id, markdown: str):
        (self.path(debate_id) / "summary.md").write_text(markdown)

    def read_summary(self, debate_id) -> str:
        p = self.path(debate_id) / "summary.md"
        return p.read_text() if p.exists() else ""

    def list_ids(self) -> list:
        if not self.root.exists():
            return []
        return sorted(
            p.name for p in self.root.iterdir() if (p / "state.json").exists()
        )

    def rebuild_index(self):
        entries = []
        for did in self.list_ids():
            s = self.read_state(did)
            entries.append({"id": did, "title": s["title"],
                            "status": s["status"], "round": s["round"]})
        self.root.mkdir(exist_ok=True)
        (self.root / "index.json").write_text(json.dumps(entries, indent=2))


def render_summary(state: dict) -> str:
    lines = [
        f"# Debate: {state['title']}",
        "",
        f"- **Status:** {state['status']}",
        f"- **Round:** {state['round']} / {state['max_rounds']}",
    ]
    decision = state.get("human_decision")
    candidate = state.get("candidate")
    if decision:
        lines += ["", f"## Final decision — {decision['decision'].upper()}", ""]
        if candidate:
            lines += [f"Candidate from **{candidate['agent']}**:", "",
                      candidate["text"], ""]
        if decision.get("note"):
            lines += [f"> Human note: {decision['note']}", ""]
    elif candidate:
        lines += [
            "",
            f"## Current candidate (from {candidate['agent']}) — "
            "pending human decision",
            "",
            candidate["text"],
            "",
        ]
    if state.get("votes") or state.get("abstained"):
        lines += ["", "## Latest votes", "", "| Agent | Vote |", "|---|---|"]
        for agent, v in state.get("votes", {}).items():
            lines.append(f"| {agent} | {v['vote']} |")
        for agent in state.get("abstained", []):
            lines.append(f"| {agent} | abstained |")
    if state.get("proposals"):
        lines += ["", "## Current proposals", ""]
        for agent, text in state["proposals"].items():
            lines += [f"### {agent}", "", text, ""]
    if state.get("critiques"):
        lines += ["## Latest critiques", ""]
        for agent, text in state["critiques"].items():
            lines += [f"### {agent}", "", text, ""]
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_store.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add debatelab/store.py tests/test_store.py
git commit -m "feat: file-backed debate store with transcript, state, index, summary"
```

---

### Task 6: Prompts and response parsers

**Files:**
- Create: `debatelab/prompts.py`
- Test: `tests/test_prompts.py`

**Interfaces:**
- Consumes: nothing
- Produces (all in `debatelab.prompts`):
  - `format_blocks(items: dict[str, str]) -> str` — `### name\ntext` blocks joined by blank lines
  - `propose_prompt(name: str, problem: str) -> str`
  - `critique_prompt(name: str, problem: str, other_proposals: dict[str, str], reject_reasons: dict[str, str] | None = None) -> str`
  - `revise_prompt(name: str, problem: str, own_proposal: str, critiques: dict[str, str]) -> str`
  - `nominate_prompt(name: str, problem: str, proposals: dict[str, str], names: list[str]) -> str` — instructs `NOMINATE: <agent-name>` reply format
  - `vote_prompt(name: str, problem: str, candidate_agent: str, candidate_text: str) -> str` — instructs `VOTE: accept` / `VOTE: reject`
  - `parse_nomination(text: str, valid_names: list[str]) -> str | None` — `NOMINATE:` line first, else first valid name mentioned anywhere, else `None`
  - `parse_vote(text: str) -> tuple[str, str]` — returns `("accept"|"reject", full_text)`; unparseable defaults to `reject` unless the first non-empty line contains "accept"

- [ ] **Step 1: Write the failing tests**

`tests/test_prompts.py`:

```python
from debatelab import prompts


def test_propose_prompt_contains_problem_and_name():
    p = prompts.propose_prompt("alpha", "What color?")
    assert "alpha" in p and "What color?" in p


def test_critique_prompt_excludes_nothing_but_shows_others():
    p = prompts.critique_prompt("alpha", "Q", {"beta": "B's idea"})
    assert "B's idea" in p and "### beta" in p


def test_critique_prompt_includes_reject_reasons_when_given():
    p = prompts.critique_prompt("alpha", "Q", {"beta": "B"}, {"gamma": "too vague"})
    assert "too vague" in p
    p2 = prompts.critique_prompt("alpha", "Q", {"beta": "B"})
    assert "Rejection reasons" not in p2


def test_revise_prompt_contains_own_and_critiques():
    p = prompts.revise_prompt("alpha", "Q", "my old take", {"beta": "weak point X"})
    assert "my old take" in p and "weak point X" in p and "Changes:" in p


def test_nominate_prompt_lists_names_and_format():
    p = prompts.nominate_prompt("alpha", "Q", {"alpha": "A", "beta": "B"},
                                ["alpha", "beta"])
    assert "NOMINATE:" in p and "alpha, beta" in p


def test_vote_prompt_contains_candidate():
    p = prompts.vote_prompt("alpha", "Q", "beta", "the answer")
    assert "VOTE:" in p and "the answer" in p and "beta" in p


def test_parse_nomination_formats():
    names = ["alpha", "beta"]
    assert prompts.parse_nomination("NOMINATE: beta\nbecause...", names) == "beta"
    assert prompts.parse_nomination("nominate:   alpha", names) == "alpha"
    assert prompts.parse_nomination("I think beta's plan wins", names) == "beta"
    assert prompts.parse_nomination("no idea", names) is None
    assert prompts.parse_nomination("NOMINATE: gamma", names) is None


def test_parse_vote_formats():
    assert prompts.parse_vote("VOTE: accept\nlooks good")[0] == "accept"
    assert prompts.parse_vote("vote: REJECT\nmissing X")[0] == "reject"
    assert prompts.parse_vote("I accept this fine answer")[0] == "accept"
    assert prompts.parse_vote("hmm not sure about this")[0] == "reject"
    verdict, reason = prompts.parse_vote("VOTE: reject\nbad idea")
    assert reason == "VOTE: reject\nbad idea"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: FAIL — `ImportError: cannot import name 'prompts'` (module missing)

- [ ] **Step 3: Implement prompts.py**

`debatelab/prompts.py`:

```python
"""Prompt templates for each debate phase, plus reply parsers."""
import re


def format_blocks(items: dict) -> str:
    return "\n\n".join(f"### {name}\n{text}" for name, text in items.items())


def propose_prompt(name: str, problem: str) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        "Give your best complete answer to the problem. "
        "Be concrete and justify key choices."
    )


def critique_prompt(name, problem, other_proposals, reject_reasons=None) -> str:
    extra = ""
    if reject_reasons:
        extra = (
            "\n\nRejection reasons from the last vote:\n"
            + format_blocks(reject_reasons)
        )
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        "Current proposals from the other agents:\n"
        f"{format_blocks(other_proposals)}\n\n"
        "Critique each proposal: where you agree, flaws, and missing "
        f"considerations.{extra}"
    )


def revise_prompt(name, problem, own_proposal, critiques) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f"Your current proposal:\n{own_proposal}\n\n"
        f"Critiques from all agents:\n{format_blocks(critiques)}\n\n"
        'Submit your revised proposal. Start with a short "Changes:" section '
        "stating what you changed and why (or why you changed nothing), "
        "then the full revised answer."
    )


def nominate_prompt(name, problem, proposals, names) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f"Current proposals:\n{format_blocks(proposals)}\n\n"
        "Which single proposal (including your own) is closest to correct?\n"
        "Reply with exactly one line in this format, then one sentence of "
        "reasoning:\nNOMINATE: <agent-name>\n"
        f"Valid agent names: {', '.join(names)}"
    )


def vote_prompt(name, problem, candidate_agent, candidate_text) -> str:
    return (
        f'You are agent "{name}" in a structured multi-agent debate.\n\n'
        f"Problem:\n{problem}\n\n"
        f'Candidate final answer (from agent "{candidate_agent}"):\n'
        f"{candidate_text}\n\n"
        "Do you accept this as the final answer? Reply with exactly one "
        "line, then your reasoning:\nVOTE: accept\nor\nVOTE: reject"
    )


def parse_nomination(text: str, valid_names: list) -> str | None:
    m = re.search(r"NOMINATE:\s*\"?([\w.-]+)", text, re.IGNORECASE)
    if m and m.group(1) in valid_names:
        return m.group(1)
    for name in valid_names:
        if re.search(rf"\b{re.escape(name)}\b", text):
            return name
    return None


def parse_vote(text: str) -> tuple:
    """Returns ("accept"|"reject", full_text). Unparseable counts as reject."""
    m = re.search(r"VOTE:\s*(accept|reject)", text, re.IGNORECASE)
    if m:
        return m.group(1).lower(), text.strip()
    first = next((l for l in text.splitlines() if l.strip()), "")
    return ("accept" if "accept" in first.lower() else "reject"), text.strip()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_prompts.py -v`
Expected: `9 passed`

- [ ] **Step 5: Commit**

```bash
git add debatelab/prompts.py tests/test_prompts.py
git commit -m "feat: phase prompt templates and nomination/vote parsers"
```

---

### Task 7: Protocol logic (phase sequencing, candidate selection, consensus)

**Files:**
- Create: `debatelab/protocol.py`
- Test: `tests/test_protocol.py`

**Interfaces:**
- Consumes: nothing
- Produces (all in `debatelab.protocol`):
  - `PHASES: tuple = ("propose", "critique", "revise", "vote")`
  - `next_phase(round_num: int, last_completed: str | None) -> tuple[int, str]` — `(0, None) → (1, "propose")`; after `"vote"` → `(round+1, "critique")` (rounds ≥ 2 skip propose); otherwise same round, next phase in `PHASES`
  - `select_candidate(nominations: dict[str, str], agent_order: list[str]) -> str` — plurality over nomination values; ties broken by lowest index in `agent_order`; empty nominations → `agent_order[0]`
  - `check_consensus(votes: dict[str, dict]) -> bool` — True iff non-empty and every entry has `vote == "accept"`

- [ ] **Step 1: Write the failing tests**

`tests/test_protocol.py`:

```python
from debatelab import protocol


def test_next_phase_fresh_debate_starts_with_propose():
    assert protocol.next_phase(0, None) == (1, "propose")


def test_next_phase_walks_round_one():
    assert protocol.next_phase(1, "propose") == (1, "critique")
    assert protocol.next_phase(1, "critique") == (1, "revise")
    assert protocol.next_phase(1, "revise") == (1, "vote")


def test_next_phase_after_vote_skips_propose():
    assert protocol.next_phase(1, "vote") == (2, "critique")
    assert protocol.next_phase(3, "vote") == (4, "critique")


def test_select_candidate_plurality():
    noms = {"a": "b", "b": "b", "c": "a"}
    assert protocol.select_candidate(noms, ["a", "b", "c"]) == "b"


def test_select_candidate_tie_breaks_by_config_order():
    noms = {"a": "c", "b": "b"}
    assert protocol.select_candidate(noms, ["a", "b", "c"]) == "b"


def test_select_candidate_empty_falls_back_to_first():
    assert protocol.select_candidate({}, ["a", "b"]) == "a"


def test_check_consensus():
    accept = {"vote": "accept", "reason": "r"}
    reject = {"vote": "reject", "reason": "r"}
    assert protocol.check_consensus({"a": accept, "b": accept}) is True
    assert protocol.check_consensus({"a": accept, "b": reject}) is False
    assert protocol.check_consensus({}) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -v`
Expected: FAIL — `ImportError` (module missing)

- [ ] **Step 3: Implement protocol.py**

`debatelab/protocol.py`:

```python
"""Pure debate-protocol logic: phase sequencing, candidate selection, consensus."""
from collections import Counter

PHASES = ("propose", "critique", "revise", "vote")


def next_phase(round_num: int, last_completed: str | None) -> tuple:
    """(round, phase) to run next. Round 1 starts with propose; after a vote
    without consensus the next round starts at critique (proposals carry over)."""
    if round_num == 0:
        return 1, "propose"
    if last_completed == "vote":
        return round_num + 1, "critique"
    return round_num, PHASES[PHASES.index(last_completed) + 1]


def select_candidate(nominations: dict, agent_order: list) -> str:
    """Plurality winner of nominations; ties broken by lowest agent_order
    index. Empty nominations fall back to the first agent in agent_order."""
    if not nominations:
        return agent_order[0]
    counts = Counter(nominations.values())
    best = max(counts.values())
    tied = [name for name, c in counts.items() if c == best]
    return min(tied, key=agent_order.index)


def check_consensus(votes: dict) -> bool:
    """Unanimous accept among agents that actually voted."""
    return bool(votes) and all(v["vote"] == "accept" for v in votes.values())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_protocol.py -v`
Expected: `7 passed`

- [ ] **Step 5: Commit**

```bash
git add debatelab/protocol.py tests/test_protocol.py
git commit -m "feat: protocol phase sequencing, candidate selection, consensus check"
```

---

### Task 8: Orchestrator (fan-out, retry/abstain, phases, resume)

**Files:**
- Create: `debatelab/orchestrator.py`
- Create: `tests/conftest.py`
- Test: `tests/test_orchestrator.py`

**Interfaces:**
- Consumes: `DebateStore`, `render_summary` (Task 5), `prompts` (Task 6), `protocol` (Task 7), `Agent`/`AgentError` (Task 2)
- Produces:
  - `debatelab.orchestrator.DebateHalted(Exception)`
  - `debatelab.orchestrator.Orchestrator(store: DebateStore, agents: list[Agent], progress: Callable[[str], None] = lambda msg: None)` — raises `ValueError` if fewer than 2 agents
  - `Orchestrator.run(debate_id: str, max_rounds: int | None = None) -> str` — returns final status (`awaiting_human`, `no_consensus`, or `error`); resumes from `state["last_completed_phase"]`; no-ops returning current status if already `approved`/`rejected`
  - `tests/conftest.py` provides `MockAgent(name, responses)` — pops scripted responses per `ask()` call; an `Exception` instance in the list is raised; an exhausted list raises `AgentError`

- [ ] **Step 1: Write MockAgent in conftest**

`tests/conftest.py`:

```python
from debatelab.agents.base import Agent, AgentError


class MockAgent(Agent):
    """Scripted agent: each ask() pops the next response. Exception instances
    are raised instead of returned; running out of responses raises AgentError."""

    def __init__(self, name, responses):
        super().__init__(name)
        self.responses = list(responses)
        self.prompts = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AgentError(f"{self.name}: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
```

- [ ] **Step 2: Write the failing tests**

Response counts per full round-1 run: propose(1) + critique(1) + revise(1) + nominate(1) + vote(1) = **5 per agent**. Each additional round adds 4 (no propose).

`tests/test_orchestrator.py`:

```python
import pytest

from debatelab.agents.base import AgentError
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent


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


def test_requires_two_agents(tmp_path):
    with pytest.raises(ValueError):
        Orchestrator(make_store(tmp_path), [MockAgent("solo", [])])


def test_single_round_consensus(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    agents = [happy_agent("a"), happy_agent("b"), happy_agent("c")]
    status = Orchestrator(store, agents).run(did)
    assert status == "awaiting_human"
    state = store.read_state(did)
    assert state["status"] == "awaiting_human"
    assert state["round"] == 1
    assert state["candidate"]["agent"] == "a"
    assert state["candidate"]["text"] == "revised proposal from a"
    assert all(v["vote"] == "accept" for v in state["votes"].values())
    types = [e["type"] for e in store.read_events(did)]
    for expected in ("proposal", "critique", "revision", "nomination",
                     "candidate", "vote", "consensus"):
        assert expected in types
    assert "pending human decision" in store.read_summary(did)


def test_no_consensus_after_max_rounds(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    dissenter = MockAgent("c", [
        "proposal from c", "critique from c", "revised from c",
        "NOMINATE: a\nok", "VOTE: reject\nstill wrong",
    ])
    agents = [happy_agent("a"), happy_agent("b"), dissenter]
    status = Orchestrator(store, agents).run(did, max_rounds=1)
    assert status == "no_consensus"
    state = store.read_state(did)
    assert state["votes"]["c"]["vote"] == "reject"
    assert any(e["type"] == "no_consensus" for e in store.read_events(did))


def test_retry_once_then_succeed(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    flaky = MockAgent("b", [
        AgentError("blip"), "proposal from b",   # propose: fail then retry ok
        "critique from b", "revised from b",
        "NOMINATE: a\nok", "VOTE: accept\nok",
    ])
    agents = [happy_agent("a"), flaky, happy_agent("c")]
    status = Orchestrator(store, agents).run(did)
    assert status == "awaiting_human"
    assert store.read_state(did)["abstained"] == []


def test_double_failure_abstains_and_continues(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    # b fails propose twice -> abstains; never called again for propose,
    # still participates from critique on (4 more calls).
    broken = MockAgent("b", [
        AgentError("down"), AgentError("still down"),
        "critique from b", "revised from b",
        "NOMINATE: a\nok", "VOTE: accept\nok",
    ])
    agents = [happy_agent("a"), broken, happy_agent("c")]
    status = Orchestrator(store, agents).run(did)
    assert status == "awaiting_human"
    events = store.read_events(did)
    assert any(e["type"] == "abstained" and e["agent"] == "b" for e in events)


def test_too_few_responders_halts_with_error(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    agents = [
        happy_agent("a"),
        MockAgent("b", [AgentError("x"), AgentError("x")]),
        MockAgent("c", [AgentError("x"), AgentError("x")]),
    ]
    status = Orchestrator(store, agents).run(did)
    assert status == "error"
    state = store.read_state(did)
    assert state["status"] == "error"
    assert any(e["type"] == "error" for e in store.read_events(did))


def test_resume_after_interrupted_phase(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem?")
    # First run: everyone proposes, then all fail critique -> error halt.
    first = [
        MockAgent("a", ["proposal from a"]),
        MockAgent("b", ["proposal from b"]),
    ]
    status = Orchestrator(store, first).run(did)
    assert status == "error"
    assert store.read_state(did)["last_completed_phase"] == "propose"

    # Second run resumes at critique — no new propose calls.
    second = [
        MockAgent("a", ["crit a", "rev a", "NOMINATE: a\nok", "VOTE: accept\nok"]),
        MockAgent("b", ["crit b", "rev b", "NOMINATE: a\nok", "VOTE: accept\nok"]),
    ]
    status = Orchestrator(store, second).run(did)
    assert status == "awaiting_human"
    events = store.read_events(did)
    assert sum(1 for e in events if e["type"] == "proposal") == 2  # not re-run
    state = store.read_state(did)
    assert state["round"] == 1
    assert state["proposals"]["a"] == "rev a"


def test_finished_debate_is_not_rerun(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "p")
    state = store.read_state(did)
    state["status"] = "approved"
    store.write_state(did, state)
    agents = [MockAgent("a", []), MockAgent("b", [])]
    assert Orchestrator(store, agents).run(did) == "approved"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.orchestrator'`

- [ ] **Step 4: Implement orchestrator.py**

`debatelab/orchestrator.py`:

```python
"""Runs a debate: fans out phase prompts to all agents, applies the protocol,
checkpoints state after every phase so interrupted runs resume."""
import concurrent.futures as cf

from . import prompts, protocol
from .agents.base import AgentError
from .store import render_summary


class DebateHalted(Exception):
    """Too few agents responded to continue the debate."""


class Orchestrator:
    def __init__(self, store, agents, progress=lambda msg: None):
        if len(agents) < 2:
            raise ValueError("a debate needs at least 2 agents")
        self.store = store
        self.agents = {a.name: a for a in agents}
        self.order = [a.name for a in agents]
        self.progress = progress

    def run(self, debate_id: str, max_rounds: int | None = None) -> str:
        state = self.store.read_state(debate_id)
        if state["status"] in ("approved", "rejected"):
            return state["status"]
        if max_rounds is not None:
            state["max_rounds"] = max_rounds
        state["status"] = "running"
        problem = self.store.read_problem(debate_id)
        try:
            while True:
                rnd, phase = protocol.next_phase(
                    state["round"], state["last_completed_phase"]
                )
                if rnd > state["max_rounds"]:
                    state["status"] = "no_consensus"
                    self.store.append_event(debate_id, {
                        "round": state["round"], "phase": "end", "agent": None,
                        "type": "no_consensus",
                        "content": (
                            f"no unanimous vote after {state['max_rounds']} rounds"
                        ),
                    })
                    break
                state["round"] = rnd
                state["abstained"] = []
                self.progress(f"round {rnd}/{state['max_rounds']}: {phase}")
                getattr(self, f"_phase_{phase}")(debate_id, state, problem)
                state["last_completed_phase"] = phase
                self._checkpoint(debate_id, state)
                if phase == "vote" and protocol.check_consensus(state["votes"]):
                    state["status"] = "awaiting_human"
                    self.store.append_event(debate_id, {
                        "round": rnd, "phase": "vote",
                        "agent": state["candidate"]["agent"],
                        "type": "consensus",
                        "content": state["candidate"]["text"],
                    })
                    break
        except DebateHalted as e:
            state["status"] = "error"
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "end", "agent": None,
                "type": "error", "content": str(e),
            })
        self._checkpoint(debate_id, state)
        self.store.rebuild_index()
        return state["status"]

    def _checkpoint(self, debate_id, state):
        self.store.write_state(debate_id, state)
        self.store.write_summary(debate_id, render_summary(state))

    def _fanout(self, debate_id, state, phase, prompt_for) -> dict:
        """Ask every agent concurrently. One retry per agent; a second failure
        records an abstention. Raises DebateHalted if fewer than 2 responded."""
        results = {}

        def call(name):
            prompt = prompt_for(name)
            try:
                return self.agents[name].ask(prompt)
            except AgentError:
                return self.agents[name].ask(prompt)  # one retry

        with cf.ThreadPoolExecutor(max_workers=len(self.order)) as ex:
            futures = {ex.submit(call, name): name for name in self.order}
            for fut in cf.as_completed(futures):
                name = futures[fut]
                try:
                    results[name] = fut.result()
                except AgentError as e:
                    state["abstained"] = sorted(set(state["abstained"]) | {name})
                    self.store.append_event(debate_id, {
                        "round": state["round"], "phase": phase, "agent": name,
                        "type": "abstained", "content": str(e),
                    })
        if len(results) < 2:
            raise DebateHalted(
                f"only {len(results)} agent(s) responded in phase "
                f"'{phase}' — need at least 2"
            )
        return results

    def _phase_propose(self, debate_id, state, problem):
        results = self._fanout(
            debate_id, state, "propose",
            lambda name: prompts.propose_prompt(name, problem),
        )
        state["proposals"] = results
        for name, text in results.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "propose", "agent": name,
                "type": "proposal", "content": text,
            })

    def _phase_critique(self, debate_id, state, problem):
        proposals = state["proposals"]
        reject_reasons = {
            name: v["reason"]
            for name, v in state.get("votes", {}).items()
            if v["vote"] == "reject"
        }

        def prompt_for(name):
            others = {n: t for n, t in proposals.items() if n != name}
            return prompts.critique_prompt(
                name, problem, others, reject_reasons or None
            )

        results = self._fanout(debate_id, state, "critique", prompt_for)
        state["critiques"] = results
        for name, text in results.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "critique", "agent": name,
                "type": "critique", "content": text,
            })

    def _phase_revise(self, debate_id, state, problem):
        def prompt_for(name):
            own = state["proposals"].get(name, "(no previous proposal)")
            return prompts.revise_prompt(name, problem, own, state["critiques"])

        results = self._fanout(debate_id, state, "revise", prompt_for)
        state["proposals"] = {**state["proposals"], **results}
        for name, text in results.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "revise", "agent": name,
                "type": "revision", "content": text,
            })

    def _phase_vote(self, debate_id, state, problem):
        proposals = state["proposals"]
        names = list(proposals)
        nom_raw = self._fanout(
            debate_id, state, "vote",
            lambda name: prompts.nominate_prompt(name, problem, proposals, names),
        )
        nominations = {}
        for name, text in nom_raw.items():
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "nomination", "content": text,
            })
            nominee = prompts.parse_nomination(text, names)
            if nominee:
                nominations[name] = nominee
        order_with_proposals = [n for n in self.order if n in proposals]
        winner = protocol.select_candidate(nominations, order_with_proposals)
        state["candidate"] = {"agent": winner, "text": proposals[winner]}
        self.store.append_event(debate_id, {
            "round": state["round"], "phase": "vote", "agent": winner,
            "type": "candidate", "content": proposals[winner],
        })
        vote_raw = self._fanout(
            debate_id, state, "vote",
            lambda name: prompts.vote_prompt(
                name, problem, winner, proposals[winner]
            ),
        )
        votes = {}
        for name, text in vote_raw.items():
            verdict, reason = prompts.parse_vote(text)
            votes[name] = {"vote": verdict, "reason": reason}
            self.store.append_event(debate_id, {
                "round": state["round"], "phase": "vote", "agent": name,
                "type": "vote", "verdict": verdict, "content": text,
            })
        state["votes"] = votes
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -v`
Expected: `8 passed`

- [ ] **Step 6: Run the whole suite**

Run: `.venv/bin/python -m pytest -v`
Expected: all tests pass (Tasks 1–8).

- [ ] **Step 7: Commit**

```bash
git add debatelab/orchestrator.py tests/conftest.py tests/test_orchestrator.py
git commit -m "feat: debate orchestrator with fan-out, retry/abstain, resume"
```

---

### Task 9: CLI (new / run / status / list / show / approve / reject / agents)

**Files:**
- Create: `debatelab/cli.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `DebateStore`, `render_summary` (Task 5), `registry` (Task 4), `Orchestrator` (Task 8)
- Produces:
  - `debatelab.cli.main(argv: list[str] | None = None)` — argparse entry point (the `debate` console script)
  - `debatelab.cli.get_store() -> DebateStore` — store rooted at `Path.cwd() / "debates"` (tests monkeypatch cwd)
  - Human decisions recorded as event `{round, phase: "human", agent: "human", type: "human_decision", content: "approved"|"rejected", "note": str}`
  - `serve` subcommand is registered here but implemented in Task 10 (`cmd_serve` placeholder raising `SystemExit("serve: implemented in Task 10")` until then)

- [ ] **Step 1: Write the failing tests**

`tests/test_cli.py`:

```python
import json

import pytest

from debatelab import cli
from debatelab.store import DebateStore


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_new_creates_debate_and_prints_id(workdir, capsys):
    cli.main(["new", "Pick a database"])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    assert store.read_state(debate_id)["status"] == "created"


def test_new_with_context_files(workdir, capsys):
    ctx = workdir / "notes.md"
    ctx.write_text("important context")
    cli.main(["new", "Pick a database", "--context", str(ctx)])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    assert "important context" in store.read_problem(debate_id)


def test_status_and_list(workdir, capsys):
    cli.main(["new", "First topic"])
    debate_id = capsys.readouterr().out.strip()
    cli.main(["status", debate_id])
    out = capsys.readouterr().out
    assert debate_id in out and "created" in out
    cli.main(["list"])
    assert debate_id in capsys.readouterr().out


def test_show_prints_summary(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    store.write_summary(debate_id, "# my summary\n")
    cli.main(["show", debate_id])
    assert "# my summary" in capsys.readouterr().out


def _make_awaiting(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    store = DebateStore(workdir / "debates")
    state = store.read_state(debate_id)
    state["status"] = "awaiting_human"
    state["candidate"] = {"agent": "a", "text": "the answer"}
    store.write_state(debate_id, state)
    return store, debate_id


def test_approve_records_decision(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    cli.main(["approve", debate_id, "-m", "looks right"])
    state = store.read_state(debate_id)
    assert state["status"] == "approved"
    assert state["human_decision"] == {"decision": "approved", "note": "looks right"}
    events = store.read_events(debate_id)
    assert events[-1]["type"] == "human_decision"
    assert "APPROVED" in store.read_summary(debate_id)
    index = json.loads((workdir / "debates" / "index.json").read_text())
    assert index[0]["status"] == "approved"


def test_reject_requires_message(workdir, capsys):
    store, debate_id = _make_awaiting(workdir, capsys)
    with pytest.raises(SystemExit):
        cli.main(["reject", debate_id])
    cli.main(["reject", debate_id, "-m", "not convincing"])
    assert store.read_state(debate_id)["status"] == "rejected"


def test_approve_wrong_status_exits(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    with pytest.raises(SystemExit):
        cli.main(["approve", debate_id])


def test_agents_command_reports_readiness(workdir, capsys, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: echoer\n    backend: cli\n"
        "    command: [\"echo\", \"{prompt}\"]\n"
        "  - name: keyless\n    backend: api\n    provider: openai\n"
        "    model: m\n    api_key_env: MISSING_KEY\n"
        "  - name: off\n    backend: cli\n    command: [\"echo\"]\n"
        "    enabled: false\n"
    )
    cli.main(["agents"])
    out = capsys.readouterr().out
    assert "echoer" in out and "ready" in out
    assert "MISSING_KEY" in out
    assert "disabled" in out


def test_run_needs_two_enabled_agents(workdir, capsys):
    cli.main(["new", "Topic"])
    debate_id = capsys.readouterr().out.strip()
    (workdir / "agents.yaml").write_text(
        "agents:\n"
        "  - name: only\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
    )
    with pytest.raises(SystemExit, match="at least 2"):
        cli.main(["run", debate_id])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.cli'`

- [ ] **Step 3: Implement cli.py**

`debatelab/cli.py`:

```python
"""The `debate` command-line interface."""
import argparse
import sys
from pathlib import Path

from .agents import registry
from .store import DebateStore, render_summary


def get_store() -> DebateStore:
    return DebateStore(Path.cwd() / "debates")


def cmd_new(args):
    store = get_store()
    contexts = []
    for f in args.context or []:
        p = Path(f)
        contexts.append((p.name, p.read_text()))
    title = args.problem.strip().splitlines()[0][:60]
    print(store.create(title, args.problem, contexts))


def cmd_run(args):
    from .orchestrator import Orchestrator

    store = get_store()
    specs = registry.load_agent_specs(args.config)
    agents = registry.build_agents(specs)
    try:
        orch = Orchestrator(store, agents,
                            progress=lambda m: print(m, flush=True))
    except ValueError as e:
        sys.exit(str(e))
    status = orch.run(args.id, max_rounds=args.max_rounds)
    print(f"final status: {status}")
    if status in ("awaiting_human", "no_consensus"):
        print(f"review with `debate show {args.id}`, then "
              f"`debate approve {args.id}` or `debate reject {args.id} -m ...`")


def _status_line(state):
    return (f"{state['id']}: {state['status']} "
            f"(round {state['round']}/{state['max_rounds']})")


def cmd_status(args):
    print(_status_line(get_store().read_state(args.id)))


def cmd_list(args):
    store = get_store()
    for did in store.list_ids():
        print(_status_line(store.read_state(did)))


def cmd_show(args):
    print(get_store().read_summary(args.id) or "(no summary yet)")


def cmd_decide(args, decision):
    store = get_store()
    state = store.read_state(args.id)
    if state["status"] not in ("awaiting_human", "no_consensus"):
        sys.exit(f"debate is '{state['status']}' — "
                 "nothing is awaiting a human decision")
    note = args.message or ""
    state["human_decision"] = {"decision": decision, "note": note}
    state["status"] = decision
    store.append_event(args.id, {
        "round": state["round"], "phase": "human", "agent": "human",
        "type": "human_decision", "content": decision, "note": note,
    })
    store.write_state(args.id, state)
    store.write_summary(args.id, render_summary(state))
    store.rebuild_index()
    print(f"{args.id}: {decision}")


def cmd_agents(args):
    specs = registry.load_agent_specs(args.config)
    for spec in specs:
        if not spec.enabled:
            verdict = "disabled"
        else:
            problem = registry.spec_problem(spec)
            verdict = f"NOT READY — {problem}" if problem else "ready"
        print(f"{spec.name:<12} {spec.backend:<4} {verdict}")
    if args.ping:
        ready = [s for s in specs
                 if s.enabled and registry.spec_problem(s) is None]
        for agent in registry.build_agents(ready):
            try:
                agent.ask("Reply with the single word: pong")
                print(f"{agent.name}: ping ok")
            except Exception as e:
                print(f"{agent.name}: ping FAILED — {e}")


def cmd_serve(args):
    sys.exit("serve: implemented in Task 10")


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="debate", description="Multi-agent AI debate orchestrator")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("new", help="create a debate")
    sp.add_argument("problem")
    sp.add_argument("--context", nargs="*", help="context files to include")
    sp.set_defaults(fn=cmd_new)

    sp = sub.add_parser("run", help="run debate rounds until consensus or cap")
    sp.add_argument("id")
    sp.add_argument("--max-rounds", type=int, default=None)
    sp.add_argument("--config", default="agents.yaml")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("status", help="show a debate's status")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("list", help="list all debates")
    sp.set_defaults(fn=cmd_list)

    sp = sub.add_parser("show", help="print a debate's summary")
    sp.add_argument("id")
    sp.set_defaults(fn=cmd_show)

    sp = sub.add_parser("approve", help="approve the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", default="")
    sp.set_defaults(fn=lambda a: cmd_decide(a, "approved"))

    sp = sub.add_parser("reject", help="reject the consensus answer")
    sp.add_argument("id")
    sp.add_argument("-m", "--message", required=True)
    sp.set_defaults(fn=lambda a: cmd_decide(a, "rejected"))

    sp = sub.add_parser("agents", help="list configured agents and readiness")
    sp.add_argument("--config", default="agents.yaml")
    sp.add_argument("--ping", action="store_true",
                    help="send a live test prompt to each ready agent")
    sp.set_defaults(fn=cmd_agents)

    sp = sub.add_parser("serve", help="serve the web viewer")
    sp.add_argument("--port", type=int, default=8080)
    sp.set_defaults(fn=cmd_serve)

    args = parser.parse_args(argv)
    try:
        args.fn(args)
    except registry.ConfigError as e:
        sys.exit(f"config error: {e}")
    except FileNotFoundError as e:
        sys.exit(str(e))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_cli.py -v`
Expected: `10 passed`

- [ ] **Step 5: Commit**

```bash
git add debatelab/cli.py tests/test_cli.py
git commit -m "feat: debate CLI with lifecycle, human gate, agent readiness"
```

---

### Task 10: Web viewer + serve command

**Files:**
- Create: `debatelab/viewer/index.html`
- Modify: `debatelab/cli.py` (replace `cmd_serve` placeholder; add `make_server`)
- Test: `tests/test_serve.py`

**Interfaces:**
- Consumes: `debates/index.json`, `debates/<id>/state.json`, `debates/<id>/transcript.jsonl` (shapes from Tasks 5/8)
- Produces:
  - `debatelab.cli.make_server(port: int, directory: str) -> http.server.ThreadingHTTPServer` — `/` serves the packaged viewer HTML; every other path is served from `directory` (so `/debates/...` works)
  - `cmd_serve` runs `make_server(args.port, str(Path.cwd())).serve_forever()`

- [ ] **Step 1: Write the failing tests**

`tests/test_serve.py`:

```python
import json
import threading
import urllib.request

import pytest

from debatelab.cli import make_server
from debatelab.store import DebateStore


@pytest.fixture
def running_server(tmp_path):
    store = DebateStore(tmp_path / "debates")
    store.create("Viewer test", "problem text")
    srv = make_server(0, str(tmp_path))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def test_root_serves_viewer(running_server):
    status, body = get(running_server + "/")
    assert status == 200
    assert "AI Debate Lab" in body


def test_debates_index_served(running_server):
    status, body = get(running_server + "/debates/index.json")
    assert status == 200
    entries = json.loads(body)
    assert entries[0]["title"] == "Viewer test"


def test_debate_state_served(running_server):
    _, body = get(running_server + "/debates/index.json")
    debate_id = json.loads(body)[0]["id"]
    status, body = get(f"{running_server}/debates/{debate_id}/state.json")
    assert status == 200
    assert json.loads(body)["status"] == "created"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_serve.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_server'`

- [ ] **Step 3: Create the viewer**

`debatelab/viewer/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>AI Debate Lab</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg:#ffffff; --fg:#1a1d23; --muted:#5c6470; --card:#f4f5f7;
    --line:#e2e4e9; --accent:#3355cc; --ok:#1a7f37; --bad:#b42318;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:#12141a; --fg:#e6e8ee; --muted:#9aa1ad; --card:#1c1f27;
      --line:#2a2e38; --accent:#7a97ff; --ok:#4ade80; --bad:#f87171;
    }
  }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.55 system-ui,sans-serif;
         background:var(--bg); color:var(--fg); }
  header { padding:16px 24px; border-bottom:1px solid var(--line);
           display:flex; align-items:baseline; gap:16px; }
  header h1 { font-size:18px; margin:0; }
  header a { color:var(--accent); text-decoration:none; font-size:14px; }
  main { max-width:980px; margin:0 auto; padding:24px; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:14px 16px; margin:10px 0; }
  .row { display:flex; justify-content:space-between; gap:12px;
         align-items:baseline; flex-wrap:wrap; }
  .muted { color:var(--muted); font-size:13px; }
  .badge { font-size:12px; padding:2px 10px; border-radius:999px;
           border:1px solid var(--line); white-space:nowrap; }
  .badge.approved, .badge.awaiting_human { color:var(--ok); }
  .badge.rejected, .badge.error, .badge.no_consensus { color:var(--bad); }
  .badge.running { color:var(--accent); }
  a.debate-link { color:var(--fg); text-decoration:none; font-weight:600; }
  h2 { font-size:16px; margin:26px 0 6px; }
  h3 { font-size:14px; margin:0 0 6px; color:var(--accent); }
  pre { white-space:pre-wrap; word-break:break-word; margin:0;
        font:13px/1.5 ui-monospace,monospace; }
  details { margin:8px 0; }
  summary { cursor:pointer; color:var(--muted); font-size:13px; }
  .vote-accept { color:var(--ok); font-weight:600; }
  .vote-reject, .vote-abstained { color:var(--bad); font-weight:600; }
  .banner { border-left:4px solid var(--accent); }
</style>
</head>
<body>
<header>
  <h1>AI Debate Lab</h1>
  <a href="#" id="back" hidden>&larr; all debates</a>
</header>
<main id="app"><p class="muted">loading…</p></main>
<script>
const app = document.getElementById("app");
const back = document.getElementById("back");
let pollTimer = null;

function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}
async function fetchJSON(url) {
  const r = await fetch(url + "?t=" + Date.now());
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}
async function fetchJSONL(url) {
  const r = await fetch(url + "?t=" + Date.now());
  if (!r.ok) return [];
  const text = await r.text();
  return text.split("\n").filter(l => l.trim()).map(l => JSON.parse(l));
}
function badge(status) {
  return `<span class="badge ${esc(status)}">${esc(status)}</span>`;
}

async function showList() {
  back.hidden = true;
  let entries = [];
  try { entries = await fetchJSON("/debates/index.json"); }
  catch { app.innerHTML = "<p class='muted'>No debates yet.</p>"; return; }
  if (!entries.length) {
    app.innerHTML = "<p class='muted'>No debates yet.</p>";
    return;
  }
  app.innerHTML = entries.map(e => `
    <div class="card row">
      <a class="debate-link" href="#${esc(e.id)}">${esc(e.title)}</a>
      <span>
        <span class="muted">round ${e.round}</span>
        ${badge(e.status)}
      </span>
    </div>`).join("");
}

function eventCard(ev) {
  const label = `${esc(ev.agent ?? "system")} · ${esc(ev.type)}` +
    (ev.verdict ? ` · <span class="vote-${esc(ev.verdict)}">${esc(ev.verdict)}</span>` : "");
  return `
    <details class="card">
      <summary>${label}</summary>
      <pre>${esc(ev.content)}</pre>
    </details>`;
}

async function showDebate(id) {
  back.hidden = false;
  let state, events;
  try {
    state = await fetchJSON(`/debates/${id}/state.json`);
    events = await fetchJSONL(`/debates/${id}/transcript.jsonl`);
  } catch (e) {
    app.innerHTML = `<p class="muted">failed to load ${esc(id)}: ${esc(String(e))}</p>`;
    return;
  }
  let html = `
    <div class="card banner row">
      <div><strong>${esc(state.title)}</strong><br>
        <span class="muted">${esc(id)} · round ${state.round}/${state.max_rounds}</span>
      </div>
      ${badge(state.status)}
    </div>`;
  if (state.human_decision) {
    html += `<div class="card banner"><h3>Human decision: ${esc(state.human_decision.decision).toUpperCase()}</h3>
      <pre>${esc(state.human_decision.note || "(no note)")}</pre></div>`;
  }
  if (state.candidate) {
    html += `<h2>Candidate answer (from ${esc(state.candidate.agent)})</h2>
      <div class="card"><pre>${esc(state.candidate.text)}</pre></div>`;
  }
  const votes = Object.entries(state.votes || {});
  if (votes.length || (state.abstained || []).length) {
    html += "<h2>Latest votes</h2><div class='card'>" +
      votes.map(([a, v]) =>
        `<div>${esc(a)}: <span class="vote-${esc(v.vote)}">${esc(v.vote)}</span></div>`
      ).join("") +
      (state.abstained || []).map(a =>
        `<div>${esc(a)}: <span class="vote-abstained">abstained</span></div>`
      ).join("") + "</div>";
  }
  const rounds = [...new Set(events.map(e => e.round))];
  for (const r of rounds) {
    html += `<h2>Round ${esc(String(r))}</h2>`;
    html += events.filter(e => e.round === r).map(eventCard).join("");
  }
  app.innerHTML = html;
  if (state.status === "running") {
    pollTimer = setTimeout(() => route(), 3000);
  }
}

function route() {
  clearTimeout(pollTimer);
  const id = decodeURIComponent(location.hash.slice(1));
  if (id) showDebate(id); else showList();
}
window.addEventListener("hashchange", route);
route();
</script>
</body>
</html>
```

- [ ] **Step 4: Replace cmd_serve in cli.py**

In `debatelab/cli.py`, add imports at the top:

```python
import functools
import http.server
from importlib import resources
```

Replace the `cmd_serve` placeholder with:

```python
def make_server(port: int, directory: str) -> http.server.ThreadingHTTPServer:
    viewer_html = (
        resources.files("debatelab").joinpath("viewer/index.html").read_text()
    )

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] in ("/", "/index.html"):
                body = viewer_html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                super().do_GET()

        def log_message(self, *args):
            pass

    handler = functools.partial(Handler, directory=directory)
    return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)


def cmd_serve(args):
    srv = make_server(args.port, str(Path.cwd()))
    print(f"viewer at http://127.0.0.1:{srv.server_address[1]}/ (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_serve.py -v`
Expected: `3 passed`

- [ ] **Step 6: Reinstall so package data ships, run full suite**

```bash
.venv/bin/pip install -e ".[dev]" --quiet
.venv/bin/python -m pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add debatelab/viewer/index.html debatelab/cli.py tests/test_serve.py
git commit -m "feat: static web viewer and serve command"
```

---

### Task 11: End-to-end integration test, sample config, README

**Files:**
- Create: `tests/test_integration.py`
- Create: `agents.yaml`
- Create: `README.md`

**Interfaces:**
- Consumes: everything above; `tests/conftest.py` `MockAgent`
- Produces: proof the whole pipeline works via `cli.main`, plus the shipped default config and docs

- [ ] **Step 1: Write the failing integration test**

Scenario: three mock agents; round 1 ends with one rejection; round 2 converges; human approves via CLI. `registry.build_agents` is monkeypatched so `debate run` uses mocks.

`tests/test_integration.py`:

```python
"""Full pipeline: new -> run (2 rounds to consensus) -> approve, all via cli.main."""
import json

import pytest

from debatelab import cli
from debatelab.agents import registry
from debatelab.store import DebateStore

from .conftest import MockAgent


def scripted_agents():
    # Round 1: propose, critique, revise, nominate, vote (charlie rejects).
    # Round 2: critique, revise, nominate, vote (all accept).
    alpha = MockAgent("alpha", [
        "use postgres",
        "bravo's idea lacks indexes; charlie ignores cost",
        "use postgres with read replicas",
        "NOMINATE: alpha\nmost complete",
        "VOTE: accept\nfine",
        "charlie's concern is addressed now",
        "use postgres with read replicas and pgbouncer",
        "NOMINATE: alpha\nstill best",
        "VOTE: accept\ngood",
    ])
    bravo = MockAgent("bravo", [
        "use mysql",
        "alpha is solid; charlie's is vague",
        "postgres is fine actually",
        "NOMINATE: alpha\nconvinced",
        "VOTE: accept\nworks",
        "agree with the pooling addition",
        "postgres with replicas works",
        "NOMINATE: alpha\nyes",
        "VOTE: accept\nship it",
    ])
    charlie = MockAgent("charlie", [
        "use sqlite",
        "both overkill for our scale",
        "fine, postgres, but keep it simple",
        "NOMINATE: alpha\nok",
        "VOTE: reject\nno connection pooling story",
        "pooling is in now",
        "postgres with pgbouncer is acceptable",
        "NOMINATE: alpha\nagreed",
        "VOTE: accept\nsatisfied",
    ])
    return [alpha, bravo, charlie]


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "agents.yaml").write_text("agents: []\n")
    monkeypatch.setattr(registry, "load_agent_specs", lambda path: [])
    monkeypatch.setattr(registry, "build_agents", lambda specs: scripted_agents())
    return tmp_path


def test_full_debate_to_approval(workdir, capsys):
    cli.main(["new", "Which database should we use?"])
    debate_id = capsys.readouterr().out.strip()

    cli.main(["run", debate_id])
    out = capsys.readouterr().out
    assert "final status: awaiting_human" in out

    store = DebateStore(workdir / "debates")
    state = store.read_state(debate_id)
    assert state["round"] == 2
    assert state["candidate"]["agent"] == "alpha"
    assert "pgbouncer" in state["candidate"]["text"]

    events = store.read_events(debate_id)
    rejects = [e for e in events if e["type"] == "vote" and e["verdict"] == "reject"]
    assert len(rejects) == 1 and rejects[0]["agent"] == "charlie"
    assert sum(1 for e in events if e["type"] == "proposal") == 3  # round 1 only
    assert any(e["type"] == "consensus" for e in events)
    assert any(e["round"] == 2 and e["type"] == "critique" for e in events)

    # Human gate: summary is pending until approval.
    assert "pending human decision" in store.read_summary(debate_id)
    cli.main(["approve", debate_id, "-m", "ship it"])
    capsys.readouterr()
    state = store.read_state(debate_id)
    assert state["status"] == "approved"
    assert state["human_decision"]["note"] == "ship it"
    assert "APPROVED" in store.read_summary(debate_id)
    assert any(e["type"] == "human_decision" for e in store.read_events(debate_id))

    index = json.loads((workdir / "debates" / "index.json").read_text())
    assert index[0]["status"] == "approved"


def test_reject_reasons_reach_round_two_prompts(workdir, capsys, monkeypatch):
    cli.main(["new", "Which database should we use?"])
    debate_id = capsys.readouterr().out.strip()
    # Patch build_agents to return this exact list so we can inspect prompts.
    agents = scripted_agents()
    monkeypatch.setattr(registry, "build_agents", lambda specs: agents)
    cli.main(["run", debate_id])
    capsys.readouterr()
    alpha = agents[0]
    round2_critique_prompt = alpha.prompts[5]  # calls 0-4 are round 1
    assert "Rejection reasons" in round2_critique_prompt
    assert "no connection pooling story" in round2_critique_prompt
```

- [ ] **Step 2: Run the test to verify current behavior**

Run: `.venv/bin/python -m pytest tests/test_integration.py -v`
Expected: PASS if Tasks 1–10 are correct — this test validates wiring, not new code. If it FAILS, debug the orchestrator/CLI (this is the point of the integration test); do not weaken assertions to make it pass.

- [ ] **Step 3: Create the sample agents.yaml**

`agents.yaml` (repo root):

```yaml
# AI Debate Lab agent roster.
# Add/remove entries or flip `enabled` to control who debates.
# API keys come from environment variables only.
agents:
  - name: claude
    backend: cli
    command: ["claude", "-p", "{prompt}"]
    enabled: true

  - name: chatgpt
    backend: api
    provider: openai
    model: gpt-5
    api_key_env: OPENAI_API_KEY
    enabled: true

  - name: gemini
    backend: cli
    command: ["gemini", "-p", "{prompt}"]
    enabled: true

  - name: grok
    backend: api
    provider: openai          # xAI's API is OpenAI-compatible
    base_url: https://api.x.ai/v1
    model: grok-4
    api_key_env: XAI_API_KEY
    enabled: true
```

- [ ] **Step 4: Create README.md**

```markdown
# AI Debate Lab

Multiple AI agents (Claude, ChatGPT, Gemini, Grok, ...) analyze the same
problem in a structured debate — propose, critique, revise, vote — until
they unanimously agree on one answer. Nothing is final until a human
approves it. Every artifact is a plain file you can read, diff, and commit.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
source .venv/bin/activate
```

Configure agents in `agents.yaml` (flip `enabled`, add entries, set
`api_key_env` env vars for API-backed agents). Check readiness:

```bash
debate agents          # static readiness check
debate agents --ping   # live test prompt to each ready agent
```

## Run a debate

```bash
debate new "Which caching strategy should we adopt?" --context notes.md
debate run 20260714-which-caching-strategy   # streams phase progress
debate show 20260714-which-caching-strategy  # read the summary
debate approve 20260714-which-caching-strategy -m "agreed"
# or: debate reject <id> -m "reason"
```

`run` resumes from the last completed phase if interrupted. If no unanimous
vote happens within `--max-rounds` (default 5), the debate ends
`no_consensus` with all dissents recorded — you still decide with
approve/reject.

## Watch in the browser

```bash
debate serve --port 8080   # http://127.0.0.1:8080/
```

## How a debate is stored (`debates/<id>/`)

| File | Purpose |
|---|---|
| `problem.md` | Problem + shared context |
| `transcript.jsonl` | Append-only event log — source of truth |
| `state.json` | Current derived state (resume checkpoint) |
| `summary.md` | Human-readable summary, regenerated each round |

## Protocol

Each round: **critique → revise → vote** (round 1 starts with **propose**).
The vote phase nominates a candidate (plurality, config-order tie-break),
then every agent accepts/rejects it. Unanimous accept = consensus →
`awaiting_human`. Failed agent calls retry once, then abstain for the
phase; a phase needs at least 2 responders.

## Tests

```bash
.venv/bin/python -m pytest -v
```
```

- [ ] **Step 5: Run the full suite one last time**

Run: `.venv/bin/python -m pytest -v`
Expected: all tests pass.

- [ ] **Step 6: Smoke-test the real CLI entry point**

```bash
.venv/bin/debate agents || true
.venv/bin/debate list
```

Expected: `agents` prints the four sample agents with readiness verdicts (NOT READY lines are fine on a machine without keys/CLIs); `list` prints nothing (no debates yet) and exits 0.

- [ ] **Step 7: Commit**

```bash
git add tests/test_integration.py agents.yaml README.md
git commit -m "feat: end-to-end integration test, sample agent roster, README"
```
