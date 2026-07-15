# Agent Reliability: Error Classification, Backoff, and Telemetry — Design Spec

Status: proposed
Date: 2026-07-15
Supersedes: nothing
Predecessor: `specs/2026-07-14-protocol-correctness-design.md` ("Deferred roadmap → Reliability")

## Purpose

Make a transient agent failure cost a retry instead of a vote, and make the reason any agent dropped out recoverable from the transcript.

Scope: the call boundary between the orchestrator and the agent backends — `AgentError`'s shape, where failures are classified, how retries are paced, and what each attempt records. No protocol changes, no new phases, no state-shape changes. `protocol.py` is not touched by this spec.

## Motivating defects

One real bug, one architectural flaw, one blind spot.

**Bug — the retry is instant, which is exactly wrong for the failure it exists to absorb.** `orchestrator.py:125-128`:

```python
try:
    return self.agents[name].ask(prompt, task)
except AgentError:
    return self.agents[name].ask(prompt, task)
```

Two attempts, back to back, no delay. For a rate limit this is worse than not retrying: it spends the retry at the moment it is most certain to fail, then abstains. The `_reask` path (`orchestrator.py:111-114`) has no retry at all — a single `AgentError` there returns `(None, None)` and abstains immediately.

**This got materially worse last cycle.** Under the superseded rule, consensus was unanimity among agents that *replied*, so a rate-limited agent silently left the denominator and the debate concluded anyway. `2026-07-14-protocol-correctness-design.md` replaced that with a quorum over the recorded roster — correctly, because the old rule let 2-of-5 report as unanimous. But the denominator is now fixed at run start, so a dropped agent is a real abstention counted against it. Two rate-limited agents on a 3-roster turn a legitimate 3-0 consensus into `no_consensus`. That fails in the safe direction, which is why it shipped, but it moves retry quality onto the critical path for whether debates conclude at all.

**Architectural — every backend knows why it failed, and every backend throws that away.** `AgentError` is a bare `Exception` (`base.py:7-8`) carrying one formatted string. The facts exist at the raise site and are destroyed there:

| Raise site | Fact available | What survives |
|---|---|---|
| `api_agent.py:143` | `e.code` — an integer; 429 is unambiguous | `"claude: HTTP 429: ..."` |
| `api_agent.py:144` | `URLError` vs `TimeoutError` — distinct types | `"claude: request failed: ..."` |
| `cli_agent.py:32` | `subprocess.TimeoutExpired` — a distinct exception | `"claude: timed out after 180s"` |
| `cli_agent.py:34` | `FileNotFoundError` — a distinct exception | `"claude: command not found: claude"` |
| `cli_agent.py:36` | `proc.returncode` — an integer | `"claude: exit 1: ..."` |

The orchestrator catches `AgentError` and has nothing left to reason with. Any classification it attempted would have to re-parse that message — which is the prose-guessing pattern the predecessor spec spent seven tasks removing from `parse_vote`. Classification belongs where the facts still exist.

**Blind spot — a failed call leaves no evidence.** `_fanout` records `{"type": "abstained", "content": str(e)}` and nothing else: no duration, no attempt count, no distinction between "the CLI is not installed" and "the API is overloaded". When an agent drops out of a real debate there is no way to learn why after the fact, and no basis on which to tune any retry policy this spec might invent.

## Design constraint: a subprocess cannot tell you it was rate limited

The default roster is CLI-backed (`claude -p`, `codex exec`, `agy -p`). `CliAgent.ask` is a one-shot `subprocess.run` (`cli_agent.py:22-40`); the only failure signal is an exit code and whatever the tool wrote to stderr. There is no status code, no `Retry-After`, no contract of any kind.

Nothing can change that. So the design does not pretend otherwise: **API backends get precise classification because HTTP gives them facts; CLI backends get honest, coarse classification from the facts they have.**

## Design

### 1. `AgentError` carries structure (`base.py`)

```python
class ErrorKind(str, Enum):
    RATE_LIMIT   = "rate_limit"    # 429
    SERVER_ERROR = "server_error"  # 5xx
    TIMEOUT      = "timeout"       # call exceeded the deadline
    AUTH         = "auth"          # 401/403, missing API key
    NOT_FOUND    = "not_found"     # CLI binary absent
    CLIENT_ERROR = "client_error"  # other 4xx — the request itself is wrong
    BAD_RESPONSE = "bad_response"  # 2xx whose shape did not parse
    UNKNOWN      = "unknown"       # non-zero CLI exit: cause genuinely unknown

_PERMANENT = (ErrorKind.AUTH, ErrorKind.NOT_FOUND,
              ErrorKind.CLIENT_ERROR, ErrorKind.BAD_RESPONSE)


class AgentError(Exception):
    def __init__(self, message, *, kind=ErrorKind.UNKNOWN, retry_after=None):
        super().__init__(message)
        self.kind = kind
        self.retry_after = retry_after   # seconds, only when the server said so

    @property
    def retryable(self) -> bool:
        return self.kind not in _PERMANENT
```

`str(e)` is unchanged, so existing transcript `content` and every current test that matches on the message keep working. The subclass-free design is deliberate: callers branch on `kind`, never on `isinstance`, so adding a kind never forces a call-site change.

`retryable` is a derived property rather than a constructor argument so that "is this worth retrying" has exactly one definition. `BAD_RESPONSE` is not retryable: a 200 whose JSON shape we cannot parse is a driver bug or an API change, and repeating the request produces the same unparseable body at full cost.

### 2. Classification at each raise site

**`api_agent.py`** — map the status code, which is a fact:

| Status | Kind | Retryable |
|---|---|---|
| 429 | `RATE_LIMIT` | yes, honoring `Retry-After` when present |
| 500, 502, 503, 504 | `SERVER_ERROR` | yes |
| 401, 403 | `AUTH` | no |
| other 4xx | `CLIENT_ERROR` | no |
| `TimeoutError` / `URLError` | `TIMEOUT` | yes |
| missing `api_key_env` | `AUTH` | no |
| unparseable 2xx body | `BAD_RESPONSE` | no |

`CLIENT_ERROR` exists rather than reusing `UNKNOWN` because the two carry opposite verdicts. A 400 is the server telling us our request is malformed — repeating it verbatim cannot help. A non-zero CLI exit is genuinely unknown, so we retry it. Collapsing both into `UNKNOWN` would force one of them to be wrong.

`Retry-After` is read from `e.headers` on the `HTTPError`, integer-seconds form only. The HTTP-date form is accepted-and-ignored (falls back to computed backoff) rather than parsed: it is rare, and a clock-skew bug here would sleep for hours.

**`cli_agent.py`** — map the exception type and exit code, which are facts:

| Condition | Kind | Retryable |
|---|---|---|
| `subprocess.TimeoutExpired` | `TIMEOUT` | yes |
| `FileNotFoundError` | `NOT_FOUND` | **no** |
| non-zero `returncode` | `UNKNOWN` | yes |

`NOT_FOUND` not being retryable is the point of classifying at all: sleeping 1s, then 2s, then 4s to re-run a binary that does not exist is pure waste, and it is the single most likely failure on a fresh checkout where `agy` or `codex` was never installed. Everything else from a CLI is retryable because we cannot prove it is not.

### 3. Rejected: sniffing stderr for "rate limit"

The obvious move is to regex `429|rate.?limit|quota|overloaded` out of `proc.stderr` and mark it `RATE_LIMIT`. This spec rejects it, for three reasons:

1. **It is unnecessary.** You do not need to *identify* a rate limit to *fix* the rate-limit bug. Backing off on everything retryable absorbs rate limits, overload, and flaky networks alike. The classification that changes behavior is retryable-vs-permanent, and that is already knowable from facts. A `RATE_LIMIT` label on a CLI error would be a telemetry nicety bought with a guess.
2. **It is the `parse_vote` sin one layer down.** A third-party CLI's stderr is unversioned prose with no contract. `"I cannot accept this"` parsing as accept and `"error: not rate limited"` parsing as a rate limit are the same failure. This codebase has an explicit, hard-won position on guessing from prose.
3. **We have no evidence.** Nobody has recorded what `claude -p` actually prints when throttled. A pattern written today is fitted to an imagined string.

Instead, stderr is captured **verbatim** in telemetry (§5). If real runs show a recognizable rate-limit signature, a later cycle can add the mapping against real evidence rather than imagination. Telemetry first, then tuning — not the reverse.

### 4. Backoff (`debatelab/retry.py`, new)

A new module, pure in the sense that matters: no files, no network, no knowledge of debates or the store. The *pacing* clock and the RNG are injected.

Two dependencies are deliberate rather than sloppy. It imports `AgentError` to read `.retryable` — it cannot pace what it cannot classify. And it calls `time.monotonic` to measure attempt duration for telemetry: measuring elapsed time is not the same as controlling pacing, and faking it would buy nothing, since a test asserting `duration_ms >= 0` needs no injected clock.

```python
DEFAULT_MAX_ATTEMPTS = 3      # the original call plus 2 retries
DEFAULT_BASE_DELAY   = 1.0    # seconds
DEFAULT_CAP          = 30.0   # seconds

def backoff_delay(retry_index, rng, base=DEFAULT_BASE_DELAY, cap=DEFAULT_CAP):
    """Full jitter: uniform(0, min(cap, base * 2**retry_index)).

    `retry_index` is 0-based and counts retries, not attempts: the delay
    *before* the first retry is index 0. Attempt 1 never sleeps.
    """
    return rng.uniform(0, min(cap, base * (2 ** retry_index)))

def call_with_retry(fn, *, rng, sleep, on_attempt=None,
                    max_attempts=DEFAULT_MAX_ATTEMPTS):
    """Call fn() until it returns, a non-retryable AgentError is raised, or
    attempts are exhausted. Re-raises the last AgentError. Reports every
    attempt to on_attempt(attempt, duration_ms, error) for telemetry."""
```

**Full jitter, not plain exponential.** This matters specifically here: `_fanout` calls the whole roster **concurrently** through a `ThreadPoolExecutor` (`orchestrator.py:130-131`). If a shared limit throttles all three agents at the same instant, undithered backoff retries all three in lockstep at t+1s, t+2s, t+4s — re-colliding at every step, which is the thundering herd the backoff was supposed to prevent. Uniform jitter decorrelates them. Equal-jitter and decorrelated-jitter were considered; full jitter is the simplest of the three and the difference between them is not measurable at a roster size of 3.

**The concrete default sequence.** With `max_attempts=3, base=1.0`, a failing call sleeps `uniform(0, 1)` then `uniform(0, 2)` — **at most 3 seconds of added waiting**, three attempts total. This is deliberately modest, and it is worth being plain that 3 seconds may not absorb a real rate limit, which can want tens of seconds. We do not know, because nobody has measured one (§3). Modest defaults plus telemetry beat aggressive defaults plus imagination: the data this cycle produces is what the deferred tunable-policy item will be set from.

**`DEFAULT_CAP` is not reachable by the computed backoff at these defaults** — `min(30, 1×2⁰)` and `min(30, 1×2¹)` are 1 and 2. The cap earns its place as the `Retry-After` clamp below, and keeps the formula correct if `max_attempts` is ever raised. It is not dead config, but it is not doing what the name suggests today, which is worth knowing before someone "fixes" it.

**`retry_after` wins when present, capped.** `min(retry_after, cap)` — a server asking for 300s is answered with 30s and one more attempt, rather than stalling a debate for five minutes. A malicious or buggy `Retry-After` therefore cannot hang a run. This is the one path where a delay can exceed the 3-second budget above, and it only happens when a server explicitly asked for it.

**Determinism.** `rng` and `sleep` are injected. Production passes `random.Random()` and `time.sleep`; tests pass a seeded `Random` and a recording fake, so the suite asserts on the delay *sequence* without sleeping. This keeps the 9-second suite at 9 seconds — a hard constraint, and one that needs more than injection alone to hold (§7).

Unlike `protocol.select_candidate`, this RNG is deliberately **unseeded in production**. Retry timing must not be reproducible: two concurrent agents drawing identical delays is the exact collision jitter exists to break. Retry timing also has no bearing on the recorded outcome, so it costs the transcript's replayability nothing.

**Worst-case wall clock.** The backoff is negligible next to the timeout it retries: 3 × 180s + ≤3s ≈ **9.05 minutes** for a single agent that hard-times-out on every attempt, against ~6 minutes today. The fanout is concurrent, so a phase costs the *slowest* agent, not the sum.

That 50% worsening on the worst path is accepted, not mitigated. The alternative — a phase-level deadline — is a second timeout mechanism layered over the per-call one already in `CliAgent.timeout`, and two interacting timeout systems is how you get a bug that only reproduces at 3am. If retrying timeouts proves to be the wrong trade, the honest fix is a lower `CliAgent.timeout` or a per-kind attempt limit, both of which want the telemetry from this cycle first.

### 5. Telemetry: `agent_call` events

Every attempt appends one event:

```json
{"ts": "...", "round": 1, "phase": "propose", "agent": "claude",
 "type": "agent_call", "task": "deep", "attempt": 1, "duration_ms": 8412,
 "ok": false, "kind": "rate_limit", "content": "claude: HTTP 429: ..."}
```

`ok: true` attempts carry `duration_ms` and omit `kind` and `content`. This fits the established schema (`{ts, round, phase, agent, type, content}`, extra keys allowed) and needs no store change. Every field above is known to the orchestrator at the call site — `phase`, `round`, and `task` from the closure, the rest from `call_with_retry`'s `on_attempt` callback.

**Every attempt, not just failures.** The reliability roadmap's own goal is `replay(events) -> state`, making the checkpoint a cache rather than a second truth. A transcript that records only the calls that failed cannot support that, and cannot answer "which agent is slow" — the question most likely to be asked of this data.

**Volume is a non-issue.** A 3-agent, 5-round debate adds ~60 small events to a transcript that already stores every proposal and critique in full. The events are additive; the viewer ignores unknown types today.

**`tokens` and `model` are both dropped from the roadmap's wish list**, for different reasons.

`tokens` is *impossible* to do honestly. `CliAgent.ask` returns `proc.stdout` and a subprocess reports no usage. A field populated for API agents and silently null for CLI agents invites exactly the wrong inference from a tally.

`model` is *possible but not cheap*, which is the more interesting call. Both backends resolve it internally, but neither exposes it: `CliAgent._model_for(task)` is private, and `ApiAgent._model_for(task, api_key)` needs a key and can itself perform network discovery and raise. Surfacing it means either a public accessor that can do I/O and fail — inside the telemetry path, which must never be the thing that breaks a debate — or a `last_model` attribute mutated on an agent object shared across threads. The first is dangerous, the second is the kind of state that is correct today only because each agent happens to receive exactly one concurrent call per phase.

Neither is worth it for a field nobody has asked a question of yet. Deferred, with a note, rather than smuggled in.

### 6. Wiring (`orchestrator.py`)

Both call sites route through `call_with_retry`, replacing the hand-rolled double-call:

- `_fanout.call()` — loses its `try/except AgentError: <call again>` entirely.
- `_reask()` — gains retry it never had. A rate-limited re-ask currently abstains on the first failure, which is the same defect one level down.

`Orchestrator.__init__` gains `sleep=None` and `rng=None` as injected dependencies, mirroring the existing `progress` callback, resolved in the body:

```python
DEFAULT_SLEEP = time.sleep          # module level

    self.sleep = sleep or DEFAULT_SLEEP
    self.rng = rng or random.Random()
```

Resolved in the body, **not** as default argument values: defaults bind once at definition and would freeze a reference to the real `time.sleep` before any test could reach it.

The indirection through a module-level `DEFAULT_SLEEP` is what lets §7's fixture neutralize backoff by patching one name in one module, instead of monkeypatching the global `time.sleep` and hoping nothing else in the process — the HTTP test server, the thread pool — depended on it.

Nothing else moves. `DebateHalted`, the quorum check, the abstention path, and `protocol.py` are untouched — retries reduce how often abstentions happen without changing what an abstention means.

### 7. Compatibility

- `AgentError(msg)` with no kwargs still constructs, defaulting to `UNKNOWN`/retryable. Existing raises are unaffected.
- **Existing tests stay correct but would start sleeping for real.** `MockAgent` raises `AgentError` once its script is exhausted (`conftest.py:18-19`), so an agent that fails now fails on all 3 attempts and still abstains — `test_agent_error_during_reask_abstains` and the `MockAgent("c", [])` roster in `test_two_accepts_of_a_five_agent_roster_is_not_consensus` keep passing on behavior. The damage is wall clock: three always-failing agents across four phases would sleep through dozens of real backoffs and turn a 9-second suite into a multi-minute one.

  The fix is an autouse fixture in `conftest.py` neutralizing the clock for every test:

  ```python
  @pytest.fixture(autouse=True)
  def no_real_sleep(monkeypatch):
      monkeypatch.setattr(orchestrator, "DEFAULT_SLEEP", lambda _seconds: None)
  ```

  It patches the orchestrator's own indirection rather than the global `time.sleep`, so nothing else in the process is affected — the suite runs a live `HTTPServer` and a thread pool, and neutering the interpreter's clock for all of them to speed up one module is a blast radius with no upside.

  Tests that assert on delays inject a recording fake explicitly via `Orchestrator(sleep=...)` and ignore the fixture; `call_with_retry` takes `sleep` as a required keyword, so its own unit tests can never sleep by accident.
- Transcripts predating this spec simply lack `agent_call` events. Nothing reads them yet.

## Testing

Unit tests, `MockAgent` only, no network, **no real sleeping**:

- **Classification:** each row of both tables in §2. A 429 with `Retry-After: 5` yields `retry_after == 5.0`; an HTTP-date `Retry-After` yields `None` rather than raising; 401 is not retryable; a 400 is not retryable while a non-zero CLI exit is (the `CLIENT_ERROR`/`UNKNOWN` split); `FileNotFoundError` is not retryable.
- **Backoff:** `backoff_delay` never exceeds `cap`; never negative; a seeded RNG gives a reproducible sequence; two different RNG states give different sequences (the anti-lockstep property).
- **`call_with_retry`:** success on attempt 1 sleeps zero times; success on attempt 2 sleeps once; a non-retryable error sleeps zero times and re-raises immediately (the `NOT_FOUND` waste case); exhaustion re-raises the *last* error; `retry_after=300` sleeps `cap`, not 300.
- **Orchestrator:** an agent that fails once then succeeds contributes a vote and produces **no** abstention (the headline regression — this is the false `no_consensus` the spec exists to prevent); an agent failing `NOT_FOUND` abstains without any sleep; `_reask` retries a transient failure instead of abstaining.
- **Telemetry:** a failed-then-successful call emits two `agent_call` events with `attempt` 1 and 2, `ok` false then true; every event carries `duration_ms`; no event carries a `tokens` key.

Integration: extend the mock-agent debate so one agent rate-limits on its first propose attempt, asserting the debate still reaches consensus and the transcript shows the retry.

Suite runtime is itself an assertion: `pytest -q` must stay in single-digit seconds (9s at the time of writing, 158 tests). A regression to minutes means the autouse clock fixture is not covering a path.

## Superseded decisions

From `2026-07-14-ai-debate-lab-design.md` and the code it produced:

- **`orchestrator.py:125-128`**, "one retry per agent, immediately" → retries are now paced with full-jitter exponential backoff and skipped entirely for permanent failures.
- **`base.py:7-8`**, `AgentError` as an opaque message → now carries `kind` and `retry_after`, classified at the raise site.

## Deferred

Not in this spec; each needs its own cycle.

- **Tunable retry policy** (`--max-attempts`, per-agent `retry:` blocks in `agents.yaml`). Deliberately omitted: there is no evidence yet about real limits, and adding tuning knobs before the telemetry that would inform them is backwards. Constants live in `retry.py` until data justifies otherwise. This is also where a longer `base` lands if 3 seconds proves too short (§4).
- **`model` in telemetry.** Needs a safe way to read the resolved model without network I/O or cross-thread mutable state (§5).
- **CLI rate-limit signatures.** Revisit once telemetry shows what throttling actually looks like (§3).
- **Phase-level deadline.** See the wall-clock note in §4.
- **A circuit breaker** across phases — an agent that has failed every call for three rounds is still asked again each round. Real, but it needs the telemetry from this cycle to be designed against.
- Everything still parked in `2026-07-14-protocol-correctness-design.md` → "Deferred roadmap".

## Explicitly rejected

- **Stderr prose matching for rate limits** — see §3.
- **Retry inside each backend's `ask`** — would duplicate the policy across `CliAgent` and `ApiAgent` and put a clock inside the adapters. One pure module, injected once.
- **An `Agent` wrapper class (`RetryingAgent`)** — would make `self.agents[name]` a different object than the one tests construct, for no gain over a function call at the two call sites that need it.
- **`tenacity` / `backoff` libraries** — the project is stdlib + PyYAML by constraint, and full-jitter backoff is four lines.
- **Retrying `BAD_RESPONSE`** — a 2xx we cannot parse is a driver bug; repeating it costs full price for the same unparseable body.
