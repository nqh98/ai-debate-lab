from debatelab.agents.base import Agent, AgentError


class MockAgent(Agent):
    """Scripted agent: each ask() pops the next response. Exception instances
    are raised instead of returned; running out of responses raises AgentError."""

    def __init__(self, name, responses):
        super().__init__(name)
        self.responses = list(responses)
        self.prompts = []

    def ask(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.responses:
            raise AgentError(f"{self.name}: no scripted response left")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
