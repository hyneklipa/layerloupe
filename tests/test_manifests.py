"""Tests for manifest fetch, classification, and the unified accept header."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from layerloupe.registry import (
    MANIFEST_ACCEPT_HEADER,
    MANIFEST_ACCEPT_TYPES,
    ManifestKind,
    MediaType,
    RegistryClient,
    RegistryHTTPError,
    classify_media_type,
)
from tests.conftest import load_fixture_bytes

# -- Media type classifier ------------------------------------------------


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (MediaType.OCI_IMAGE_INDEX.value, ManifestKind.OCI_INDEX),
        (MediaType.OCI_IMAGE_MANIFEST.value, ManifestKind.OCI_IMAGE),
        (MediaType.DOCKER_MANIFEST_LIST.value, ManifestKind.DOCKER_LIST),
        (MediaType.DOCKER_MANIFEST_V2.value, ManifestKind.DOCKER_V2),
        (MediaType.DOCKER_MANIFEST_V1_SIGNED.value, ManifestKind.DOCKER_V1),
        (MediaType.DOCKER_MANIFEST_V1.value, ManifestKind.DOCKER_V1),
        (
            "application/vnd.oci.image.index.v1+json; charset=utf-8",
            ManifestKind.OCI_INDEX,
        ),
        ("APPLICATION/VND.OCI.IMAGE.MANIFEST.V1+JSON", ManifestKind.OCI_IMAGE),
        ("application/json", ManifestKind.UNKNOWN),
        ("", ManifestKind.UNKNOWN),
        (None, ManifestKind.UNKNOWN),
    ],
)
def test_classify_media_type(content_type: str | None, expected: ManifestKind) -> None:
    assert classify_media_type(content_type) is expected


def test_manifest_accept_header_includes_all_six_types() -> None:
    """The header must list every modern manifest type — order = preference."""
    assert len(MANIFEST_ACCEPT_TYPES) == 6
    for media_type in MediaType:
        assert media_type.value in MANIFEST_ACCEPT_HEADER


def test_manifest_accept_header_oci_first() -> None:
    """OCI-first ordering: registries that support OCI shouldn't downgrade."""
    types = MANIFEST_ACCEPT_HEADER.split(", ")
    assert types[0] == MediaType.OCI_IMAGE_INDEX.value
    assert types[1] == MediaType.OCI_IMAGE_MANIFEST.value


def test_manifest_kind_is_index() -> None:
    assert ManifestKind.OCI_INDEX.is_index
    assert ManifestKind.DOCKER_LIST.is_index
    assert not ManifestKind.OCI_IMAGE.is_index
    assert not ManifestKind.DOCKER_V2.is_index
    assert not ManifestKind.DOCKER_V1.is_index
    assert not ManifestKind.UNKNOWN.is_index


# -- End-to-end get_manifest with fixtures --------------------------------


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _manifest_handler(
    fixture_name: str,
    content_type: str,
    *,
    include_digest: bool = True,
) -> tuple[list[dict[str, str]], Callable[[httpx.Request], httpx.Response]]:
    raw = load_fixture_bytes(fixture_name)
    digest = _digest_of(raw)
    requests: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(
            {
                "method": request.method,
                "path": request.url.path,
                "accept": request.headers.get("accept", ""),
            }
        )
        headers: dict[str, str] = {"content-type": content_type}
        if include_digest:
            headers["docker-content-digest"] = digest
        return httpx.Response(200, content=raw, headers=headers)

    return requests, handler


@pytest.mark.parametrize(
    ("fixture", "content_type", "expected_kind"),
    [
        ("manifest_v2", MediaType.DOCKER_MANIFEST_V2.value, ManifestKind.DOCKER_V2),
        ("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value, ManifestKind.OCI_IMAGE),
        ("manifest_index", MediaType.OCI_IMAGE_INDEX.value, ManifestKind.OCI_INDEX),
        (
            "manifest_docker_list",
            MediaType.DOCKER_MANIFEST_LIST.value,
            ManifestKind.DOCKER_LIST,
        ),
        ("manifest_v1", MediaType.DOCKER_MANIFEST_V1_SIGNED.value, ManifestKind.DOCKER_V1),
    ],
)
async def test_get_manifest_classifies_each_fixture(
    fixture: str, content_type: str, expected_kind: ManifestKind
) -> None:
    _requests, handler = _manifest_handler(fixture, content_type)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        manifest = await client.get_manifest("foo", "latest")

    assert manifest.kind is expected_kind
    assert manifest.media_type == content_type
    assert manifest.digest is not None
    assert manifest.digest.startswith("sha256:")
    assert manifest.body == manifest.body  # parses as dict
    assert isinstance(manifest.raw_body, bytes)
    # The raw bytes round-trip to the same digest the registry advertised.
    assert _digest_of(manifest.raw_body) == manifest.digest


async def test_get_manifest_sends_full_accept_header() -> None:
    requests, handler = _manifest_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_manifest("foo", "latest")

    assert len(requests) == 1
    accept = requests[0]["accept"]
    for media_type in MANIFEST_ACCEPT_TYPES:
        assert media_type in accept


async def test_get_manifest_uses_repo_and_reference_in_path() -> None:
    requests, handler = _manifest_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_manifest("library/ubuntu", "22.04")

    assert requests[0]["path"] == "/v2/library/ubuntu/manifests/22.04"


async def test_get_manifest_with_digest_reference() -> None:
    requests, handler = _manifest_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)
    digest = "sha256:abc123"

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_manifest("foo", digest)

    assert requests[0]["path"] == f"/v2/foo/manifests/{digest}"


async def test_get_manifest_strips_content_type_parameters() -> None:
    _requests, handler = _manifest_handler(
        "manifest_oci", "application/vnd.oci.image.manifest.v1+json; charset=utf-8"
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        manifest = await client.get_manifest("foo", "latest")

    # Stored without parameters, classifier still recognizes it.
    assert manifest.media_type == "application/vnd.oci.image.manifest.v1+json"
    assert manifest.kind is ManifestKind.OCI_IMAGE


async def test_get_manifest_handles_missing_digest_header() -> None:
    _requests, handler = _manifest_handler(
        "manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value, include_digest=False
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        manifest = await client.get_manifest("foo", "latest")

    assert manifest.digest is None
    assert manifest.kind is ManifestKind.OCI_IMAGE


async def test_get_manifest_unknown_content_type_falls_through() -> None:
    _requests, handler = _manifest_handler("manifest_oci", "application/json")

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        manifest = await client.get_manifest("foo", "latest")

    assert manifest.kind is ManifestKind.UNKNOWN
    assert manifest.body  # we still parse the JSON body


async def test_get_manifest_404_raises_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_manifest("foo", "missing-tag")
    assert exc_info.value.status_code == 404


async def test_get_manifest_non_json_body_raises_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html>not json</html>",
            headers={"content-type": MediaType.OCI_IMAGE_MANIFEST.value},
        )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError, match="not valid JSON"):
            await client.get_manifest("foo", "latest")


async def test_get_manifest_array_body_raises_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=["not", "an", "object"],
            headers={"content-type": MediaType.OCI_IMAGE_MANIFEST.value},
        )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError, match="not a JSON object"):
            await client.get_manifest("foo", "latest")


# -- Fixture sanity -------------------------------------------------------


def test_fixture_v2_has_expected_shape(manifest_v2: dict[str, Any]) -> None:
    assert manifest_v2["schemaVersion"] == 2
    assert manifest_v2["mediaType"] == MediaType.DOCKER_MANIFEST_V2.value
    assert "config" in manifest_v2
    assert isinstance(manifest_v2["layers"], list)


def test_fixture_oci_has_annotations(manifest_oci: dict[str, Any]) -> None:
    assert manifest_oci["mediaType"] == MediaType.OCI_IMAGE_MANIFEST.value
    assert "org.opencontainers.image.source" in manifest_oci["annotations"]


def test_fixture_index_has_multiple_platforms(manifest_index: dict[str, Any]) -> None:
    assert manifest_index["mediaType"] == MediaType.OCI_IMAGE_INDEX.value
    archs = {m["platform"]["architecture"] for m in manifest_index["manifests"]}
    assert archs == {"amd64", "arm64"}


def test_fixture_v1_has_history_with_v1compatibility(manifest_v1: dict[str, Any]) -> None:
    assert manifest_v1["schemaVersion"] == 1
    assert all("v1Compatibility" in entry for entry in manifest_v1["history"])
