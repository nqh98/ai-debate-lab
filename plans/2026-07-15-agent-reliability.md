# Agent Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a transient agent failure cost a retry instead of a vote, and make the reason any agent dropped out recoverable from the transcript.

**Architecture:** Six tasks against the existing `debatelab` package. Tasks 1–3 push classification down to the raise sites, where the facts still exist (`AgentError` gains `kind`/`retry_after`; `cli_agent` and `api_agent` populate them). Task 4 adds `debatelab/retry.py`, a new pure module holding full-jitter backoff. Tasks 5–6 wire it into the orchestrator and record every attempt. `protocol.py` is never touched — retries change how *often* an agent abstains, never what an abstention means.

**Tech Stack:** Python ≥ 3.10, PyYAML, stdlib only (`enum`, `random`, `time`, `subprocess`, `urllib`, `concurrent.futures`). pytest.

**Spec:** `specs/2026-07-15-agent-reliability-design.md`

## Global Constraints

- Python ≥ 3.10; runtime dependencies: **PyYAML only**. No new dependencies — `tenacity`/`backoff` are explicitly rejected; full-jitter backoff is four lines of stdlib.
- **`protocol.py` must not be modified by any task in this plan.** Not one line.
- **`debatelab/retry.py` must not import `store`, `orchestrator`, or `prompts`.** It may import `agents.base` (it reads `AgentError.retryable`). No files, no network, no debate knowledge. The *pacing* clock and the RNG are injected.
- **No test may sleep for real.** The suite is 9 seconds and must stay single-digit. Task 5 installs the autouse fixture that guarantees this; every task after it depends on that fixture existing.
- **Classification never reads prose.** Kinds are derived from HTTP status codes, exit codes, and exception types only. Matching `proc.stderr` against `rate.?limit` is explicitly rejected by the spec (§3) — it is the `parse_vote` mistake one layer down.
- `str(AgentError)` must not change: existing transcript `content` and every test matching on a message (`match="exit 3"`, `match="timed out"`, `match="not found"`) depend on it.
- Transcript event schema: `{ts, round, phase, agent, type, content}`, extra keys allowed.
- Commit messages: conventional style (`feat:`, `fix:`, `test:`), **no attribution trailers of any kind**.
- All commands run from repo root `/home/bossbaby/Desktop/fix-me/ai-debate-lab`; Python is `.venv/bin/python`.
- Baseline before starting: `.venv/bin/python -m pytest -q` ⇒ **158 passed**.

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `debatelab/agents/base.py` | `ErrorKind` vocabulary; `AgentError` carrying `kind`/`retry_after`; the single definition of `retryable` | 1 |
| `debatelab/agents/cli_agent.py` | Classify a subprocess failure from exception type and exit code | 2 |
| `debatelab/agents/api_agent.py` | Classify an HTTP failure from the status code; read `Retry-After` | 3 |
| `debatelab/retry.py` **(new)** | Pure backoff arithmetic and the retry loop. No I/O | 4 |
| `debatelab/orchestrator.py` | Inject clock/RNG; route `_fanout` and `_reask` through `call_with_retry` | 5 |
| `debatelab/orchestrator.py` | Emit `agent_call` telemetry via the `on_attempt` hook | 6 |
| `tests/conftest.py` | Autouse fixture neutralizing backoff sleep for the whole suite | 5 |

---

### Task 1: `AgentError` carries a classification

**Files:**
- Modify: `debatelab/agents/base.py:1-19` (whole file)
- Test: `tests/test_agent_error.py` (create)

**Interfaces:**
- Consumes: nothing
- Produces:
  - `base.ErrorKind` — a `(str, Enum)` with members `RATE_LIMIT`, `SERVER_ERROR`, `TIMEOUT`, `AUTH`, `NOT_FOUND`, `CLIENT_ERROR`, `BAD_RESPONSE`, `UNKNOWN` (values are the lowercase names, e.g. `ErrorKind.RATE_LIMIT.value == "rate_limit"`).
  - `base.AgentError(message, *, kind=ErrorKind.UNKNOWN, retry_after=None)` — `.kind: ErrorKind`, `.retry_after: float | None`, `.retryable: bool` (read-only property).

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_error.py`:

```python
import pytest

from debatelab.agents.base import AgentError, ErrorKind


def test_bare_agent_error_still_constructs_and_is_retryable():
    """Every existing raise site passes a message and nothing else."""
    err = AgentError("claude: exit 1: boom")
    assert str(err) == "claude: exit 1: boom"
    assert err.kind is ErrorKind.UNKNOWN
    assert err.retry_after is None
    assert err.retryable is True


def test_transient_kinds_are_retryable():
    for kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER_ERROR,
                 ErrorKind.TIMEOUT, ErrorKind.UNKNOWN):
        assert AgentError("x", kind=kind).retryable is True, kind


def test_permanent_kinds_are_not_retryable():
    """Sleeping 1s then 2s to re-run a binary that does not exist is waste,
    and repeating a request the server called malformed cannot help."""
    for kind in (ErrorKind.AUTH, ErrorKind.NOT_FOUND,
                 ErrorKind.CLIENT_ERROR, ErrorKind.BAD_RESPONSE):
        assert AgentError("x", kind=kind).retryable is False, kind


def test_retry_after_is_carried_when_the_server_supplied_one():
    err = AgentError("x", kind=ErrorKind.RATE_LIMIT, retry_after=5.0)
    assert err.retry_after == 5.0


def test_kind_values_are_stable_strings_for_the_transcript():
    # Telemetry writes kind.value into JSON; these strings are a wire format.
    assert ErrorKind.RATE_LIMIT.value == "rate_limit"
    assert ErrorKind.NOT_FOUND.value == "not_found"
    assert ErrorKind.BAD_RESPONSE.value == "bad_response"


def test_retryable_is_read_only():
    """One definition of 'worth retrying', derived from kind — never set."""
    with pytest.raises(AttributeError):
        AgentError("x").retryable = True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_agent_error.py -q`
Expected: FAIL — `ImportError: cannot import name 'ErrorKind' from 'debatelab.agents.base'`

- [ ] **Step 3: Implement the structured error**

Replace the whole of `debatelab/agents/base.py`:

```python
"""Minimal agent interface every backend adapter implements."""
from abc import ABC, abstractmethod
from enum import Enum

from . import models


class ErrorKind(str, Enum):
    """Why an agent call failed.

    Classified at the raise site, where the facts (status code, exit code,
    exception type) still exist. The orchestrator only ever reads
    `AgentError.retryable`; it never inspects the message text.
    """

    RATE_LIMIT = "rate_limit"      # 429
    SERVER_ERROR = "server_error"  # 5xx
    TIMEOUT = "timeout"            # the call exceeded its deadline
    AUTH = "auth"                  # 401/403, missing API key
    NOT_FOUND = "not_found"        # the CLI binary is not installed
    CLIENT_ERROR = "client_error"  # other 4xx: the request itself is wrong
    BAD_RESPONSE = "bad_response"  # a 2xx whose shape did not parse
    UNKNOWN = "unknown"            # non-zero CLI exit: cause genuinely unknown


# CLIENT_ERROR is separate from UNKNOWN because they carry opposite verdicts:
# a 400 is the server saying our request is malformed, so repeating it cannot
# help, while a non-zero CLI exit is genuinely unknown and worth another try.
_PERMANENT = (
    ErrorKind.AUTH,
    ErrorKind.NOT_FOUND,
    ErrorKind.CLIENT_ERROR,
    ErrorKind.BAD_RESPONSE,
)


class AgentError(Exception):
    """An agent call failed (bad exit, timeout, HTTP error, missing key)."""

    def __init__(self, message, *, kind=ErrorKind.UNKNOWN, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after  # seconds, only when the server said so

    @property
    def retryable(self) -> bool:
        """Derived, never stored: one definition of 'worth retrying'."""
        return self.kind not in _PERMANENT


class Agent(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        """Send a prompt, return the agent's text reply. Raises AgentError on
        failure. `task` (models.DEEP or models.FAST) lets the backend pick
        the most appropriate model for the work."""
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 164 passed. Nothing else changes: every existing raise site still passes only a message and defaults to `UNKNOWN`.

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/base.py tests/test_agent_error.py
git commit -m "feat: give AgentError a structured kind and retry_after

AgentError was a bare Exception carrying one formatted string, so every
backend flattened what it knew — an HTTP status code, an exit code, three
distinct exception types — into prose at the raise site. The orchestrator
caught it and had nothing left to reason with but the message.

Adds ErrorKind and carries kind/retry_after on the error. retryable is a
derived property so 'worth retrying' has exactly one definition. str(e) is
unchanged, so existing transcripts and message-matching tests still hold."
```

---

### Task 2: Classify CLI subprocess failures

**Files:**
- Modify: `debatelab/agents/cli_agent.py:1-40` (imports and `ask`)
- Test: `tests/test_cli_agent.py:1-8` (imports), append tests

**Interfaces:**
- Consumes: Task 1's `ErrorKind`
- Produces: `CliAgent.ask` raises `AgentError` with `kind` set — `TIMEOUT` on `subprocess.TimeoutExpired`, `NOT_FOUND` on `FileNotFoundError`, `UNKNOWN` on a non-zero exit.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli_agent.py`, replace the import block (lines 1-8):

```python
import subprocess
import sys

import pytest

from debatelab.agents.base import Agent, AgentError, ErrorKind
from debatelab.agents.cli_agent import CliAgent
from debatelab.agents.models import DEEP, FAST
```

Append to `tests/test_cli_agent.py`:

```python
def test_nonzero_exit_is_unknown_and_retryable(tmp_path):
    """A subprocess cannot tell us why it failed, so we cannot prove the
    failure is permanent — retry it."""
    script = make_script(tmp_path, 'echo "boom" >&2; exit 3')
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.UNKNOWN
    assert exc.value.retryable is True


def test_timeout_is_classified_as_timeout_and_retryable(tmp_path):
    script = make_script(tmp_path, "sleep 5")
    agent = CliAgent("stub", [script, "{prompt}"], timeout=1)
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.TIMEOUT
    assert exc.value.retryable is True


def test_missing_binary_is_not_found_and_not_retryable():
    """Regression: backing off 1s then 2s to re-run a binary that does not
    exist is pure waste, and a missing agy/codex is the likeliest failure on
    a fresh checkout."""
    agent = CliAgent("stub", ["/no/such/binary", "{prompt}"])
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.NOT_FOUND
    assert exc.value.retryable is False


def test_cli_errors_never_carry_a_retry_after(tmp_path):
    """A CLI has no Retry-After to give; only HTTP backends can supply one."""
    script = make_script(tmp_path, "exit 1")
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.retry_after is None
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_cli_agent.py -q`
Expected: FAIL — `assert ErrorKind.UNKNOWN is ErrorKind.TIMEOUT` on the timeout test (every raise currently defaults to `UNKNOWN`).

- [ ] **Step 3: Classify at the three raise sites**

In `debatelab/agents/cli_agent.py`, replace the import block (lines 1-5):

```python
"""Adapter for locally installed AI CLIs (claude, codex, agy, ...)."""
import subprocess

from . import models
from .base import Agent, AgentError, ErrorKind
```

Replace `ask` (lines 22-40):

```python
    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        cmd = self._build_command(prompt, task)
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
            # UNKNOWN, not a guess: stderr is unversioned prose with no
            # contract, so we do not mine it for "rate limit". Backing off on
            # everything retryable absorbs throttling without pretending to
            # recognise it. See the spec's "Rejected: sniffing stderr".
            raise AgentError(
                f"{self.name}: exit {proc.returncode}: {proc.stderr.strip()[:500]}",
                kind=ErrorKind.UNKNOWN,
            )
        return proc.stdout.strip()
```

- [ ] **Step 4: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 168 passed.

- [ ] **Step 5: Commit**

```bash
git add debatelab/agents/cli_agent.py tests/test_cli_agent.py
git commit -m "feat: classify CLI failures from exit code and exception type

A missing binary was indistinguishable from a flaky call, so a retry loop
would sleep through a backoff to re-run something that will never exist.
TimeoutExpired and FileNotFoundError are already distinct exceptions and
returncode is already an integer — the facts were there and discarded.

A non-zero exit stays UNKNOWN rather than being mined out of stderr: a
third-party CLI's stderr is unversioned prose, and matching it for 'rate
limit' is the parse_vote mistake one layer down."
```

---

### Task 3: Classify HTTP failures and read `Retry-After`

**Files:**
- Modify: `debatelab/agents/api_agent.py` (imports, add two helpers, `ask`, `_request`)
- Test: `tests/test_api_agent.py:1-51` (imports, `Recorder`, fixture), append tests

**Interfaces:**
- Consumes: Task 1's `ErrorKind`
- Produces:
  - `api_agent._kind_for_status(code: int) -> ErrorKind`
  - `api_agent._retry_after_seconds(headers) -> float | None`
  - `ApiAgent.ask` / `._request` raise `AgentError` with `kind` and, for 429s, `retry_after`.

- [ ] **Step 1: Give the test server settable response headers**

`Retry-After` cannot be tested without a server that sends it. In `tests/test_api_agent.py`, replace the import block (lines 1-9):

```python
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from debatelab.agents import models
from debatelab.agents.api_agent import ApiAgent, DRIVERS, MODEL_LISTS
from debatelab.agents.base import AgentError, ErrorKind
```

Replace the `Recorder` class attributes (lines 12-17) — add `extra_headers`:

```python
class Recorder(BaseHTTPRequestHandler):
    calls = []
    payload = {}
    raw_payload = None
    status = 200
    models_payload = {}
    extra_headers = {}
```

Replace `Recorder._respond` (lines 27-35) to emit them:

```python
    def _respond(self, body, data):
        Recorder.calls.append(
            {"path": self.path, "headers": dict(self.headers), "body": body}
        )
        self.send_response(Recorder.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for key, value in Recorder.extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)
```

Replace the `server` fixture body (lines 41-51) to reset the new attribute:

```python
@pytest.fixture
def server():
    Recorder.calls = []
    Recorder.raw_payload = None
    Recorder.status = 200
    Recorder.models_payload = {}
    Recorder.extra_headers = {}
    srv = HTTPServer(("127.0.0.1", 0), Recorder)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
```

- [ ] **Step 2: Write the failing classification tests**

Append to `tests/test_api_agent.py`:

```python
def make_agent(server, monkeypatch):
    monkeypatch.setenv("TEST_KEY", "sk-test")
    Recorder.payload = {"error": "nope"}
    return ApiAgent("gpt", "openai", "gpt-5", "TEST_KEY", base_url=server)


def test_rate_limit_is_classified_and_carries_retry_after(server, monkeypatch):
    """Regression: the status code was formatted into a string and thrown
    away, so a 429 — the one failure that says exactly how long to wait —
    was indistinguishable from a bad request."""
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    Recorder.extra_headers = {"Retry-After": "5"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retryable is True
    assert exc.value.retry_after == 5.0


def test_rate_limit_without_a_retry_after_header(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retry_after is None


def test_http_date_retry_after_is_ignored_rather_than_parsed(server, monkeypatch):
    """The HTTP-date form is rare, and a clock-skew bug here would sleep for
    hours. Falling back to computed backoff is the safe direction."""
    agent = make_agent(server, monkeypatch)
    Recorder.status = 429
    Recorder.extra_headers = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.RATE_LIMIT
    assert exc.value.retry_after is None


def test_server_errors_are_retryable(server, monkeypatch):
    for status in (500, 502, 503, 504):
        agent = make_agent(server, monkeypatch)
        Recorder.status = status
        with pytest.raises(AgentError) as exc:
            agent.ask("hello")
        assert exc.value.kind is ErrorKind.SERVER_ERROR, status
        assert exc.value.retryable is True, status


def test_auth_failures_are_not_retryable(server, monkeypatch):
    for status in (401, 403):
        agent = make_agent(server, monkeypatch)
        Recorder.status = status
        with pytest.raises(AgentError) as exc:
            agent.ask("hello")
        assert exc.value.kind is ErrorKind.AUTH, status
        assert exc.value.retryable is False, status


def test_other_client_errors_are_not_retryable(server, monkeypatch):
    """A 400 is the server telling us the request is malformed; repeating it
    verbatim cannot help."""
    agent = make_agent(server, monkeypatch)
    Recorder.status = 400
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.CLIENT_ERROR
    assert exc.value.retryable is False


def test_unparseable_body_is_bad_response_and_not_retryable(server, monkeypatch):
    """A 200 we cannot parse is a driver bug or an API change; retrying costs
    full price for the same unparseable body."""
    agent = make_agent(server, monkeypatch)
    Recorder.raw_payload = b"this is not json"
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.BAD_RESPONSE
    assert exc.value.retryable is False


def test_unexpected_json_shape_is_bad_response(server, monkeypatch):
    agent = make_agent(server, monkeypatch)
    Recorder.payload = {"unexpected": "shape"}
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.BAD_RESPONSE
    assert exc.value.retryable is False


def test_missing_api_key_is_auth_and_not_retryable(monkeypatch):
    monkeypatch.delenv("TEST_KEY", raising=False)
    agent = ApiAgent("gpt", "openai", "gpt-5", "TEST_KEY")
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.AUTH
    assert exc.value.retryable is False


def test_unreachable_host_is_timeout_and_retryable(monkeypatch):
    """A connection that never lands is a URLError, not an HTTPError."""
    monkeypatch.setenv("TEST_KEY", "sk-test")
    agent = ApiAgent(
        "gpt", "openai", "gpt-5", "TEST_KEY",
        base_url="http://127.0.0.1:1",
    )
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.TIMEOUT
    assert exc.value.retryable is True
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_api_agent.py -q`
Expected: FAIL — `assert ErrorKind.UNKNOWN is ErrorKind.RATE_LIMIT`.

- [ ] **Step 4: Implement classification**

In `debatelab/agents/api_agent.py`, replace the import block (lines 1-9):

```python
"""Adapter for HTTP LLM APIs. Three thin drivers, no provider SDKs."""
import json
import os
import urllib.error
import urllib.request

from . import models
from .base import Agent, AgentError, ErrorKind
```

Add these two helpers immediately after the import block, above `_openai_request`:

```python
_STATUS_KINDS = {
    429: ErrorKind.RATE_LIMIT,
    401: ErrorKind.AUTH,
    403: ErrorKind.AUTH,
}


def _kind_for_status(code: int) -> ErrorKind:
    """Map an HTTP status to a kind. The code is a fact, unlike a CLI's
    stderr, so this backend gets to classify precisely."""
    if code in _STATUS_KINDS:
        return _STATUS_KINDS[code]
    if 500 <= code < 600:
        return ErrorKind.SERVER_ERROR
    if 400 <= code < 500:
        return ErrorKind.CLIENT_ERROR
    return ErrorKind.UNKNOWN


def _retry_after_seconds(headers) -> float | None:
    """Read Retry-After, integer-seconds form only.

    The HTTP-date form is accepted and ignored rather than parsed: it is
    rare, and a clock-skew bug here would sleep for hours. Falling back to
    computed backoff is the safe direction to err.
    """
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(int(str(raw).strip()))
    except ValueError:
        return None
```

Replace `ask` (lines 102-114) so the two non-HTTP failures classify:

```python
    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        api_key = os.environ.get(self.api_key_env, "")
        if not api_key:
            raise AgentError(
                f"{self.name}: env var {self.api_key_env} is not set",
                kind=ErrorKind.AUTH,
            )
        build, parse = DRIVERS[self.provider]
        url, headers, body = build(
            self._model_for(task, api_key), self.base_url, api_key, prompt
        )
        data = self._request(url, headers, body)
        try:
            return parse(data).strip()
        except (KeyError, IndexError, TypeError, AttributeError) as e:
            raise AgentError(
                f"{self.name}: unexpected response shape: {e!r}",
                kind=ErrorKind.BAD_RESPONSE,
            )
```

Replace the `except`/`json.loads` block at the end of `_request` (lines 139-149):

```python
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw_response = resp.read().decode()
        except urllib.error.HTTPError as e:
            # HTTPError is a subclass of URLError, so it must be caught first.
            raise AgentError(
                f"{self.name}: HTTP {e.code}: {e.read().decode()[:500]}",
                kind=_kind_for_status(e.code),
                retry_after=_retry_after_seconds(e.headers),
            )
        except (urllib.error.URLError, TimeoutError) as e:
            raise AgentError(
                f"{self.name}: request failed: {e}",
                kind=ErrorKind.TIMEOUT,
            )
        try:
            return json.loads(raw_response)
        except json.JSONDecodeError as e:
            raise AgentError(
                f"{self.name}: unexpected response shape: {e!r}",
                kind=ErrorKind.BAD_RESPONSE,
            )
```

Also replace the two `AgentError` raises inside `_model_for` (lines 126, 128) so a broken model list is not retried forever:

```python
            except (KeyError, TypeError, AttributeError) as e:
                raise AgentError(
                    f"{self.name}: unexpected model list shape: {e!r}",
                    kind=ErrorKind.BAD_RESPONSE,
                )
            if not available:
                raise AgentError(
                    f"{self.name}: provider lists no models",
                    kind=ErrorKind.BAD_RESPONSE,
                )
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 179 passed.

- [ ] **Step 6: Commit**

```bash
git add debatelab/agents/api_agent.py tests/test_api_agent.py
git commit -m "feat: classify HTTP failures and honor Retry-After

api_agent had e.code — an unambiguous integer — and formatted it into a
message string, so a 429 that says exactly how long to wait was
indistinguishable from a 400 that will never succeed.

Maps status to kind, splitting retryable (429, 5xx, connection failures)
from permanent (401/403, other 4xx, unparseable bodies). Retry-After is
read in its integer-seconds form only; the HTTP-date form is ignored
rather than parsed, since a clock-skew bug would sleep for hours."
```

---

### Task 4: Pure backoff module

**Files:**
- Create: `debatelab/retry.py`
- Test: `tests/test_retry.py` (create)

**Interfaces:**
- Consumes: Task 1's `AgentError`/`ErrorKind`
- Produces:
  - `retry.DEFAULT_MAX_ATTEMPTS = 3`, `retry.DEFAULT_BASE_DELAY = 1.0`, `retry.DEFAULT_CAP = 30.0`
  - `retry.backoff_delay(retry_index: int, rng, base=..., cap=...) -> float`
  - `retry.call_with_retry(fn, *, rng, sleep, on_attempt=None, max_attempts=..., base=..., cap=...)` — returns `fn()`'s value; re-raises the last `AgentError`. `on_attempt(attempt: int, duration_ms: int, error: AgentError | None)` fires once per attempt.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_retry.py`:

```python
import random

import pytest

from debatelab import retry
from debatelab.agents.base import AgentError, ErrorKind


class Clock:
    """Records requested sleeps instead of performing them."""

    def __init__(self):
        self.slept = []

    def __call__(self, seconds):
        self.slept.append(seconds)


def test_backoff_delay_stays_within_zero_and_the_cap():
    rng = random.Random(0)
    for retry_index in range(10):
        delay = retry.backoff_delay(retry_index, rng, base=1.0, cap=5.0)
        assert 0.0 <= delay <= 5.0


def test_backoff_delay_ceiling_doubles_per_retry():
    # The bound is 1, 2, 4; sample enough draws to see each ceiling.
    rng = random.Random(1)
    ceilings = [
        max(retry.backoff_delay(i, rng, base=1.0, cap=30.0) for _ in range(300))
        for i in range(3)
    ]
    assert ceilings[0] < ceilings[1] < ceilings[2]


def test_backoff_delay_is_reproducible_for_a_given_rng_state():
    left = [retry.backoff_delay(i, random.Random(7)) for i in range(3)]
    right = [retry.backoff_delay(i, random.Random(7)) for i in range(3)]
    assert left == right


def test_different_rng_states_draw_different_delays():
    """Anti-lockstep: the roster is retried concurrently, so two agents
    drawing identical delays would re-collide at every step."""
    left = [retry.backoff_delay(1, random.Random(1)) for _ in range(5)]
    right = [retry.backoff_delay(1, random.Random(2)) for _ in range(5)]
    assert left != right


def test_success_on_the_first_attempt_never_sleeps():
    clock = Clock()
    result = retry.call_with_retry(
        lambda: "fine", rng=random.Random(0), sleep=clock
    )
    assert result == "fine"
    assert clock.slept == []


def test_a_transient_failure_is_retried_then_succeeds():
    clock = Clock()
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("throttled", kind=ErrorKind.RATE_LIMIT)
        return "recovered"

    result = retry.call_with_retry(flaky, rng=random.Random(0), sleep=clock)
    assert result == "recovered"
    assert len(calls) == 2
    assert len(clock.slept) == 1


def test_a_permanent_failure_is_not_retried_and_never_sleeps():
    """The whole point of classifying: backing off to re-run a binary that
    does not exist is pure waste."""
    clock = Clock()
    calls = []

    def missing():
        calls.append(1)
        raise AgentError("command not found: agy", kind=ErrorKind.NOT_FOUND)

    with pytest.raises(AgentError, match="command not found"):
        retry.call_with_retry(missing, rng=random.Random(0), sleep=clock)
    assert calls == [1]
    assert clock.slept == []


def test_exhaustion_reraises_the_last_error():
    clock = Clock()
    calls = []

    def always_fails():
        calls.append(1)
        raise AgentError(f"failure {len(calls)}", kind=ErrorKind.TIMEOUT)

    with pytest.raises(AgentError, match="failure 3"):
        retry.call_with_retry(always_fails, rng=random.Random(0), sleep=clock)
    assert len(calls) == 3          # DEFAULT_MAX_ATTEMPTS
    assert len(clock.slept) == 2    # one fewer sleep than attempts


def test_retry_after_overrides_the_computed_backoff():
    clock = Clock()
    calls = []

    def throttled():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("429", kind=ErrorKind.RATE_LIMIT, retry_after=4.0)
        return "ok"

    retry.call_with_retry(throttled, rng=random.Random(0), sleep=clock)
    assert clock.slept == [4.0]


def test_retry_after_is_clamped_to_the_cap():
    """A server asking for 300s gets 30s and one more attempt, rather than
    stalling a debate for five minutes."""
    clock = Clock()
    calls = []

    def throttled():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("429", kind=ErrorKind.RATE_LIMIT, retry_after=300.0)
        return "ok"

    retry.call_with_retry(throttled, rng=random.Random(0), sleep=clock)
    assert clock.slept == [retry.DEFAULT_CAP]


def test_default_delays_never_exceed_three_seconds_in_total():
    """Documents the actual default budget: uniform(0,1) then uniform(0,2)."""
    clock = Clock()

    def always_fails():
        raise AgentError("nope", kind=ErrorKind.TIMEOUT)

    with pytest.raises(AgentError):
        retry.call_with_retry(always_fails, rng=random.Random(3), sleep=clock)
    assert sum(clock.slept) <= 3.0


def test_on_attempt_fires_for_every_attempt_with_the_outcome():
    clock = Clock()
    seen = []
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("boom", kind=ErrorKind.SERVER_ERROR)
        return "ok"

    retry.call_with_retry(
        flaky,
        rng=random.Random(0),
        sleep=clock,
        on_attempt=lambda attempt, ms, err: seen.append((attempt, err)),
    )
    assert [attempt for attempt, _ in seen] == [1, 2]
    assert seen[0][1].kind is ErrorKind.SERVER_ERROR
    assert seen[1][1] is None


def test_on_attempt_reports_a_non_negative_duration():
    seen = []
    retry.call_with_retry(
        lambda: "ok",
        rng=random.Random(0),
        sleep=Clock(),
        on_attempt=lambda attempt, ms, err: seen.append(ms),
    )
    assert seen == [seen[0]]
    assert isinstance(seen[0], int)
    assert seen[0] >= 0


def test_non_agent_errors_are_not_swallowed():
    """Only AgentError is a retry signal; a bug must surface immediately."""
    clock = Clock()
    with pytest.raises(ZeroDivisionError):
        retry.call_with_retry(
            lambda: 1 / 0, rng=random.Random(0), sleep=clock
        )
    assert clock.slept == []
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_retry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'debatelab.retry'`

- [ ] **Step 3: Implement the module**

Create `debatelab/retry.py`:

```python
"""Retry pacing for agent calls: full-jitter exponential backoff.

Pure in the sense that matters here: no files, no network, no knowledge of
debates or the store. The pacing clock and the RNG are injected, so the
suite can assert on delay *sequences* without ever sleeping.

Two dependencies are deliberate rather than accidental: it imports
AgentError to read `.retryable`, and it uses time.monotonic to measure how
long an attempt took. Measuring elapsed time is not the same as controlling
pacing — telemetry needs a duration, and nothing is gained by faking it.
"""
import time

from .agents.base import AgentError

DEFAULT_MAX_ATTEMPTS = 3  # the original call plus two retries
DEFAULT_BASE_DELAY = 1.0  # seconds
DEFAULT_CAP = 30.0  # seconds


def backoff_delay(retry_index, rng, base=DEFAULT_BASE_DELAY, cap=DEFAULT_CAP):
    """Full jitter: uniform(0, min(cap, base * 2**retry_index)).

    `retry_index` is 0-based and counts retries, not attempts: the delay
    before the first retry is index 0, and attempt 1 never sleeps.

    Full jitter rather than plain exponential because the orchestrator fans
    the whole roster out concurrently. If a shared limit throttles every
    agent at the same instant, undithered backoff retries them in lockstep
    at t+1s, t+2s, t+4s — re-colliding at every step, which is the pile-up
    the backoff exists to prevent. Jitter decorrelates them.

    At the defaults the cap is unreachable here (min(30, 1) and min(30, 2));
    it earns its place clamping Retry-After in call_with_retry, and keeps
    this correct if max_attempts is ever raised.
    """
    return rng.uniform(0, min(cap, base * (2 ** retry_index)))


def call_with_retry(fn, *, rng, sleep, on_attempt=None,
                    max_attempts=DEFAULT_MAX_ATTEMPTS,
                    base=DEFAULT_BASE_DELAY, cap=DEFAULT_CAP):
    """Call fn() until it returns or its failures are not worth retrying.

    Stops on the first non-retryable AgentError, or when attempts run out;
    either way the last AgentError propagates. Anything that is not an
    AgentError is a bug rather than a transient failure and is never caught.

    on_attempt(attempt, duration_ms, error) fires once per attempt, with
    error=None on success. It is the telemetry hook; it must not raise.
    """
    for retry_index in range(max_attempts):
        attempt = retry_index + 1
        started = time.monotonic()
        try:
            result = fn()
        except AgentError as e:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if on_attempt is not None:
                on_attempt(attempt, elapsed_ms, e)
            if not e.retryable or attempt == max_attempts:
                raise
            if e.retry_after is not None:
                delay = min(e.retry_after, cap)
            else:
                delay = backoff_delay(retry_index, rng, base, cap)
            sleep(delay)
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if on_attempt is not None:
                on_attempt(attempt, elapsed_ms, None)
            return result
```

- [ ] **Step 4: Run to verify the retry tests pass**

Run: `.venv/bin/python -m pytest tests/test_retry.py -q`
Expected: PASS

- [ ] **Step 5: Verify the module stayed pure**

Run: `.venv/bin/python -c "import ast,sys; tree=ast.parse(open('debatelab/retry.py').read()); mods=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]+[a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]; banned=[m for m in mods if any(b in m for b in ('store','orchestrator','prompts','protocol'))]; print('BANNED:',banned) or sys.exit(1 if banned else 0)"`
Expected: `BANNED: []` and exit 0.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 193 passed.

- [ ] **Step 7: Commit**

```bash
git add debatelab/retry.py tests/test_retry.py
git commit -m "feat: add pure full-jitter backoff module

Full jitter rather than plain exponential because the orchestrator fans the
roster out concurrently: if a shared limit throttles every agent at once,
undithered backoff retries them in lockstep and re-collides at every step.

The pacing clock and RNG are injected, so the suite asserts on delay
sequences without sleeping. Retry-After is clamped to the cap — a server
asking for 300s gets 30s and one more attempt rather than stalling a run."
```

---

### Task 5: Route the orchestrator's calls through the retry loop

**Files:**
- Modify: `debatelab/orchestrator.py:1-23` (imports, `__init__`), `:105-115` (`_reask`), `:117-131` (`_fanout.call`)
- Modify: `tests/conftest.py` (add the autouse fixture)
- Test: `tests/test_orchestrator.py:1-10` (imports), append tests

**Interfaces:**
- Consumes: Task 4's `retry.call_with_retry`, Task 1's `ErrorKind`
- Produces:
  - `orchestrator.DEFAULT_SLEEP` — module-level indirection for `time.sleep`, so tests can neutralize backoff without patching the global `time` module.
  - `Orchestrator(store, agents, progress=..., sleep=None, rng=None)` — **two new optional parameters**.

- [ ] **Step 1: Add the autouse fixture that keeps the suite fast**

Without this, every test with a failing agent sleeps through real backoffs. `MockAgent` raises `AgentError` once its script is exhausted (`conftest.py:18-19`), and `test_two_accepts_of_a_five_agent_roster_is_not_consensus` alone has three always-failing agents across four phases.

Replace the import block of `tests/conftest.py` (lines 1-2):

```python
import pytest

from debatelab import orchestrator
from debatelab.agents import models
from debatelab.agents.base import Agent, AgentError
```

Append to `tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retry backoff must never cost the suite wall-clock time.

    Patches the orchestrator's own indirection rather than the global
    time.sleep, so nothing else in the process (threads, HTTP test servers)
    is affected. Tests that assert on delays pass their own recording fake
    to Orchestrator(sleep=...) and ignore this.
    """
    monkeypatch.setattr(orchestrator, "DEFAULT_SLEEP", lambda _seconds: None)
```

- [ ] **Step 2: Write the failing tests**

In `tests/test_orchestrator.py`, replace the import block (lines 1-10):

```python
import pytest

from debatelab.agents import models
from debatelab.agents.base import Agent, AgentError, ErrorKind
from debatelab import prompts, protocol
from debatelab.orchestrator import Orchestrator
from debatelab.store import DebateStore

from .conftest import MockAgent
```

Append to `tests/test_orchestrator.py`:

```python
class DeadAgent(Agent):
    """Fails every call the way an uninstalled CLI binary fails."""

    def __init__(self, name):
        super().__init__(name)
        self.calls = 0

    def ask(self, prompt, task=models.DEEP):
        self.calls += 1
        raise AgentError(
            f"{self.name}: command not found: agy", kind=ErrorKind.NOT_FOUND
        )


def test_a_transient_failure_is_retried_and_costs_no_vote(tmp_path):
    """Regression: quorum counts against the recorded roster, so an agent
    dropped by a transient failure is a real abstention — two of them turn a
    legitimate 3-0 consensus into no_consensus. The retry must absorb it."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 429: slow down", kind=ErrorKind.RATE_LIMIT),
            "proposal from c",
            "critique from c",
            "revised proposal from c",
            "NOMINATE: a\nbest one",
            "VOTE: accept\nagreed",
        ]),
    ]
    status = Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["abstained"] == []
    assert sorted(state["votes"]) == ["a", "b", "c"]
    assert status == "awaiting_human"


def test_a_permanent_failure_is_never_retried(tmp_path):
    """Sleeping through a backoff to re-run a binary that does not exist is
    pure waste.

    Five fanouts, one call each, not fifteen: propose, critique and revise
    are one each, and _phase_vote fans out twice (nominate, then vote).
    """
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    dead = DeadAgent("c")
    slept = []
    Orchestrator(
        store, [happy_agent("a"), happy_agent("b"), dead], sleep=slept.append
    ).run(did, max_rounds=1)
    assert dead.calls == 5
    assert slept == []


def test_a_permanent_failure_still_abstains(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b"), DeadAgent("c")]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert "c" in state["abstained"]
    assert "c" not in state["votes"]


def test_exhausted_retries_still_abstain(tmp_path):
    """Retrying changes how often an agent abstains, never what abstaining
    means: the quorum still measures against the recorded roster."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b"), MockAgent("c", [])]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert "c" in state["abstained"]
    assert state["roster"] == ["a", "b", "c"]


def test_reask_retries_a_transient_failure_instead_of_abstaining(tmp_path):
    """Regression: _reask had no retry at all, so a rate-limited re-ask
    abstained on the first failure — the same defect one level down."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        MockAgent("a", [
            "prop a", "crit a", "rev a", "NOMINATE: b",
            "I cannot accept this",                       # unparseable
            AgentError("a: HTTP 503", kind=ErrorKind.SERVER_ERROR),  # re-ask 1
            "VOTE: reject\nnow parseable",                # re-ask 2
        ]),
        MockAgent("b", [
            "prop b", "crit b", "rev b", "NOMINATE: a", "VOTE: accept",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    state = store.read_state(did)
    assert state["votes"]["a"]["vote"] == "reject"
    assert state["abstained"] == []


def test_backoff_delays_are_drawn_from_the_injected_rng(tmp_path):
    """Reproducible in tests, unseeded in production."""
    import random

    store = make_store(tmp_path)
    did = store.create("T", "problem")
    slept = []
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 503", kind=ErrorKind.SERVER_ERROR),
            "proposal from c", "critique from c", "revised proposal from c",
            "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        ]),
    ]
    Orchestrator(
        store, agents, sleep=slept.append, rng=random.Random(0)
    ).run(did, max_rounds=1)
    assert len(slept) == 1
    assert slept == [random.Random(0).uniform(0, 1.0)]
```

- [ ] **Step 3: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -q`
Expected: FAIL — `TypeError: Orchestrator.__init__() got an unexpected keyword argument 'sleep'`

- [ ] **Step 4: Inject the clock and RNG**

In `debatelab/orchestrator.py`, replace the import block and `__init__` (lines 1-23):

```python
"""Runs a debate: fans out phase prompts to all agents, applies the protocol,
checkpoints state after every phase so interrupted runs resume."""
import concurrent.futures as cf
import random
import time
from fractions import Fraction

from . import prompts, protocol, retry
from .agents import models
from .agents.base import AgentError
from .store import render_summary

# Module-level indirection so tests can neutralize backoff without patching
# the global time module. Resolved in __init__, never as a default argument:
# defaults bind once at definition and would freeze the real time.sleep
# before any test could reach it.
DEFAULT_SLEEP = time.sleep


class DebateHalted(Exception):
    """Too few agents responded to continue the debate."""


class Orchestrator:
    def __init__(self, store, agents, progress=lambda msg: None,
                 sleep=None, rng=None):
        if len(agents) < 2:
            raise ValueError("a debate needs at least 2 agents")
        self.store = store
        self.agents = {a.name: a for a in agents}
        self.order = [a.name for a in agents]
        self.progress = progress
        self.sleep = sleep or DEFAULT_SLEEP
        # Unseeded on purpose, unlike protocol.select_candidate: two agents
        # drawing identical delays is the exact collision jitter exists to
        # break, and retry timing has no bearing on the recorded outcome.
        # random.Random.random() is a single C call, atomic under the GIL, so
        # one instance shared across the fanout threads is safe.
        self.rng = rng or random.Random()
```

- [ ] **Step 5: Route `_fanout` and `_reask` through the retry loop**

Replace `_reask` (lines 105-115):

```python
    def _reask(self, name, prompt, parse, required, task):
        """Ask one agent again after an unparseable reply.

        Returns (value, text); (None, None) when the agent errors out.
        Re-asks run serially because they are rare, cheap FAST requests.
        """
        try:
            text = retry.call_with_retry(
                lambda: self.agents[name].ask(
                    prompts.reask(prompt, required), task
                ),
                rng=self.rng,
                sleep=self.sleep,
            )
        except AgentError:
            return None, None
        return parse(text), text
```

Replace the `call` closure inside `_fanout` (lines 121-128) — the docstring changes too:

```python
        """Ask every agent concurrently, retrying transient failures; an
        agent whose retries are exhausted records an abstention. Raises
        DebateHalted if fewer than 2 responded."""
        results = {}

        def call(name):
            prompt = prompt_for(name)
            return retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
            )
```

- [ ] **Step 6: Rename the `retry` locals in `_phase_vote`**

`_phase_vote` has two locals named `retry` (`nominee, retry = self._reask(...)` and `verdict, retry = self._reask(...)`, each followed by `if retry is not None: text = retry`). `retry` is now an imported module. The locals shadow it only inside `_phase_vote`, which never calls the module — so nothing breaks today. It is a trap for whoever adds a retry call there next, and Task 6 needs the name anyway.

Rename both occurrences and their follow-up lines, so each pair reads:

```python
                if retry_text is not None:
                    text = retry_text
```

with the assignments becoming `nominee, retry_text = self._reask(...)` and `verdict, retry_text = self._reask(...)`.

- [ ] **Step 7: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 199 passed. Existing tests keep their behavior: `MockAgent` with an exhausted script raises `AgentError` on all three attempts and still abstains.

- [ ] **Step 8: Verify the suite did not get slower**

Run: `.venv/bin/python -m pytest -q --durations=5`
Expected: total runtime still single-digit seconds. If it jumped to minutes, the autouse fixture is not covering a path — do not proceed.

- [ ] **Step 9: Commit**

```bash
git add debatelab/orchestrator.py tests/conftest.py tests/test_orchestrator.py
git commit -m "fix: pace agent retries with backoff instead of retrying instantly

The retry was a bare double-call with no delay, which is worse than not
retrying for the failure it exists to absorb: it spends the retry at the
moment it is most certain to fail, then abstains. Quorum over the recorded
roster made that expensive — a dropped agent is now a real abstention, so
two throttled agents turn a legitimate consensus into no_consensus.

_reask gains retry it never had, and permanent failures skip the backoff
entirely. Clock and RNG are injected; an autouse fixture neutralizes the
clock so the suite never sleeps."
```

---

### Task 6: Record every attempt as an `agent_call` event

**Files:**
- Modify: `debatelab/orchestrator.py` (add `_record_call`, pass `on_attempt` at both call sites)
- Test: `tests/test_orchestrator.py` (append), `tests/test_store.py` (append)

**Interfaces:**
- Consumes: Task 5's wiring, Task 4's `on_attempt` hook
- Produces: `Orchestrator._record_call(debate_id, state, phase, name, task) -> callable` — builds the `on_attempt` callback that appends one `agent_call` event per attempt.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_orchestrator.py`:

```python
def test_every_attempt_is_recorded_with_its_outcome(tmp_path):
    """Regression: a failed call left only {'type': 'abstained'} with a
    flattened message — no duration, no attempt count, no way to learn why
    an agent dropped out of a real debate."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [
        happy_agent("a"),
        happy_agent("b"),
        MockAgent("c", [
            AgentError("c: HTTP 429: slow down", kind=ErrorKind.RATE_LIMIT),
            "proposal from c", "critique from c", "revised proposal from c",
            "NOMINATE: a\nbest one", "VOTE: accept\nagreed",
        ]),
    ]
    Orchestrator(store, agents).run(did, max_rounds=1)
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["agent"] == "c"
        and e["phase"] == "propose"
    ]
    assert [e["attempt"] for e in calls] == [1, 2]
    assert [e["ok"] for e in calls] == [False, True]
    assert calls[0]["kind"] == "rate_limit"
    assert "HTTP 429" in calls[0]["content"]
    assert "kind" not in calls[1]


def test_agent_call_events_carry_phase_task_and_duration(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    calls = [e for e in store.read_events(did) if e["type"] == "agent_call"]
    assert calls, "expected an agent_call per attempt"
    assert all(isinstance(e["duration_ms"], int) for e in calls)
    assert all(e["duration_ms"] >= 0 for e in calls)
    assert {e["phase"] for e in calls} == {
        "propose", "critique", "revise", "vote"
    }
    # vote covers both the nominate and vote fanouts, both FAST.
    assert {e["task"] for e in calls} == {models.DEEP, models.FAST}


def test_agent_call_events_never_claim_a_token_count(tmp_path):
    """A subprocess reports no usage; a field populated for API agents and
    null for CLI agents would invite the wrong inference from a tally."""
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    Orchestrator(store, [happy_agent("a"), happy_agent("b")]).run(
        did, max_rounds=1
    )
    calls = [e for e in store.read_events(did) if e["type"] == "agent_call"]
    assert all("tokens" not in e for e in calls)


def test_a_permanent_failure_records_exactly_one_attempt(tmp_path):
    store = make_store(tmp_path)
    did = store.create("T", "problem")
    agents = [happy_agent("a"), happy_agent("b"), DeadAgent("c")]
    Orchestrator(store, agents).run(did, max_rounds=1)
    calls = [
        e for e in store.read_events(did)
        if e["type"] == "agent_call" and e["agent"] == "c"
        and e["phase"] == "propose"
    ]
    assert [e["attempt"] for e in calls] == [1]
    assert calls[0]["kind"] == "not_found"
```

Telemetry is the first thing to append events from inside the fanout's worker threads — every existing append happens on the main thread after `fut.result()`. Append to `tests/test_store.py`:

```python
def test_concurrent_appends_never_interleave(tmp_path):
    """Telemetry appends from the fanout's worker threads. Small O_APPEND
    writes are atomic, which is what makes this safe — events must stay well
    under the 8KB buffer for that to hold.
    """
    import concurrent.futures as cf

    store = DebateStore(tmp_path / "debates")
    did = store.create("T", "problem")

    def append(i):
        store.append_event(did, {
            "round": 1, "phase": "propose", "agent": f"agent-{i}",
            "type": "agent_call", "content": "x" * 500,
        })

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(append, range(200)))

    events = store.read_events(did)          # raises if any line is torn
    assert len(events) == 200
    assert all(e["content"] == "x" * 500 for e in events)
```

- [ ] **Step 2: Run to verify failure**

Run: `.venv/bin/python -m pytest tests/test_orchestrator.py -q`
Expected: FAIL — `assert [] == [1, 2]`; no `agent_call` events exist yet.

- [ ] **Step 3: Implement the telemetry hook**

In `debatelab/orchestrator.py`, add `_record_call` directly after `_abstain` (above `_reask`):

```python
    def _record_call(self, debate_id, state, phase, name, task):
        """Build the on_attempt hook that logs one agent_call per attempt.

        Every attempt, not only the failures: the transcript is meant to
        become the single source of truth (replay), and it is what answers
        "which agent is slow". Events stay small — stderr is already capped
        at 500 chars upstream — which is what keeps the concurrent appends
        from the fanout threads atomic.

        No `model` or `tokens` field: a subprocess reports no usage, and
        reading the resolved model would need either network I/O or mutable
        cross-thread state on a shared agent. Both are deferred.
        """
        def on_attempt(attempt, duration_ms, error):
            event = {
                "round": state["round"], "phase": phase, "agent": name,
                "type": "agent_call", "task": task, "attempt": attempt,
                "duration_ms": duration_ms, "ok": error is None,
            }
            if error is not None:
                event["kind"] = error.kind.value
                event["content"] = str(error)
            self.store.append_event(debate_id, event)

        return on_attempt
```

Pass it at the `_fanout` call site — replace the `call` closure from Task 5:

```python
        def call(name):
            prompt = prompt_for(name)
            return retry.call_with_retry(
                lambda: self.agents[name].ask(prompt, task),
                rng=self.rng,
                sleep=self.sleep,
                on_attempt=self._record_call(
                    debate_id, state, phase, name, task
                ),
            )
```

`_reask` needs the phase and debate id it never took. Replace its signature and body:

```python
    def _reask(self, debate_id, state, phase, name, prompt, parse, required,
               task):
        """Ask one agent again after an unparseable reply.

        Returns (value, text); (None, None) when the agent errors out.
        Re-asks run serially because they are rare, cheap FAST requests.
        """
        try:
            text = retry.call_with_retry(
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
        return parse(text), text
```

- [ ] **Step 4: Update the two `_reask` call sites**

Both are in `_phase_vote`, and both already use `retry_text` after Task 5's rename — only the arguments change. The nomination one becomes:

```python
                nominee, retry_text = self._reask(
                    debate_id, state, "vote", name,
                    prompts.nominate_prompt(name, problem, proposals, names),
                    lambda t: prompts.parse_nomination(t, names),
                    prompts.NOMINATE_REQUIRED,
                    models.FAST,
                )
                if retry_text is not None:
                    text = retry_text
```

And the vote one:

```python
                verdict, retry_text = self._reask(
                    debate_id, state, "vote", name,
                    prompts.vote_prompt(name, problem, winner, proposals[winner]),
                    prompts.parse_vote,
                    prompts.VOTE_REQUIRED,
                    models.FAST,
                )
                if retry_text is not None:
                    text = retry_text
```

- [ ] **Step 5: Confirm no `retry` local shadows the module**

Run: `.venv/bin/python -c "import ast; src=open('debatelab/orchestrator.py').read(); tree=ast.parse(src); bad=[n.lineno for n in ast.walk(tree) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Store) and n.id=='retry']; print('shadowing at lines:',bad); assert not bad"`
Expected: `shadowing at lines: []` and exit 0. A leftover local would otherwise surface as `AttributeError: 'str' object has no attribute 'call_with_retry'` only once someone adds a retry call to that function.

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS — 204 passed.

- [ ] **Step 7: Commit**

```bash
git add debatelab/orchestrator.py tests/test_orchestrator.py tests/test_store.py
git commit -m "feat: record every agent call attempt in the transcript

A failed call left only an abstained event with a flattened message: no
duration, no attempt count, no way to tell an uninstalled CLI from an
overloaded API after the fact. There was no evidence on which to tune any
retry policy.

Emits agent_call per attempt with task, attempt, duration_ms, ok and kind.
Every attempt rather than only failures, because the transcript is meant to
become the single source of truth and it is what answers which agent is
slow. No tokens field: a subprocess reports no usage, and a column that is
null for CLI agents invites the wrong inference."
```

---

## Verification

After all six tasks:

```bash
.venv/bin/python -m pytest -q
```

Expected: all green, 204 passed, still single-digit seconds.

Confirm `protocol.py` was never touched — this plan changes how often an agent abstains, never what an abstention means:

```bash
git diff --stat 7d509c0..HEAD -- debatelab/protocol.py
```

Expected: empty output.

Manual end-to-end check that the headline defect is dead:

```bash
.venv/bin/python - <<'PY'
import random
from debatelab import retry
from debatelab.agents.base import AgentError, ErrorKind

# A missing binary is never retried: no sleeping to re-run what cannot exist.
slept = []
try:
    retry.call_with_retry(
        lambda: (_ for _ in ()).throw(
            AgentError("agy: command not found", kind=ErrorKind.NOT_FOUND)
        ),
        rng=random.Random(0), sleep=slept.append,
    )
except AgentError:
    pass
assert slept == [], slept

# A throttled call is retried, paced, and costs no vote.
calls = []
def flaky():
    calls.append(1)
    if len(calls) == 1:
        raise AgentError("HTTP 429", kind=ErrorKind.RATE_LIMIT, retry_after=300)
    return "recovered"

slept = []
assert retry.call_with_retry(
    flaky, rng=random.Random(0), sleep=slept.append
) == "recovered"
assert slept == [retry.DEFAULT_CAP], slept   # 300s clamped to 30s
print("agent reliability verified")
PY
```

Expected: `agent reliability verified`.

## Out of scope

Tracked in the spec's "Deferred" section; do **not** build these here: tunable retry policy (`--max-attempts`, per-agent `retry:` blocks in `agents.yaml`), a longer `base` delay, `model` in telemetry, CLI rate-limit stderr signatures, a phase-level deadline, a circuit breaker across phases, and everything still parked in `2026-07-14-protocol-correctness-design.md`'s deferred roadmap.
