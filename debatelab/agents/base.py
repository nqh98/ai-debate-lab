"""Minimal agent interface every backend adapter implements."""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum

from . import models


class ErrorKind(str, Enum):
    """Why an agent call failed."""

    RATE_LIMIT = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    CLIENT_ERROR = "client_error"
    BAD_RESPONSE = "bad_response"
    UNKNOWN = "unknown"


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
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind not in _PERMANENT


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
    # Overridden per-instance by registry.build_agents; class-level defaults
    # keep every test double honest without boilerplate.
    workspace_attached: bool = False
    stall_after = {"fast": 300, "deep": 900}

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        """Send a prompt, return the agent's reply. Raises AgentError on
        failure. `task` (models.DEEP or models.FAST) lets the backend pick
        the most appropriate model for the work."""
