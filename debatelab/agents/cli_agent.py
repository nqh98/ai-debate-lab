"""Adapter for locally installed AI CLIs (claude, codex, gemini, ...)."""
import subprocess

from .base import Agent, AgentError


class CliAgent(Agent):
    def __init__(self, name: str, command: list[str], timeout: int = 180):
        super().__init__(name)
        self.command = command
        self.timeout = timeout

    def ask(self, prompt: str) -> str:
        cmd = [part.replace("{prompt}", prompt) for part in self.command]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout
            )
        except subprocess.TimeoutExpired:
            raise AgentError(f"{self.name}: timed out after {self.timeout}s")
        except FileNotFoundError:
            raise AgentError(f"{self.name}: command not found: {cmd[0]}")
        if proc.returncode != 0:
            raise AgentError(
                f"{self.name}: exit {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        return proc.stdout.strip()
