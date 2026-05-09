"""Tests for :meth:`RegistryClient.delete_manifest`."""

from __future__ import annotations

import httpx
import pytest

from layerloupe.registry import (
    MANIFEST_ACCEPT_HEADER,
    RegistryClient,
    RegistryError,
    RegistryHTTPError,
)
from layerloupe.registry.client import _looks_like_digest

DIGEST = "sha256:b5b2b2c507a0944348e0303114d8d93aaaa081732b86451d9bce1f432a537bc7"


# -- _looks_like_digest helper --------------------------------------------


@pytest.mark.parametrize(
    ("reference", "expected"),
    [
        ("sha256:abc123", True),
        ("sha256:" + "a" * 64, True),
        ("sha512:" + "f" * 128, True),
        ("latest", False),
        ("v1.2.3", False),
        ("22.04", False),
        ("", False),
        ("sha256:", False),  # missing hex
        ("sha256:not-hex!", False),
        (":abc", False),  # missing algorithm
    ],
)
def test_looks_like_digest(reference: str, expected: bool) -> None:
    assert _looks_like_digest(reference) is expected


# -- delete_manifest with tag → HEAD then DELETE on digest ----------------


async def test_delete_by_tag_resolves_digest_and_deletes_it() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "HEAD":
            assert request.headers.get("accept") == MANIFEST_ACCEPT_HEADER
            return httpx.Response(200, headers={"docker-content-digest": DIGEST})
        if request.method == "DELETE":
            return httpx.Response(202)
        return httpx.Response(405)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        deleted = await client.delete_manifest("library/ubuntu", "latest")

    assert deleted == DIGEST
    assert requests == [
        ("HEAD", "/v2/library/ubuntu/manifests/latest"),
        ("DELETE", f"/v2/library/ubuntu/manifests/{DIGEST}"),
    ]


async def test_delete_by_digest_skips_head() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(202)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        deleted = await client.delete_manifest("foo", DIGEST)

    assert deleted == DIGEST
    assert requests == [("DELETE", f"/v2/foo/manifests/{DIGEST}")]


async def test_delete_by_tag_missing_digest_header_raises() -> None:
    """Some non-conformant registries don't set Docker-Content-Digest on HEAD."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200)  # no digest header
        return httpx.Response(202)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryError, match="Docker-Content-Digest"):
            await client.delete_manifest("foo", "latest")


async def test_delete_by_tag_404_on_head_propagates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.delete_manifest("foo", "missing-tag")
    assert exc_info.value.status_code == 404


async def test_delete_405_when_registry_disallows_deletes() -> None:
    """Most default registry deployments reject DELETE with 405."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(200, headers={"docker-content-digest": DIGEST})
        return httpx.Response(
            405,
            json={"errors": [{"code": "UNSUPPORTED", "message": "deletion is disabled"}]},
        )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.delete_manifest("foo", "latest")
    assert exc_info.value.status_code == 405


async def test_delete_by_digest_404_propagates() -> None:
    """Already-deleted manifest should raise so caller can show 'not found'."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.delete_manifest("foo", DIGEST)
    assert exc_info.value.status_code == 404


async def test_delete_by_tag_with_nested_repo_path() -> None:
    """Repos with slashes (library/ubuntu) must round-trip correctly."""
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(f"{request.method} {request.url.path}")
        if request.method == "HEAD":
            return httpx.Response(200, headers={"docker-content-digest": DIGEST})
        return httpx.Response(202)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.delete_manifest("team/project/service", "v1.2.3")

    assert requests == [
        "HEAD /v2/team/project/service/manifests/v1.2.3",
        f"DELETE /v2/team/project/service/manifests/{DIGEST}",
    ]
