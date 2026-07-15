import pytest

from debatelab import orchestrator
from debatelab.agents import models
from debatelab.agents.base import Agent, AgentError
from debatelab.store import DebateStore


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(orchestrator, "DEFAULT_SLEEP", lambda _seconds: None)


class MockAgent(Agent):
    """Scripted agent: each ask() pops the next response. Exception instances
    are raised instead of returned; running out of responses raises AgentError."""

    def __init__(self, name, responses):
        super().__init__(name)
        self.responses = list(responses)
        self.prompts = []
        self.tasks = []

    def ask(self, prompt: str, task: str = models.DEEP) -> str:
        self.prompts.append(prompt)
        self.tasks.append(task)
        if not self.responses:
            raise AgentError(f"{self.name}: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


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
