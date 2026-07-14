import pytest

from debatelab.agents.base import Agent, AgentError
from debatelab.agents.cli_agent import CliAgent


def make_script(tmp_path, body):
    p = tmp_path / "stub.sh"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(0o755)
    return str(p)


def test_cli_agent_is_an_agent(tmp_path):
    agent = CliAgent("stub", ["echo", "{prompt}"])
    assert isinstance(agent, Agent)
    assert agent.name == "stub"


def test_cli_agent_returns_stripped_stdout(tmp_path):
    script = make_script(tmp_path, 'echo "reply to: $1"')
    agent = CliAgent("stub", [script, "{prompt}"])
    assert agent.ask("hello") == "reply to: hello"


def test_cli_agent_substitutes_prompt_inside_arg(tmp_path):
    script = make_script(tmp_path, 'echo "$1"')
    agent = CliAgent("stub", [script, "prefix {prompt} suffix"])
    assert agent.ask("MID") == "prefix MID suffix"


def test_cli_agent_nonzero_exit_raises(tmp_path):
    script = make_script(tmp_path, 'echo "boom" >&2; exit 3')
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError, match="exit 3"):
        agent.ask("hello")


def test_cli_agent_timeout_raises(tmp_path):
    script = make_script(tmp_path, "sleep 5")
    agent = CliAgent("stub", [script, "{prompt}"], timeout=1)
    with pytest.raises(AgentError, match="timed out"):
        agent.ask("hello")


def test_cli_agent_missing_binary_raises():
    agent = CliAgent("stub", ["/no/such/binary", "{prompt}"])
    with pytest.raises(AgentError, match="not found"):
        agent.ask("hello")
