"""Adapter for locally installed AI CLIs (claude, codex, agy, ...)."""
import subprocess

from . import models
from .base import Agent, AgentError, ErrorKind, Reply

MODELS_DISCOVERY_TIMEOUT = 30


def _normalize_timeout(timeout):
    if timeout is None:
        return {"fast": None, "deep": None}
    if isinstance(timeout, int):
        return {"fast": timeout, "deep": timeout}
    return dict(timeout)


class CliAgent(Agent):
    def __init__(
        self,
        name: str,
        command: list[str],
        timeout: dict | int | None = None,
        models_command: list[str] | None = None,
        workdir: str | None = None,
        workspace_args: list[str] | None = None,
    ):
        super().__init__(name)
        self.command = command
        self.timeout = _normalize_timeout(timeout)
        self.models_command = models_command
        self.workdir = workdir
        self.workspace_args = workspace_args
        self._available: list[str] | None = None  # discovered lazily

    def ask(self, prompt: str, task: str = models.DEEP) -> Reply:
        model = self._model_for(task)
        cmd = self._build_command(prompt, model)
        ceiling = self.timeout.get(task)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=ceiling,
                stdin=subprocess.DEVNULL,
                cwd=self.workdir,
            )
        except subprocess.TimeoutExpired:
            raise AgentError(
                f"{self.name}: timed out after {ceiling}s",
                kind=ErrorKind.TIMEOUT,
            )
        except FileNotFoundError:
            raise AgentError(
                f"{self.name}: command not found: {cmd[0]}",
                kind=ErrorKind.NOT_FOUND,
            )
        if proc.returncode != 0:
            raise AgentError(
                f"{self.name}: exit {proc.returncode}: {proc.stderr.strip()[:500]}",
                kind=ErrorKind.UNKNOWN,
            )
        return Reply(text=proc.stdout.strip(), model=model)

    def _build_command(self, prompt: str, model: str | None) -> list[str]:
        """Substitute {prompt} and {model}. A token containing {model} is
        dropped entirely when no model was selected, so the CLI falls back
        to its own default routing."""
        cmd = []
        for part in self.command:
            if "{model}" in part:
                if model is None:
                    continue
                part = part.replace("{model}", model)
            cmd.append(part.replace("{prompt}", prompt))
        if self.workdir and self.workspace_args:
            cmd.extend(self.workspace_args)
        return cmd

    def _model_for(self, task: str) -> str | None:
        if not self.models_command:
            return None
        if self._available is None:
            self._available = self._discover_models()
        return models.choose_model(self._available, task)

    def _discover_models(self) -> list[str]:
        """One line of `models_command` stdout per model. Any failure means
        an empty list: the agent still runs on the platform's default."""
        try:
            proc = subprocess.run(
                self.models_command,
                capture_output=True,
                text=True,
                timeout=MODELS_DISCOVERY_TIMEOUT,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if proc.returncode != 0:
            return []
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]
