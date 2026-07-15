import pytest

from debatelab.agents.base import AgentError, ErrorKind


def test_bare_agent_error_still_constructs_and_is_retryable():
    err = AgentError("claude: exit 1: boom")
    assert str(err) == "claude: exit 1: boom"
    assert err.kind is ErrorKind.UNKNOWN
    assert err.retry_after is None
    assert err.retryable is True


def test_transient_kinds_are_retryable():
    for kind in (
        ErrorKind.RATE_LIMIT,
        ErrorKind.SERVER_ERROR,
        ErrorKind.TIMEOUT,
        ErrorKind.UNKNOWN,
    ):
        assert AgentError("x", kind=kind).retryable is True, kind


def test_permanent_kinds_are_not_retryable():
    for kind in (
        ErrorKind.AUTH,
        ErrorKind.NOT_FOUND,
        ErrorKind.CLIENT_ERROR,
        ErrorKind.BAD_RESPONSE,
    ):
        assert AgentError("x", kind=kind).retryable is False, kind


def test_retry_after_is_carried_when_the_server_supplied_one():
    err = AgentError("x", kind=ErrorKind.RATE_LIMIT, retry_after=5.0)
    assert err.retry_after == 5.0


def test_kind_values_are_stable_strings_for_the_transcript():
    assert ErrorKind.RATE_LIMIT.value == "rate_limit"
    assert ErrorKind.NOT_FOUND.value == "not_found"
    assert ErrorKind.BAD_RESPONSE.value == "bad_response"


def test_retryable_is_read_only():
    with pytest.raises(AttributeError):
        AgentError("x").retryable = True
