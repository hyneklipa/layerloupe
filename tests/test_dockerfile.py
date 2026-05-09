"""Dockerfile + .dockerignore static checks.

We don't actually shell out to ``docker build`` (no Docker in CI for the
unit-test job), but we do verify the file is syntactically reasonable and
follows the contract the deployment guide promises:

* Multi-stage build with a ``deps`` stage and a ``runtime`` stage.
* uv used for dependency installation.
* Non-root runtime user.
* HEALTHCHECK against ``/api/healthz``.
* Tests / dev cruft excluded by ``.dockerignore``.

Container behavior is exercised manually with ``docker build`` + ``docker run``
via the compose stack and the e2e tests against ``registry:2``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
DOCKERFILE = ROOT / "Dockerfile"
DOCKERIGNORE = ROOT / ".dockerignore"


@pytest.fixture(scope="module")
def dockerfile_text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def dockerignore_text() -> str:
    return DOCKERIGNORE.read_text(encoding="utf-8")


# -- Existence -----------------------------------------------------------


def test_dockerfile_exists() -> None:
    assert DOCKERFILE.exists()


def test_dockerignore_exists() -> None:
    assert DOCKERIGNORE.exists()


# -- Multi-stage shape ---------------------------------------------------


def test_multi_stage_build(dockerfile_text: str) -> None:
    """Two named stages: deps and runtime."""
    assert "AS deps" in dockerfile_text
    assert "AS runtime" in dockerfile_text


def test_uses_python_3_14_slim(dockerfile_text: str) -> None:
    """Pinned base; matches the project's requires-python."""
    assert "python:3.14-slim" in dockerfile_text


def test_uv_brought_in_from_official_image(dockerfile_text: str) -> None:
    """uv as a single static binary copied from its official OCI image."""
    assert "ghcr.io/astral-sh/uv" in dockerfile_text
    assert "/usr/local/bin/" in dockerfile_text


def test_uv_sync_uses_frozen_no_dev(dockerfile_text: str) -> None:
    """Production install must respect the lockfile and skip dev deps."""
    assert "uv sync --frozen --no-dev" in dockerfile_text


def test_runtime_copies_venv_from_deps_stage(dockerfile_text: str) -> None:
    assert "COPY --from=deps" in dockerfile_text
    assert "/app/.venv" in dockerfile_text


# -- Security / runtime hygiene ------------------------------------------


def test_runs_as_non_root_user(dockerfile_text: str) -> None:
    """``runAsNonRoot: true`` policies in K8s require this."""
    assert "useradd" in dockerfile_text
    assert "USER layerloupe" in dockerfile_text


def test_python_runtime_env_vars(dockerfile_text: str) -> None:
    assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile_text
    assert "PYTHONUNBUFFERED=1" in dockerfile_text


def test_json_logging_default_on(dockerfile_text: str) -> None:
    """Production stdout should be machine-readable by default."""
    assert "LOG_JSON=true" in dockerfile_text


def test_exposes_8080(dockerfile_text: str) -> None:
    assert "EXPOSE 8080" in dockerfile_text


def test_cmd_runs_uvicorn_on_0_0_0_0_8080(dockerfile_text: str) -> None:
    assert "uvicorn" in dockerfile_text
    assert "layerloupe.main:app" in dockerfile_text
    assert "0.0.0.0" in dockerfile_text
    assert "8080" in dockerfile_text


def test_healthcheck_present(dockerfile_text: str) -> None:
    """Container-level liveness — orchestrators (compose, k8s) use it."""
    assert "HEALTHCHECK" in dockerfile_text
    assert "/api/healthz" in dockerfile_text


def test_oci_image_labels_present(dockerfile_text: str) -> None:
    assert "org.opencontainers.image.title" in dockerfile_text
    assert "org.opencontainers.image.licenses" in dockerfile_text
    assert "org.opencontainers.image.source" in dockerfile_text


# -- .dockerignore covers the obvious noise ------------------------------


@pytest.mark.parametrize(
    "ignored",
    [
        ".venv/",
        "__pycache__/",
        ".pytest_cache/",
        ".mypy_cache/",
        ".ruff_cache/",
        ".git/",
        ".github/",
        "tests/",
        ".env",
        ".pre-commit-config.yaml",
    ],
)
def test_dockerignore_excludes(ignored: str, dockerignore_text: str) -> None:
    assert ignored in dockerignore_text, f".dockerignore should list {ignored!r}"


def test_dockerignore_does_not_exclude_pyproject(dockerignore_text: str) -> None:
    """The deps stage needs pyproject.toml + uv.lock — don't ignore them."""
    assert "pyproject.toml" not in dockerignore_text
    assert "uv.lock" not in dockerignore_text


def test_dockerignore_does_not_exclude_app_source(dockerignore_text: str) -> None:
    """The layerloupe/ package itself MUST NOT be ignored."""
    assert "\nlayerloupe/\n" not in dockerignore_text
    assert "\nlayerloupe\n" not in dockerignore_text
