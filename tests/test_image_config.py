"""Tests for blob fetching and image config parsing."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from layerloupe.registry import (
    ImageConfig,
    ManifestKind,
    ManifestResponse,
    MediaType,
    RegistryClient,
    RegistryError,
    RegistryHTTPError,
)
from tests.conftest import load_fixture, load_fixture_bytes

# -- ImageConfig model ----------------------------------------------------


def test_image_config_parses_full_fixture(image_config: dict[str, Any]) -> None:
    config = ImageConfig.model_validate(image_config)

    assert config.architecture == "amd64"
    assert config.os == "linux"
    assert config.author == "LayerLoupe test fixtures"

    # ContainerConfig's CamelCase JSON fields → snake_case Python fields.
    assert config.config.cmd == ["/bin/bash"]
    assert config.config.entrypoint == ["/usr/local/bin/entrypoint.sh"]
    assert config.config.working_dir == "/app"
    assert config.config.exposed_ports == {"8080/tcp": {}}
    assert config.config.volumes == {"/data": {}}
    assert config.config.labels == {
        "org.opencontainers.image.source": "https://github.com/example/repo",
        "org.opencontainers.image.version": "1.2.3",
    }

    assert config.rootfs is not None
    assert config.rootfs.type == "layers"
    assert len(config.rootfs.diff_ids) == 2

    assert len(config.history) == 3
    assert config.history[2].empty_layer is True
    assert config.history[1].comment == "install curl"


def test_image_config_minimal() -> None:
    """A minimum-viable image config — just architecture and os."""
    config = ImageConfig.model_validate({"architecture": "arm64", "os": "linux"})
    assert config.architecture == "arm64"
    assert config.os == "linux"
    assert config.config.cmd is None
    assert config.history == []
    assert config.rootfs is None


def test_image_config_ignores_unknown_fields() -> None:
    """Real-world configs include vendor-specific fields — must not raise."""
    raw = {
        "architecture": "amd64",
        "os": "linux",
        "vendor.specific.field": "should be ignored",
        "config": {"User": "nobody", "ExtraThingy": "ignored"},
    }
    config = ImageConfig.model_validate(raw)
    assert config.config.user == "nobody"


def test_image_config_accepts_snake_case_input() -> None:
    """populate_by_name=True lets us round-trip through dict() output."""
    raw = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"cmd": ["echo", "hi"], "working_dir": "/tmp"},
    }
    config = ImageConfig.model_validate(raw)
    assert config.config.cmd == ["echo", "hi"]
    assert config.config.working_dir == "/tmp"


def test_image_config_rejects_missing_required_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ImageConfig.model_validate({"os": "linux"})  # missing architecture


# -- get_blob -------------------------------------------------------------


async def test_get_blob_returns_raw_response() -> None:
    payload = b'{"hello": "world"}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/foo/blobs/sha256:abc"
        return httpx.Response(200, content=payload)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        response = await client.get_blob("foo", "sha256:abc")

    assert response.status_code == 200
    assert response.content == payload


async def test_get_blob_404_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "BLOB_UNKNOWN"}]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_blob("foo", "sha256:missing")
    assert exc_info.value.status_code == 404


# -- get_image_config end-to-end ------------------------------------------


def _manifest_response(fixture_name: str, kind: ManifestKind, media_type: str) -> ManifestResponse:
    body = load_fixture(fixture_name)
    return ManifestResponse(
        digest="sha256:dummy",
        media_type=media_type,
        kind=kind,
        body=body,
        raw_body=load_fixture_bytes(fixture_name),
    )


async def test_get_image_config_from_oci_manifest() -> None:
    """End-to-end: OCI manifest → fetch config blob → parse as ImageConfig."""
    manifest = _manifest_response(
        "manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value
    )
    config_digest = manifest.body["config"]["digest"]
    config_bytes = load_fixture_bytes("image_config")

    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(request.url.path)
        if request.url.path == f"/v2/library/example/blobs/{config_digest}":
            return httpx.Response(200, content=config_bytes)
        return httpx.Response(404)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        config = await client.get_image_config("library/example", manifest)

    assert isinstance(config, ImageConfig)
    assert config.architecture == "amd64"
    assert config.config.cmd == ["/bin/bash"]
    assert requested_paths == [f"/v2/library/example/blobs/{config_digest}"]


async def test_get_image_config_from_docker_v2_manifest() -> None:
    manifest = _manifest_response(
        "manifest_v2", ManifestKind.DOCKER_V2, MediaType.DOCKER_MANIFEST_V2.value
    )
    config_bytes = load_fixture_bytes("image_config")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=config_bytes)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        config = await client.get_image_config("foo", manifest)

    assert config.architecture == "amd64"
    assert config.os == "linux"


async def test_get_image_config_from_index_raises() -> None:
    manifest = _manifest_response(
        "manifest_index", ManifestKind.OCI_INDEX, MediaType.OCI_IMAGE_INDEX.value
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    ) as client:
        with pytest.raises(RegistryError, match=r"index|manifest list"):
            await client.get_image_config("foo", manifest)


async def test_get_image_config_from_docker_list_raises() -> None:
    manifest = _manifest_response(
        "manifest_docker_list",
        ManifestKind.DOCKER_LIST,
        MediaType.DOCKER_MANIFEST_LIST.value,
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    ) as client:
        with pytest.raises(RegistryError, match=r"index|manifest list"):
            await client.get_image_config("foo", manifest)


async def test_get_image_config_from_schema_1_raises() -> None:
    manifest = _manifest_response(
        "manifest_v1", ManifestKind.DOCKER_V1, MediaType.DOCKER_MANIFEST_V1_SIGNED.value
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    ) as client:
        with pytest.raises(RegistryError, match=r"[Ss]chema 1"):
            await client.get_image_config("foo", manifest)


async def test_get_image_config_missing_config_section_raises() -> None:
    manifest = ManifestResponse(
        digest="sha256:dummy",
        media_type=MediaType.OCI_IMAGE_MANIFEST.value,
        kind=ManifestKind.OCI_IMAGE,
        body={"schemaVersion": 2, "layers": []},  # no 'config' key
        raw_body=b"{}",
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    ) as client:
        with pytest.raises(RegistryError, match="no 'config' object"):
            await client.get_image_config("foo", manifest)


async def test_get_image_config_missing_digest_raises() -> None:
    manifest = ManifestResponse(
        digest="sha256:dummy",
        media_type=MediaType.OCI_IMAGE_MANIFEST.value,
        kind=ManifestKind.OCI_IMAGE,
        body={"config": {"mediaType": "application/json", "size": 100}},  # no digest
        raw_body=b"{}",
    )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    ) as client:
        with pytest.raises(RegistryError, match="missing a digest"):
            await client.get_image_config("foo", manifest)


async def test_get_image_config_non_json_blob_raises() -> None:
    manifest = _manifest_response(
        "manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError, match="not valid JSON"):
            await client.get_image_config("foo", manifest)


async def test_get_image_config_array_blob_raises() -> None:
    manifest = _manifest_response(
        "manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps([1, 2, 3]).encode())

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError, match="not a JSON object"):
            await client.get_image_config("foo", manifest)
