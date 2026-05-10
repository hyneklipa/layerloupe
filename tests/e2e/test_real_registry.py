"""End-to-end tests against a live ``registry:2``.

These run **only** when ``E2E_REGISTRY`` points at a reachable
registry (``http://localhost:5000`` in our GitHub Actions e2e job). They
exercise the full ``RegistryClient`` against real protocol traffic —
catching the kinds of regressions that mock transports can't see, like
header casing, redirect chains, and how the registry actually behaves on
DELETE.

The CI seed (``.github/workflows/e2e.yml``) populates the registry with:

* ``alpine:3.20``      — multi-arch index (Docker manifest list).
* ``alpine:latest``    — multi-arch index (Docker manifest list).
* ``alpine:3.19``      — multi-arch index (also).
* ``hello-world:latest`` — multi-arch index, very small.
* ``scratch/delete-me:1.0`` — a single-tag throwaway for the delete test.

Tests assume those fixtures exist and are read-only **except** for the
``scratch/delete-me`` repo, which the delete test consumes.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest

from layerloupe.registry import (
    ManifestKind,
    RegistryClient,
    RegistryHTTPError,
)
from layerloupe.utils import sort_tags

E2E_REGISTRY = os.environ.get("E2E_REGISTRY")

pytestmark = pytest.mark.skipif(
    E2E_REGISTRY is None,
    reason="set E2E_REGISTRY=<url> to enable live-registry tests",
)


@pytest.fixture
async def client() -> AsyncIterator[RegistryClient]:
    """Fresh client per test; ``verify=False`` because the e2e registry is plain HTTP."""
    assert E2E_REGISTRY is not None  # narrowed by the module-level skip
    async with RegistryClient(E2E_REGISTRY, verify=False) as c:
        yield c


# -- Liveness / catalog -------------------------------------------------


async def test_probe_returns_authenticated(client: RegistryClient) -> None:
    """``registry:2`` runs without auth; probe should succeed and report the version header."""
    probe = await client.probe()
    assert probe.reachable is True
    assert probe.authenticated is True
    assert probe.version is not None
    assert probe.version.startswith("registry/")


async def test_catalog_lists_seeded_repos(client: RegistryClient) -> None:
    repos = [r async for r in client.iter_repositories()]
    assert "alpine" in repos
    assert "hello-world" in repos
    assert "scratch/delete-me" in repos


async def test_catalog_filter_returns_matching_subset(client: RegistryClient) -> None:
    """Substring filter is case-insensitive."""
    repos = [r async for r in client.iter_repositories(query="ALP")]
    assert "alpine" in repos
    # ``hello-world`` is not "alp"-y.
    assert "hello-world" not in repos


# -- Tag listing + smart sort -------------------------------------------


async def test_alpine_tags_smart_sorted(client: RegistryClient) -> None:
    """``latest`` should pin to the top, semver descending below it."""
    raw = [t async for t in client.iter_tags("alpine")]
    sorted_tags = sort_tags(raw)
    # ``latest`` is the head of the list.
    assert sorted_tags[0] == "latest"
    # ``3.20`` outranks ``3.19`` numerically (despite both being in the list).
    assert sorted_tags.index("3.20") < sorted_tags.index("3.19")


# -- Manifest fetch (multi-arch index + child) --------------------------


async def test_multi_arch_index_classifies_correctly(client: RegistryClient) -> None:
    """Real ``alpine:latest`` is a manifest list; classifier recognizes it."""
    manifest = await client.get_manifest("alpine", "latest")
    assert manifest.digest is not None
    assert manifest.kind in (ManifestKind.OCI_INDEX, ManifestKind.DOCKER_LIST)
    assert manifest.is_index is True

    # The index lists multiple platforms.
    children = manifest.body.get("manifests", [])
    archs = {
        m["platform"]["architecture"] for m in children if isinstance(m, dict) and "platform" in m
    }
    assert "amd64" in archs


async def test_child_manifest_and_config_blob_round_trip(
    client: RegistryClient,
) -> None:
    """Pick the amd64 child of ``alpine:latest`` and walk into its config blob."""
    index = await client.get_manifest("alpine", "latest")
    children = index.body.get("manifests", [])
    amd64 = next(m for m in children if m.get("platform", {}).get("architecture") == "amd64")

    child = await client.get_manifest("alpine", amd64["digest"])
    assert child.kind in (ManifestKind.OCI_IMAGE, ManifestKind.DOCKER_V2)
    assert child.is_index is False
    assert child.body.get("layers")  # there should be at least one filesystem layer

    config = await client.get_image_config("alpine", child)
    assert config.architecture == "amd64"
    assert config.os == "linux"


async def test_404_for_missing_manifest(client: RegistryClient) -> None:
    """The registry sends a real 404 for unknown tags; the client surfaces it as such."""
    with pytest.raises(RegistryHTTPError) as exc_info:
        await client.get_manifest("alpine", "this-tag-does-not-exist")
    assert exc_info.value.status_code == 404


# -- Referrers (registry:2 doesn't implement OCI 1.1 → soft-fail) -------


async def test_referrers_soft_fail_when_unsupported(client: RegistryClient) -> None:
    """``registry:2.8`` doesn't implement ``/v2/<name>/referrers/<digest>``;
    the client must treat that as an empty list, not an error."""
    manifest = await client.get_manifest("alpine", "latest")
    assert manifest.digest is not None
    referrers = await client.get_referrers("alpine", manifest.digest)
    assert referrers == []


# -- Delete (destructive — uses the dedicated scratch repo) -------------


async def test_delete_actually_removes_manifest(client: RegistryClient) -> None:
    """Run last (alphabetical order). Deletes ``scratch/delete-me:1.0``,
    then confirms a follow-up fetch returns 404."""
    # Sanity: it's still there before we delete.
    pre = await client.get_manifest("scratch/delete-me", "1.0")
    assert pre.digest is not None
    pre_digest = pre.digest

    deleted = await client.delete_manifest("scratch/delete-me", "1.0")
    assert deleted == pre_digest  # the resolved digest, not the tag

    # The registry's eventual-consistency window can be a beat or two; the
    # tag should already be unresolved on the next fetch attempt either way.
    with pytest.raises(RegistryHTTPError) as exc_info:
        await client.get_manifest("scratch/delete-me", "1.0")
    assert exc_info.value.status_code == 404


# -- Pagination plumbing (quick sanity — most pages will be tiny in CI) -


async def test_pagination_yields_every_repo(client: RegistryClient) -> None:
    """Force tiny page size so the ``Link``/``last=`` cursor logic actually runs."""
    repos = [r async for r in client.iter_repositories(page_size=1)]
    # All four seed repos must come through.
    for expected in ("alpine", "hello-world", "scratch/delete-me"):
        assert expected in repos
