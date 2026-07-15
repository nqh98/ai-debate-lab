"""CLI agents run inside the workspace with per-platform flags (spec §3)."""
import sys

from debatelab.agents import models, registry
from debatelab.agents.cli_agent import CliAgent


def test_cli_agent_runs_in_workdir(tmp_path):
    """The stub can only see marker.txt if cwd was the workdir — this is
    the wiring the whole feature hangs on (spec §8 integration)."""
    (tmp_path / "marker.txt").write_text("proof-of-cwd\n")
    agent = CliAgent(
        "x",
        [sys.executable, "-c",
         "print(open('marker.txt').read().strip())"],
        workdir=str(tmp_path),
    )
    assert agent.ask("ignored").text == "proof-of-cwd"


def test_workspace_args_appended_only_when_attached(tmp_path):
    attached = CliAgent(
        "x", ["echo", "{prompt}"], workdir=str(tmp_path),
        workspace_args=["--sandbox", "workspace-write"],
    )
    assert attached._build_command("hi", None) == [
        "echo", "hi", "--sandbox", "workspace-write",
    ]
    detached = CliAgent(
        "x", ["echo", "{prompt}"],
        workspace_args=["--sandbox", "workspace-write"],
    )
    assert detached._build_command("hi", None) == ["echo", "hi"]


def test_build_agents_marks_attachment(tmp_path):
    config = tmp_path / "agents.yaml"
    config.write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "    stall_after: {deep: 1200}\n"
    )
    specs = registry.load_agent_specs(config)
    attached = registry.build_agents(specs, workdir=str(tmp_path))[0]
    assert attached.workspace_attached is True
    assert attached.stall_after == {"fast": 300, "deep": 1200}
    detached = registry.build_agents(specs)[0]
    assert detached.workspace_attached is False


def test_workspace_args_must_be_a_list_of_strings(tmp_path):
    config = tmp_path / "agents.yaml"
    config.write_text(
        "agents:\n"
        "  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
        "    workspace_args: \"--oops\"\n"
    )
    try:
        registry.load_agent_specs(config)
    except registry.ConfigError:
        return
    raise AssertionError("expected ConfigError")
