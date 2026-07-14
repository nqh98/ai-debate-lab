"""Minimal agent interface every backend adapter implements."""
from abc import ABC, abstractmethod

from . import models


class AgentError(Exception):
    """An agent call failed (bad exit, timeout, HTTP error, missing key)."""


class Agent(ABC):
    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        """Send a prompt, return the agent's text reply. Raises AgentError on
        failure. `task` (models.DEEP or models.FAST) lets the backend pick
        the most appropriate model for the work."""
