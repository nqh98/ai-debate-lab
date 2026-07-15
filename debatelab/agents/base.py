"""Minimal agent interface every backend adapter implements."""
from abc import ABC, abstractmethod
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


class Agent(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        """Send a prompt, return the agent's text reply. Raises AgentError on
        failure. `task` (models.DEEP or models.FAST) lets the backend pick
        the most appropriate model for the work."""
