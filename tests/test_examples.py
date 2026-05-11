"""Structural validation for the ``examples/`` directory.

Each subdirectory under ``examples/`` is a self-contained deployment
scenario. The contract is light: every scenario carries at least a
``README.md``; if it ships its own ``docker-compose.yml`` or
``.env.example``, those must be valid (parseable YAML, env keys we
actually recognize).

These tests intentionally don't validate compose semantics
(service-level env, port maps, …). End-to-end behavior is exercised by
the per-scenario e2e tests as they get added; this file just keeps the
inventory consistent so a typo in a new example doesn't slip through.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from layerloupe.config import Settings

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"


def _scenarios() -> list[Path]:
    """Return all example scenarios (one subdir per scenario)."""
    if not EXAMPLES_DIR.exists():
        return []
    return sorted(p for p in EXAMPLES_DIR.iterdir() if p.is_dir())


def _settings_env_names() -> set[str]:
    """Env names every ``Settings`` field accepts (no prefix, uppercased).

    Includes both the field itself and any ``_FILE`` companions that the
    redesign adds (e.g. ``SESSION_SECRET_FILE``) — they're modeled as
    distinct fields, so ``model_fields`` already covers both.
    """
    return {name.upper() for name in Settings.model_fields}


# -- Top-level inventory --------------------------------------------------


def test_examples_dir_has_overview_readme() -> None:
    """The top-level ``examples/README.md`` is the rozcestník."""
    assert (EXAMPLES_DIR / "README.md").exists()


def test_public_scenario_exists() -> None:
    """``public/`` is the canonical baseline — always present."""
    assert (EXAMPLES_DIR / "public" / "README.md").exists()


# -- Per-scenario structural checks --------------------------------------


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda p: p.name)
def test_scenario_has_readme(scenario: Path) -> None:
    assert (scenario / "README.md").exists(), f"{scenario.name}/ is missing README.md"


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda p: p.name)
def test_scenario_compose_is_valid_yaml_when_present(scenario: Path) -> None:
    """When a scenario ships its own compose, it must parse and declare services.

    Scenarios that point at an external canonical compose (``public/``
    points back at the root) legitimately have no compose file here.
    """
    compose = scenario / "docker-compose.yml"
    if not compose.exists():
        pytest.skip(f"{scenario.name}/ has no docker-compose.yml")
    data = yaml.safe_load(compose.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{scenario.name}/docker-compose.yml is not a mapping"
    assert "services" in data, f"{scenario.name}/docker-compose.yml has no `services` key"


@pytest.mark.parametrize("scenario", _scenarios(), ids=lambda p: p.name)
def test_scenario_env_example_keys_are_known_settings(scenario: Path) -> None:
    """Every uncommented ``KEY=value`` in ``.env.example`` must match a
    ``Settings`` field. Catches typos (``ADMIN_USERNMAE``,
    ``AUTHMODE``) before they reach docs."""
    env_file = scenario / ".env.example"
    if not env_file.exists():
        pytest.skip(f"{scenario.name}/ has no .env.example")
    known = _settings_env_names()
    unknown: list[str] = []
    for line in env_file.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in known:
            unknown.append(key)
    assert not unknown, f"{scenario.name}/.env.example references unknown Settings keys: {unknown}"
