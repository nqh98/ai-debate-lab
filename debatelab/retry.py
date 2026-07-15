"""Retry pacing for agent calls: full-jitter exponential backoff."""
import time

from .agents.base import AgentError

DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_CAP = 30.0


def backoff_delay(retry_index, rng, base=DEFAULT_BASE_DELAY, cap=DEFAULT_CAP):
    """Full jitter: uniform(0, min(cap, base * 2**retry_index))."""
    return rng.uniform(0, min(cap, base * (2 ** retry_index)))


def call_with_retry(
    fn,
    *,
    rng,
    sleep,
    on_attempt=None,
    max_attempts=DEFAULT_MAX_ATTEMPTS,
    base=DEFAULT_BASE_DELAY,
    cap=DEFAULT_CAP,
):
    """Call fn() until it returns or its AgentError is not worth retrying."""
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
                delay = max(0.0, min(e.retry_after, cap))
            else:
                delay = backoff_delay(retry_index, rng, base, cap)
            sleep(delay)
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if on_attempt is not None:
                on_attempt(attempt, elapsed_ms, None)
            return result
