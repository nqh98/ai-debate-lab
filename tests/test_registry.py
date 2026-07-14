import pytest

from debatelab.agents.cli_agent import CliAgent
from debatelab.agents.api_agent import ApiAgent
from debatelab.agents.registry import (
    AgentSpec,
    ConfigError,
    build_agents,
    load_agent_specs,
    spec_problem,
)

GOOD_YAML = """\
agents:
  - name: alpha
    backend: cli
    command: ["echo", "{prompt}"]
  - name: beta
    backend: api
    provider: openai
    model: gpt-5
    api_key_env: BETA_KEY
    base_url: https://api.x.ai/v1
    enabled: false
"""


def write(tmp_path, text):
    p = tmp_path / "agents.yaml"
    p.write_text(text)
    return p


def test_load_good_config(tmp_path):
    specs = load_agent_specs(write(tmp_path, GOOD_YAML))
    assert [s.name for s in specs] == ["alpha", "beta"]
    assert specs[0].backend == "cli" and specs[0].enabled is True
    assert specs[1].enabled is False and specs[1].base_url == "https://api.x.ai/v1"
    assert specs[0].timeout == 180


def test_load_rejects_missing_agents_key(tmp_path):
    with pytest.raises(ConfigError, match="agents"):
        load_agent_specs(write(tmp_path, "foo: bar\n"))


def test_load_rejects_bad_backend(tmp_path):
    with pytest.raises(ConfigError, match="backend"):
        load_agent_specs(
            write(tmp_path, "agents:\n  - name: x\n    backend: quantum\n")
        )


def test_load_rejects_duplicate_names(tmp_path):
    text = (
        "agents:\n"
        "  - name: x\n    backend: cli\n    command: [echo]\n"
        "  - name: x\n    backend: cli\n    command: [echo]\n"
    )
    with pytest.raises(ConfigError, match="duplicate"):
        load_agent_specs(write(tmp_path, text))


def test_spec_problem_missing_binary():
    spec = AgentSpec(name="x", backend="cli", command=["/no/such/bin"])
    assert "not found" in spec_problem(spec)


def test_spec_problem_missing_env_var(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    spec = AgentSpec(
        name="x", backend="api", provider="openai", model="m", api_key_env="NOPE_KEY"
    )
    assert "NOPE_KEY" in spec_problem(spec)


def test_spec_problem_unknown_provider():
    spec = AgentSpec(
        name="x", backend="api", provider="mystery", model="m", api_key_env="K"
    )
    assert "unknown provider" in spec_problem(spec)


def test_spec_problem_none_when_usable(monkeypatch):
    monkeypatch.setenv("OK_KEY", "k")
    cli = AgentSpec(name="a", backend="cli", command=["echo", "{prompt}"])
    api = AgentSpec(
        name="b", backend="api", provider="openai", model="m", api_key_env="OK_KEY"
    )
    assert spec_problem(cli) is None
    assert spec_problem(api) is None


def test_build_agents_skips_disabled_and_builds_types(monkeypatch):
    monkeypatch.setenv("OK_KEY", "k")
    specs = [
        AgentSpec(name="a", backend="cli", command=["echo", "{prompt}"]),
        AgentSpec(
            name="b", backend="api", provider="openai", model="m", api_key_env="OK_KEY"
        ),
        AgentSpec(name="c", backend="cli", enabled=False, command=["echo"]),
    ]
    agents = build_agents(specs)
    assert [a.name for a in agents] == ["a", "b"]
    assert isinstance(agents[0], CliAgent)
    assert isinstance(agents[1], ApiAgent)


def test_build_agents_raises_on_broken_enabled_spec(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    specs = [
        AgentSpec(
            name="bad", backend="api", provider="openai", model="m",
            api_key_env="NOPE_KEY",
        )
    ]
    with pytest.raises(ConfigError, match="bad"):
        build_agents(specs)
