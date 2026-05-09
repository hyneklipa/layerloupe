"""End-to-end workflow assertions.

The live-registry job runs in CI on every push/PR; we can't trigger it
from pytest. What we *can* guarantee here is that the YAML stays in
sync with what the e2e tests expect: a real ``registry:2`` with delete
enabled, the right seed images, and pytest invoked against
``tests/e2e/`` with the registry URL exported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
E2E = ROOT / ".github" / "workflows" / "e2e.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(E2E.read_text(encoding="utf-8"))


def _job(workflow: dict[str, Any]) -> dict[str, Any]:
    return workflow["jobs"]["e2e"]


def _step_by_uses(workflow: dict[str, Any], substring: str) -> dict[str, Any]:
    for step in _job(workflow)["steps"]:
        if substring in step.get("uses", ""):
            return step
    raise AssertionError(f"no step using {substring!r} in e2e workflow")


def _step_by_name(workflow: dict[str, Any], substring: str) -> dict[str, Any]:
    for step in _job(workflow)["steps"]:
        if substring in step.get("name", ""):
            return step
    raise AssertionError(f"no step named like {substring!r} in e2e workflow")


# -- Trigger / permissions -----------------------------------------------


def test_e2e_workflow_exists() -> None:
    assert E2E.exists()


def test_runs_on_push_and_pull_request(workflow: dict[str, Any]) -> None:
    """Both branches into ``main`` and PRs should exercise the registry path."""
    on = workflow.get("on") or workflow.get(True)
    assert on is not None
    assert "push" in on
    assert on["push"]["branches"] == ["main"]
    assert "pull_request" in on


def test_workflow_is_read_only_on_contents(workflow: dict[str, Any]) -> None:
    """No reason for the e2e job to write anywhere — defense in depth."""
    perms = workflow["permissions"]
    assert perms["contents"] == "read"
    assert set(perms.keys()) == {"contents"}


def test_concurrency_cancels_in_progress(workflow: dict[str, Any]) -> None:
    """A new push to the same ref supersedes any older e2e run."""
    concurrency = workflow["concurrency"]
    assert "${{ github.ref }}" in concurrency["group"]
    assert concurrency["cancel-in-progress"] is True


# -- Registry service ---------------------------------------------------


def test_registry_service_is_distribution_v2(workflow: dict[str, Any]) -> None:
    """We pin to ``registry:2.8`` so the OCI 1.1 referrers path soft-fails predictably."""
    svc = _job(workflow)["services"]["registry"]
    assert svc["image"].startswith("registry:2")


def test_registry_service_exposes_5000(workflow: dict[str, Any]) -> None:
    svc = _job(workflow)["services"]["registry"]
    assert "5000:5000" in svc["ports"]


def test_registry_service_enables_delete(workflow: dict[str, Any]) -> None:
    """The destructive delete test needs ``REGISTRY_STORAGE_DELETE_ENABLED``."""
    svc = _job(workflow)["services"]["registry"]
    assert svc["env"]["REGISTRY_STORAGE_DELETE_ENABLED"] == "true"


def test_registry_service_has_healthcheck(workflow: dict[str, Any]) -> None:
    """Without a healthcheck the seed step races the registry's startup."""
    svc = _job(workflow)["services"]["registry"]
    options = svc["options"]
    assert "--health-cmd" in options
    assert "/v2/" in options


# -- Seed step ----------------------------------------------------------


def test_install_crane_step_exists(workflow: dict[str, Any]) -> None:
    """Seeding goes via ``crane copy`` — daemon-free and fast in CI."""
    step = _step_by_name(workflow, "Install crane")
    assert "go-containerregistry" in step["run"]
    assert "crane version" in step["run"]


def test_seed_includes_required_fixtures(workflow: dict[str, Any]) -> None:
    """The e2e tests reference these images by name; they must all be seeded."""
    step = _step_by_name(workflow, "Seed")
    body = step["run"]
    for image in (
        "alpine:3.20",
        "alpine:3.19",
        "alpine:latest",
        "hello-world:latest",
    ):
        assert image in body, f"missing seed: {image}"
    # Destructive test target lives in its own repo path.
    assert "scratch/delete-me:1.0" in body


def test_seed_uses_insecure_for_plain_http(workflow: dict[str, Any]) -> None:
    """The in-job ``registry:2`` speaks plain HTTP; crane needs ``--insecure``."""
    step = _step_by_name(workflow, "Seed")
    assert "--insecure" in step["run"]


# -- Test execution -----------------------------------------------------


def test_uses_setup_uv(workflow: dict[str, Any]) -> None:
    _step_by_uses(workflow, "astral-sh/setup-uv")


def test_installs_frozen_deps(workflow: dict[str, Any]) -> None:
    """``--frozen`` keeps CI in lockstep with ``uv.lock``."""
    step = _step_by_name(workflow, "Install Python deps")
    assert "uv sync --frozen" in step["run"]


def test_e2e_pytest_step_targets_e2e_dir(workflow: dict[str, Any]) -> None:
    """Only ``tests/e2e/`` runs in this job; the unit suite has its own workflow."""
    step = _step_by_name(workflow, "Run e2e tests")
    assert "tests/e2e/" in step["run"]
    assert "pytest" in step["run"]


def test_e2e_pytest_step_exports_registry_url(workflow: dict[str, Any]) -> None:
    """Without ``E2E_REGISTRY`` the test module skips itself wholesale."""
    step = _step_by_name(workflow, "Run e2e tests")
    assert step["env"]["E2E_REGISTRY"] == "http://localhost:5000"


# -- Reproducibility ----------------------------------------------------


def test_actions_are_pinned(workflow: dict[str, Any]) -> None:
    """No unpinned ``@main`` references — the e2e job runs on every PR."""
    for step in _job(workflow)["steps"]:
        uses = step.get("uses")
        if uses is None:
            continue
        ref = uses.split("@", 1)[1] if "@" in uses else ""
        assert ref, f"unpinned action: {uses}"
        assert ref != "main", f"action {uses} pinned to a moving branch"
