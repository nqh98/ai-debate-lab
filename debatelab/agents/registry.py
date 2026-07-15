"""Loads agents.yaml into specs and builds enabled Agent instances."""
import os
import shutil
from dataclasses import dataclass, field
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
    models_command: list | None = None
    provider: str | None = None
    model: str | None = None  # optional pin; omit to auto-select per task
    api_key_env: str | None = None
    base_url: str | None = None
    timeout: dict = field(
        default_factory=lambda: {"fast": None, "deep": None}
    )


def _parse_task_seconds(raw, *, field_name, path, name,
                        default_fast, default_deep):
    """Normalize an int-or-{fast,deep} YAML value to a per-tier dict.

    Absent -> the given defaults. A bare int applies to both tiers. A map
    may set either tier to an int or null."""
    if raw is None:
        return {"fast": default_fast, "deep": default_deep}
    if isinstance(raw, bool):
        raise ConfigError(
            f"{path}: agent '{name}': {field_name} must be a number or "
            f"a {{fast, deep}} map"
        )
    if isinstance(raw, int):
        return {"fast": raw, "deep": raw}
    if isinstance(raw, dict):
        unknown = set(raw) - {"fast", "deep"}
        if unknown:
            raise ConfigError(
                f"{path}: agent '{name}': {field_name} has unknown "
                f"key(s): {', '.join(sorted(unknown))}"
            )
        out = {}
        for tier, default in (("fast", default_fast), ("deep", default_deep)):
            value = raw.get(tier, default)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int)
            ):
                raise ConfigError(
                    f"{path}: agent '{name}': {field_name}.{tier} must be "
                    f"a number or null"
                )
            out[tier] = value
        return out
    raise ConfigError(
        f"{path}: agent '{name}': {field_name} must be a number or "
        f"a {{fast, deep}} map"
    )


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
        if backend not in ("cli", "api", "auto"):
            raise ConfigError(
                f"{path}: agent '{name}': backend must be 'cli', 'api', or 'auto'"
            )
        specs.append(
            AgentSpec(
                name=name,
                backend=backend,
                enabled=bool(entry.get("enabled", True)),
                command=entry.get("command"),
                models_command=entry.get("models_command"),
                provider=entry.get("provider"),
                model=entry.get("model"),
                api_key_env=entry.get("api_key_env"),
                base_url=entry.get("base_url"),
                timeout=_parse_task_seconds(
                    entry.get("timeout"), field_name="timeout",
                    path=path, name=name,
                    default_fast=None, default_deep=None,
                ),
            )
        )
    return specs


def _cli_problem(spec: AgentSpec) -> str | None:
    if not spec.command:
        return "cli agent needs a 'command' list"
    if shutil.which(spec.command[0]) is None:
        return f"command not found on PATH: {spec.command[0]}"
    return None


def _api_problem(spec: AgentSpec) -> str | None:
    if spec.provider not in DRIVERS:
        return (
            f"unknown provider '{spec.provider}' "
            f"(known: {', '.join(sorted(DRIVERS))})"
        )
    if not spec.api_key_env:
        return "api agent needs 'api_key_env'"
    if not os.environ.get(spec.api_key_env):
        return f"env var {spec.api_key_env} is not set"
    return None


def spec_problem(spec: AgentSpec) -> str | None:
    """Actionable reason this agent can't run right now, or None if usable."""
    if spec.backend == "cli":
        return _cli_problem(spec)
    if spec.backend == "api":
        return _api_problem(spec)
    cli_problem = _cli_problem(spec)
    if cli_problem is None:
        return None
    api_problem = _api_problem(spec)
    if api_problem is None:
        return None
    return f"cli: {cli_problem}; api: {api_problem}"


def resolve_backend(spec: AgentSpec) -> str:
    """Concrete backend to run: 'auto' picks cli when usable, api otherwise."""
    if spec.backend != "auto":
        return spec.backend
    return "cli" if _cli_problem(spec) is None else "api"


def build_agents(specs: list[AgentSpec]) -> list[Agent]:
    agents = []
    for spec in specs:
        if not spec.enabled:
            continue
        problem = spec_problem(spec)
        if problem:
            raise ConfigError(f"agent '{spec.name}': {problem}")
        if resolve_backend(spec) == "cli":
            agents.append(
                CliAgent(
                    spec.name, spec.command, spec.timeout, spec.models_command
                )
            )
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
