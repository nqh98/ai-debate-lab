import pytest

from debatelab import orchestrator, prompts
from debatelab.agents import models
from debatelab.agents.base import Agent, AgentError, Reply
from debatelab.store import DebateStore


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(orchestrator, "DEFAULT_SLEEP", lambda _seconds: None)


class MockAgent(Agent):
    """Scripted agent: each ask() pops the next response. Exception instances
    are raised instead of returned; running out of responses raises AgentError.

    The synthesize call is answered from a dedicated `synthesis` slot rather
    than the queue. Only the nomination winner is asked to synthesize, so the
    call is out-of-band relative to the per-agent round-robin the queue
    models: a sixth queued response would be popped by every NON-winner's
    vote-phase ask and fail to parse as a vote. Routing on the prompt is
    correct for any roster, including one where the seeded fallback draw
    elects an agent nobody nominated.
    """

    def __init__(self, name, responses, synthesis=None, model=None):
        super().__init__(name)
        self.responses = list(responses)
        self.synthesis = synthesis
        self.model = model
        self.prompts = []
        self.tasks = []

    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        self.prompts.append(prompt)
        self.tasks.append(task)
        if prompts.SYNTHESIS_HEADER in prompt:
            item = self.synthesis
            if item is None:
                item = f"synthesis from {self.name}"
            if isinstance(item, Exception):
                raise item
            return Reply(text=item, model=self.model)
        if not self.responses:
            raise AgentError(f"{self.name}: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Reply(text=item, model=self.model)


def make_store(tmp_path):
    return DebateStore(tmp_path / "debates")


def happy_agent(name, nominee="a", synthesis=None):
    return MockAgent(name, [
        f"proposal from {name}",
        f"critique from {name}",
        f"revised proposal from {name}",
        f"NOMINATE: {nominee}\nbest one",
        "VOTE: accept\nagreed",
    ], synthesis=synthesis)
