"""README sanity checks.

The goal is "a stranger can stand the project up in under 5 minutes by
reading the README". We can't test that directly here, but we can
guarantee the README at least *contains* the ingredients of that flow:

* the Docker Compose quickstart command,
* every documented ``*`` env var,
* the development setup (uv + tests + lint).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
README = ROOT / "README.md"


@pytest.fixture(scope="module")
def readme_text() -> str:
    return README.read_text(encoding="utf-8")


# -- Five-minute quickstart ----------------------------------------------


def test_readme_documents_docker_compose_quickstart(readme_text: str) -> None:
    """A copy-pasteable ``docker compose up`` is the headline path."""
    assert "docker compose up" in readme_text
    assert "localhost:8080" in readme_text


def test_readme_explains_what_compose_starts(readme_text: str) -> None:
    """The reader should know what services they're getting."""
    for kw in ("registry", "layerloupe", "seed"):
        assert kw in readme_text.lower()


def test_readme_mentions_default_seed_images(readme_text: str) -> None:
    """Helps the user verify the demo loaded correctly."""
    text = readme_text.lower()
    seeded = sum(1 for img in ("alpine", "busybox", "hello-world") if img in text)
    assert seeded >= 2


# -- Configuration reference: every documented env var ------------------


_DOCUMENTED_ENV_VARS = {
    "REGISTRY_URL",
    "REGISTRY_PUBLIC_URL",
    "SSL_VERIFY",
    "REGISTRY_USERNAME",
    "REGISTRY_PASSWORD",
    "ALLOW_REGISTRY_LOGIN",
    "AUTH_MODE",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD_HASH",
    "AUDIT_LOG_PATH",
    "TITLE",
    "SESSION_SECRET",
    "LOG_LEVEL",
    "LOG_JSON",
    "CACHE_TTL",
    "PAGE_SIZE",
}


@pytest.mark.parametrize("env_var", sorted(_DOCUMENTED_ENV_VARS))
def test_readme_documents_env_var(env_var: str, readme_text: str) -> None:
    assert env_var in readme_text, f"README is missing config: {env_var}"


# -- Production deployment ----------------------------------------------


def test_readme_documents_production_docker_run(readme_text: str) -> None:
    """A bare ``docker run`` example for prod-like deployment."""
    assert "docker build" in readme_text
    assert "docker run" in readme_text
    assert "8080:8080" in readme_text


def test_readme_mentions_health_endpoints(readme_text: str) -> None:
    assert "/api/healthz" in readme_text
    assert "/api/readyz" in readme_text


def test_readme_mentions_non_root_runtime(readme_text: str) -> None:
    """Operators running under restricted PSPs need to know."""
    assert "non-root" in readme_text.lower()


# -- Development guide --------------------------------------------------


def test_readme_documents_uv_workflow(readme_text: str) -> None:
    for cmd in ("uv sync", "uv run pytest", "uv run ruff", "uv run mypy"):
        assert cmd in readme_text, f"missing developer command: {cmd}"


def test_readme_documents_python_version(readme_text: str) -> None:
    assert "3.13" in readme_text


def test_readme_lists_project_layout(readme_text: str) -> None:
    """At minimum, point at the package + docs + dockerfile."""
    layout_section = re.search(r"```\s*\n(layerloupe/.*?)```", readme_text, re.DOTALL)
    assert layout_section is not None, "expected a project-layout code block"
    layout = layout_section.group(1)
    for entry in ("pyproject.toml", "Dockerfile", "docker-compose.yml", "tests/"):
        assert entry in layout


# -- Architecture overview ----------------------------------------------


def test_readme_explains_three_layer_architecture(readme_text: str) -> None:
    """The reader should grasp the registry-client / API / web split."""
    text = readme_text.lower()
    assert "layerloupe.registry" in text or "registry client" in text
    assert "layerloupe.api" in text or "rest api" in text
    assert "layerloupe.web" in text or "htmx" in text


# -- Status / quality signals -------------------------------------------


def test_readme_states_license(readme_text: str) -> None:
    assert "MIT" in readme_text
