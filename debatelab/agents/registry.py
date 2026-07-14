"""Loads agents.yaml into specs and builds enabled Agent instances."""
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import yaml

from .api_agent import ApiAgent, DRIVERS
from .base import Agent
from .cli_agent import CliAgent


class ConfigError(Exception):
    pass


@dataclass
class AgentSpec:
    name: str
    backend: str
    enabled: bool = True
    command: list | None = None
    provider: str | None = None
    model: str | None = None
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: int = 180


def load_agent_specs(path) -> list[AgentSpec]:
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict) or not isinstance(raw.get("agents"), list):
        raise ConfigError(f"{path}: expected a top-level 'agents' list")
    specs, seen = [], set()
    for i, entry in enumerate(raw["agents"]):
        if not isinstance(entry, dict) or "name" not in entry:
            raise ConfigError(f"{path}: agents[{i}] needs a 'name'")
        name = entry["name"]
        if name in seen:
            raise ConfigError(f"{path}: duplicate agent name '{name}'")
        seen.add(name)
        backend = entry.get("backend")
        if backend not in ("cli", "api"):
            raise ConfigError(
                f"{path}: agent '{name}': backend must be 'cli' or 'api'"
            )
        specs.append(
            AgentSpec(
                name=name,
                backend=backend,
                enabled=bool(entry.get("enabled", True)),
                command=entry.get("command"),
                provider=entry.get("provider"),
                model=entry.get("model"),
                api_key_env=entry.get("api_key_env"),
                base_url=entry.get("base_url"),
                timeout=int(entry.get("timeout", 180)),
            )
        )
    return specs


def spec_problem(spec: AgentSpec) -> str | None:
    """Actionable reason this agent can't run right now, or None if usable."""
    if spec.backend == "cli":
        if not spec.command:
            return "cli agent needs a 'command' list"
        if shutil.which(spec.command[0]) is None:
            return f"command not found on PATH: {spec.command[0]}"
    else:
        if spec.provider not in DRIVERS:
            return (
                f"unknown provider '{spec.provider}' "
                f"(known: {', '.join(sorted(DRIVERS))})"
            )
        if not spec.model:
            return "api agent needs a 'model'"
        if not spec.api_key_env:
            return "api agent needs 'api_key_env'"
        if not os.environ.get(spec.api_key_env):
            return f"env var {spec.api_key_env} is not set"
    return None


def build_agents(specs: list[AgentSpec]) -> list[Agent]:
    agents = []
    for spec in specs:
        if not spec.enabled:
            continue
        problem = spec_problem(spec)
        if problem:
            raise ConfigError(f"agent '{spec.name}': {problem}")
        if spec.backend == "cli":
            agents.append(CliAgent(spec.name, spec.command, spec.timeout))
        else:
            agents.append(
                ApiAgent(
                    spec.name,
                    spec.provider,
                    spec.model,
                    spec.api_key_env,
                    spec.base_url,
                    spec.timeout,
                )
            )
    return agents
