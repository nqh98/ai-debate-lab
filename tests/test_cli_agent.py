import subprocess
import sys

import pytest

from debatelab.agents.base import Agent, AgentError, ErrorKind
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
    assert agent.ask("hello").text == "reply to: hello"


def test_cli_agent_substitutes_prompt_inside_arg(tmp_path):
    script = make_script(tmp_path, 'echo "$1"')
    agent = CliAgent("stub", [script, "prefix {prompt} suffix"])
    assert agent.ask("MID").text == "prefix MID suffix"


def test_cli_agent_nonzero_exit_raises(tmp_path):
    script = make_script(tmp_path, 'echo "boom" >&2; exit 3')
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError, match="exit 3"):
        agent.ask("hello")


def test_cli_agent_timeout_raises(tmp_path, monkeypatch):
    script = make_script(tmp_path, "echo unused")
    agent = CliAgent("stub", [script, "{prompt}"], timeout=1)

    def timeout(cmd, **kwargs):
        assert kwargs["timeout"] == 1
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AgentError, match="timed out"):
        agent.ask("hello")


def test_cli_agent_does_not_inherit_open_stdin(tmp_path):
    # CLIs like `codex exec` read piped stdin until EOF; an inherited
    # never-closing stdin (an interactive terminal) would hang them until
    # the timeout. Run ask() in a child whose stdin we hold open.
    script = make_script(tmp_path, 'echo "got:$(cat)"')
    code = (
        "from debatelab.agents.cli_agent import CliAgent\n"
        f"print(CliAgent('x', [{script!r}, '{{prompt}}'], timeout=3).ask('hi').text)\n"
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


def test_nonzero_exit_is_unknown_and_retryable(tmp_path):
    script = make_script(tmp_path, 'echo "boom" >&2; exit 3')
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.UNKNOWN
    assert exc.value.retryable is True


def test_timeout_is_classified_as_timeout_and_retryable(tmp_path, monkeypatch):
    script = make_script(tmp_path, "echo unused")
    agent = CliAgent("stub", [script, "{prompt}"], timeout=1)

    def timeout(cmd, **kwargs):
        assert kwargs["timeout"] == 1
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.TIMEOUT
    assert exc.value.retryable is True


def test_missing_binary_is_not_found_and_not_retryable():
    agent = CliAgent("stub", ["/no/such/binary", "{prompt}"])
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.kind is ErrorKind.NOT_FOUND
    assert exc.value.retryable is False


def test_cli_errors_never_carry_a_retry_after(tmp_path):
    script = make_script(tmp_path, "exit 1")
    agent = CliAgent("stub", [script, "{prompt}"])
    with pytest.raises(AgentError) as exc:
        agent.ask("hello")
    assert exc.value.retry_after is None


def make_models_script(tmp_path, lines):
    p = tmp_path / "models.sh"
    p.write_text("#!/bin/sh\n" + "".join(f"echo '{line}'\n" for line in lines))
    p.chmod(0o755)
    return str(p)


def test_model_token_dropped_without_models_command(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    agent = CliAgent("stub", [script, "--model={model}", "{prompt}"])
    assert agent.ask("hi").text == "args:hi"


def test_model_selected_per_task(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    lister = make_models_script(tmp_path, ["big-pro", "tiny-mini"])
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    assert agent.ask("hi", task=DEEP).text == "args:--model=big-pro hi"
    assert agent.ask("hi", task=FAST).text == "args:--model=tiny-mini hi"


def test_model_token_dropped_when_names_carry_no_signal(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    lister = make_models_script(tmp_path, ["model-a", "model-b"])
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    assert agent.ask("hi").text == "args:hi"


def test_failing_models_command_falls_back_to_default(tmp_path):
    script = make_script(tmp_path, 'echo "args:$*"')
    agent = CliAgent(
        "stub",
        [script, "--model={model}", "{prompt}"],
        models_command=["/no/such/binary"],
    )
    assert agent.ask("hi").text == "args:hi"


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


def test_cli_agent_reply_carries_no_model_when_it_routes_itself(tmp_path):
    """None is a fact, not a hole: it says the CLI picked its own model."""
    script = make_script(tmp_path, 'echo "reply"')
    agent = CliAgent("stub", [script, "{prompt}"])
    reply = agent.ask("hello")
    assert reply.text == "reply"
    assert reply.model is None


def test_cli_agent_reply_carries_the_resolved_model(tmp_path):
    script = make_script(tmp_path, 'echo "reply"')
    lister = make_script(tmp_path, 'echo "gemini-3-pro"; echo "gemini-3-flash"')
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    assert agent.ask("hello", DEEP).model == "gemini-3-pro"


def test_cli_agent_routes_deep_and_fast_to_different_models(tmp_path):
    """The assertion the model field exists for: choose_model's DEEP/FAST
    routing is otherwise unverifiable from a transcript."""
    script = make_script(tmp_path, 'echo "reply"')
    lister = make_script(tmp_path, 'echo "gemini-3-pro"; echo "gemini-3-flash"')
    agent = CliAgent(
        "stub", [script, "--model={model}", "{prompt}"], models_command=[lister]
    )
    deep = agent.ask("hello", DEEP).model
    fast = agent.ask("hello", FAST).model
    assert deep != fast
    assert {deep, fast} == {"gemini-3-pro", "gemini-3-flash"}


def test_reply_is_immutable():
    from dataclasses import FrozenInstanceError

    from debatelab.agents.base import Reply

    reply = Reply(text="hi", model="m")
    with pytest.raises(FrozenInstanceError):
        reply.model = "other"
