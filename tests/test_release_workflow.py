"""Release workflow assertions.

The workflow runs in CI on tag pushes; we can't trigger it from pytest.
What we can guarantee is that the YAML stays in sync with the deployment
guide: it triggers on the right ref, has the permissions GHCR needs,
builds multi-arch, and creates a GitHub Release.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).parent.parent
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    return yaml.safe_load(RELEASE.read_text(encoding="utf-8"))


def _step_by_uses(workflow: dict[str, Any], substring: str) -> dict[str, Any]:
    """Find the first step whose ``uses:`` contains ``substring``."""
    for step in workflow["jobs"]["release"]["steps"]:
        if substring in step.get("uses", ""):
            return step
    raise AssertionError(f"no step using {substring!r} in release workflow")


def _all_steps_by_uses(workflow: dict[str, Any], substring: str) -> list[dict[str, Any]]:
    """Find every step whose ``uses:`` contains ``substring``."""
    return [
        step
        for step in workflow["jobs"]["release"]["steps"]
        if substring in step.get("uses", "")
    ]


# -- Trigger / permissions -----------------------------------------------


def test_release_workflow_exists() -> None:
    assert RELEASE.exists()


def test_triggered_only_on_v_tag_push(workflow: dict[str, Any]) -> None:
    """Push to ``v*`` tags is the sole entry point — no manual / schedule triggers."""
    # YAML's ``on:`` is parsed as the boolean ``True`` key in PyYAML 6.x.
    on = workflow.get("on") or workflow.get(True)
    assert on is not None
    assert "push" in on
    assert on["push"]["tags"] == ["v*"]
    # No other triggers — keeps the release path tight and auditable.
    assert set(on.keys()) == {"push"}


def test_workflow_has_required_permissions(workflow: dict[str, Any]) -> None:
    """``packages: write`` for ghcr push, ``contents: write`` for GitHub Release."""
    perms = workflow["permissions"]
    assert perms["packages"] == "write"
    assert perms["contents"] == "write"


def test_workflow_has_no_extra_permissions(workflow: dict[str, Any]) -> None:
    """Principle of least privilege — release shouldn't write to issues etc."""
    assert set(workflow["permissions"].keys()) == {"contents", "packages"}


# -- Container build ----------------------------------------------------


def test_uses_docker_build_cloud(workflow: dict[str, Any]) -> None:
    """Native amd64 + arm64 builders via Build Cloud — skips the QEMU
    emulation tax that cross-arch buildx would otherwise pay."""
    step = _step_by_uses(workflow, "docker/setup-buildx-action")
    assert step["with"]["driver"] == "cloud"
    assert "endpoint" in step["with"]


def test_does_not_use_qemu(workflow: dict[str, Any]) -> None:
    """Build Cloud replaces QEMU — having both would just slow the runner
    down (and the qemu setup step would be dead weight)."""
    for step in workflow["jobs"]["release"]["steps"]:
        assert "docker/setup-qemu-action" not in step.get("uses", "")


def test_logs_into_both_registries(workflow: dict[str, Any]) -> None:
    """Releases publish to both GHCR and Docker Hub — one login step each."""
    logins = _all_steps_by_uses(workflow, "docker/login-action")
    assert len(logins) == 2, f"expected 2 login steps (ghcr + docker hub), got {len(logins)}"

    ghcr_login = next((s for s in logins if s["with"].get("registry") == "ghcr.io"), None)
    assert ghcr_login is not None, "missing GHCR login step"
    assert "GITHUB_TOKEN" in ghcr_login["with"]["password"]

    # Docker Hub login has no ``registry:`` (defaults to docker.io) — that's
    # how we tell the two apart in the tag/login wiring.
    dockerhub_login = next((s for s in logins if "registry" not in s["with"]), None)
    assert dockerhub_login is not None, "missing Docker Hub login step"
    assert "DOCKER_USER" in dockerhub_login["with"]["username"]
    assert "DOCKER_PAT" in dockerhub_login["with"]["password"]


def test_images_published_under_canonical_paths(workflow: dict[str, Any]) -> None:
    """``ghcr.io/<owner>/<repo>`` for GitHub-side discoverability;
    ``<docker_user>/layerloupe`` for Docker Hub. Both env vars are
    referenced by metadata-action and the release-body template."""
    assert workflow["env"]["GHCR_IMAGE"] == "ghcr.io/${{ github.repository }}"
    assert workflow["env"]["DOCKERHUB_IMAGE"] == "${{ vars.DOCKER_USER }}/layerloupe"


def test_metadata_action_targets_both_registries(workflow: dict[str, Any]) -> None:
    """Single metadata + build step covers both registries via the ``images:``
    multi-line list — ensures the same semver tags land in both places."""
    step = _step_by_uses(workflow, "docker/metadata-action")
    images = step["with"]["images"]
    assert "${{ env.GHCR_IMAGE }}" in images
    assert "${{ env.DOCKERHUB_IMAGE }}" in images


def test_metadata_tags_include_semver_variants(workflow: dict[str, Any]) -> None:
    """Each release tags ``X.Y.Z``, ``X.Y``, ``X`` — and ``latest`` for stables."""
    step = _step_by_uses(workflow, "docker/metadata-action")
    tags = step["with"]["tags"]
    for pattern in ("{{version}}", "{{major}}.{{minor}}", "{{major}}"):
        assert pattern in tags
    # ``latest`` shows up via the ``flavor.latest=auto`` rule.
    flavor = step["with"].get("flavor", "")
    assert "latest=auto" in flavor


def test_build_pushes_multi_arch(workflow: dict[str, Any]) -> None:
    """Both amd64 and arm64 — Macs, Pis, Graviton instances all served."""
    step = _step_by_uses(workflow, "docker/build-push-action")
    platforms = step["with"]["platforms"]
    assert "linux/amd64" in platforms
    assert "linux/arm64" in platforms


def test_no_explicit_cache_with_build_cloud(workflow: dict[str, Any]) -> None:
    """Build Cloud has shared remote cache built-in — explicit type=gha
    cache would be redundant and burn the (small) GHA cache quota."""
    step = _step_by_uses(workflow, "docker/build-push-action")
    assert "cache-from" not in step["with"]
    assert "cache-to" not in step["with"]


def test_build_emits_provenance_and_sbom(workflow: dict[str, Any]) -> None:
    """Supply-chain hygiene — buildx attestations land alongside the image."""
    step = _step_by_uses(workflow, "docker/build-push-action")
    assert step["with"].get("provenance") is True
    assert step["with"].get("sbom") is True


# -- GitHub Release -----------------------------------------------------


def test_creates_github_release(workflow: dict[str, Any]) -> None:
    step = _step_by_uses(workflow, "softprops/action-gh-release")
    assert step["with"]["generate_release_notes"] is True
    # The release body teaches operators where to grab the image —
    # both registries should be advertised so users on either ecosystem
    # see a one-liner pull command.
    body = step["with"]["body"]
    assert "docker pull" in body
    assert "${{ env.GHCR_IMAGE }}" in body
    assert "${{ env.DOCKERHUB_IMAGE }}" in body


def test_prerelease_detection_uses_hyphen_convention(workflow: dict[str, Any]) -> None:
    """``v1.0.0-rc1`` should land as a pre-release, ``v1.0.0`` as stable."""
    step = _step_by_uses(workflow, "softprops/action-gh-release")
    expr = step["with"]["prerelease"]
    assert "contains" in expr
    assert "github.ref_name" in expr
    assert "'-'" in expr


# -- Reproducibility ----------------------------------------------------


def test_actions_are_pinned_to_major_versions(workflow: dict[str, Any]) -> None:
    """No unpinned ``@main`` references — surprise breakage is bad on release day."""
    for step in workflow["jobs"]["release"]["steps"]:
        uses = step.get("uses")
        if uses is None:
            continue
        ref = uses.split("@", 1)[1] if "@" in uses else ""
        assert ref, f"unpinned action: {uses}"
        assert ref != "main", f"action {uses} pinned to a moving branch"


def test_full_history_fetched_for_changelog(workflow: dict[str, Any]) -> None:
    """``fetch-depth: 0`` — release notes diff against the prior tag."""
    step = _step_by_uses(workflow, "actions/checkout")
    assert step["with"]["fetch-depth"] == 0
