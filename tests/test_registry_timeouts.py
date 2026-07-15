"""Timeouts are opt-in ceilings: absent means run forever (spec §3)."""
import pytest

from debatelab.agents import registry


def write_config(tmp_path, timeout_yaml=""):
    body = "agents:\n  - name: a\n    backend: cli\n    command: [\"echo\", \"{prompt}\"]\n"
    if timeout_yaml:
        body += f"    timeout: {timeout_yaml}\n"
    p = tmp_path / "agents.yaml"
    p.write_text(body)
    return p


def test_absent_timeout_means_no_ceiling(tmp_path):
    spec = registry.load_agent_specs(write_config(tmp_path))[0]
    assert spec.timeout == {"fast": None, "deep": None}


def test_int_timeout_applies_to_both_tiers(tmp_path):
    spec = registry.load_agent_specs(write_config(tmp_path, "240"))[0]
    assert spec.timeout == {"fast": 240, "deep": 240}


def test_map_timeout_sets_tiers_independently(tmp_path):
    spec = registry.load_agent_specs(
        write_config(tmp_path, "{fast: 120, deep: null}")
    )[0]
    assert spec.timeout == {"fast": 120, "deep": None}


def test_map_timeout_missing_tier_defaults_to_none(tmp_path):
    spec = registry.load_agent_specs(write_config(tmp_path, "{fast: 120}"))[0]
    assert spec.timeout == {"fast": 120, "deep": None}


def test_unknown_timeout_key_is_config_error(tmp_path):
    with pytest.raises(registry.ConfigError):
        registry.load_agent_specs(write_config(tmp_path, "{slow: 5}"))


def test_non_numeric_timeout_is_config_error(tmp_path):
    with pytest.raises(registry.ConfigError):
        registry.load_agent_specs(write_config(tmp_path, "soon"))
