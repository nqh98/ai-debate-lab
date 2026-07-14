import subprocess
import sys

import pytest

from debatelab.agents.base import Agent, AgentError
from debatelab.agents.cli_agent import CliAgent
from debatelab.agents.models import DEEP, FAST


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


def test_cli_agent_does_not_inherit_open_stdin(tmp_path):
    # CLIs like `codex exec` read piped stdin until EOF; an inherited
    # never-closing stdin (an interactive terminal) would hang them until
    # the timeout. Run ask() in a child whose stdin we hold open.
    script = make_script(tmp_path, 'echo "got:$(cat)"')
    code = (
        "from debatelab.agents.cli_agent import CliAgent\n"
        f"print(CliAgent('x', [{script!r}, '{{prompt}}'], timeout=3).ask('hi'))\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        out = proc.stdout.read()
        assert proc.wait(timeout=10) == 0, proc.stderr.read()
        assert out == "got:\n"
    finally:
        proc.stdin.close()
        proc.kill()


def test_cli_agent_missing_binary_raises():
    agent = CliAgent("stub", ["/no/such/binary", "{prompt}"])
    with pytest.raises(AgentError, match="not found"):
        agent.ask("hello")


def make_models_script(tmp_path, lines):
    p = tmp_path / "models.sh"
    p.write_text("#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in lines))
    p.chmod(0o755)
    return str(p)


def test_model_token_dropped_without_models_command(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    agent = CliAgent("stub", [script, "--model={model}", "{prompt}"])
    assert agent.ask("hi") == "args:hi"


def test_model_selected_per_task(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    lister = make_models_script(tmp_path, ["big-pro", "tiny-mini"])
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    assert agent.ask("hi", task=DEEP) == "args:--model=big-pro hi"
    assert agent.ask("hi", task=FAST) == "args:--model=tiny-mini hi"


def test_model_token_dropped_when_names_carry_no_signal(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    lister = make_models_script(tmp_path, ["model-a", "model-b"])
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    assert agent.ask("hi") == "args:hi"


def test_failing_models_command_falls_back_to_default(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    agent = CliAgent(
        "stub",
        [script, "--model={model}", "{prompt}"],
        models_command=["/no/such/binary"],
    )
    assert agent.ask("hi") == "args:hi"


def test_models_command_runs_once_and_is_cached(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    counter = tmp_path / "count"
    lister = tmp_path / "models.sh"
    lister.write_text(
        "#!/bin/sh\n"
        f"echo x >> {counter}\n"
        "echo 'big-pro'\necho 'tiny-mini'\n"
    )
    lister.chmod(0o755)
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"],
        models_command=[str(lister)],
    )
    agent.ask("one", task=DEEP)
    agent.ask("two", task=FAST)
    assert counter.read_text().count("x") == 1
