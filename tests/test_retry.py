import random

import pytest

from debatelab import retry
from debatelab.agents.base import AgentError, ErrorKind


class Clock:
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
    assert len(calls) == 3
    assert len(clock.slept) == 2


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
    clock = Clock()
    calls = []

    def throttled():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("429", kind=ErrorKind.RATE_LIMIT, retry_after=300.0)
        return "ok"

    retry.call_with_retry(throttled, rng=random.Random(0), sleep=clock)
    assert clock.slept == [retry.DEFAULT_CAP]


def test_negative_retry_after_is_clamped_to_zero_defensively():
    clock = Clock()
    calls = []

    def throttled():
        calls.append(1)
        if len(calls) == 1:
            raise AgentError("429", kind=ErrorKind.RATE_LIMIT, retry_after=-1.0)
        return "ok"

    retry.call_with_retry(throttled, rng=random.Random(0), sleep=clock)
    assert clock.slept == [0.0]


def test_default_delays_never_exceed_three_seconds_in_total():
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
    assert isinstance(seen[0], int)
    assert seen[0] >= 0


def test_non_agent_errors_are_not_swallowed():
    clock = Clock()
    with pytest.raises(ZeroDivisionError):
        retry.call_with_retry(lambda: 1 / 0, rng=random.Random(0), sleep=clock)
    assert clock.slept == []
