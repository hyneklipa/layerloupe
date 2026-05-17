"""Tests for :func:`to_unified` - every fixture round-trips into a UnifiedManifest."""

from __future__ import annotations

from typing import Any

import pytest

from layerloupe.registry import (
    ImageConfig,
    ManifestKind,
    ManifestResponse,
    MediaType,
    RegistryError,
    UnifiedManifest,
    UnifiedPlatform,
    to_unified,
)
from tests.conftest import load_fixture, load_fixture_bytes


def _mr(fixture: str, kind: ManifestKind, media_type: str) -> ManifestResponse:
    return ManifestResponse(
        digest="sha256:fixturedigest",
        media_type=media_type,
        kind=kind,
        body=load_fixture(fixture),
        raw_body=load_fixture_bytes(fixture),
    )


@pytest.fixture
def oci_image_config(image_config: dict[str, Any]) -> ImageConfig:
    return ImageConfig.model_validate(image_config)


# -- OCI image (single-arch) ----------------------------------------------


def test_to_unified_oci_image_with_config(oci_image_config: ImageConfig) -> None:
    mr = _mr("manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value)
    body = mr.body
    expected_size = sum(layer["size"] for layer in body["layers"])

    unified = to_unified(mr, oci_image_config, pull_command="docker pull foo:latest")

    assert isinstance(unified, UnifiedManifest)
    assert unified.digest == "sha256:fixturedigest"
    assert unified.media_type == MediaType.OCI_IMAGE_MANIFEST.value
    assert unified.schema_version == 2
    assert unified.type == "image"
    assert unified.size == expected_size

    assert unified.platforms == [
        UnifiedPlatform(architecture="amd64", os="linux", variant=None, digest=None)
    ]

    assert unified.config is not None
    assert unified.config.digest == body["config"]["digest"]
    assert unified.config.size == body["config"]["size"]
    assert unified.config.data is oci_image_config

    assert len(unified.layers) == len(body["layers"])
    assert all(layer.size > 0 for layer in unified.layers)
    assert unified.layers[0].digest == body["layers"][0]["digest"]

    assert unified.annotations["org.opencontainers.image.licenses"] == "Apache-2.0"
    assert unified.pull_command == "docker pull foo:latest"


def test_to_unified_oci_image_without_config_yields_no_platforms() -> None:
    """Without a fetched image_config we don't know architecture/os - empty platforms."""
    mr = _mr("manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value)
    unified = to_unified(mr)

    assert unified.platforms == []
    assert unified.config is not None
    assert unified.config.data is None
    assert unified.layers  # layers still parse


def test_to_unified_oci_image_passes_subject_through() -> None:
    """OCI 1.1 ``subject`` round-trips through the unified model."""
    body = load_fixture("manifest_oci")
    body["subject"] = {
        "mediaType": MediaType.OCI_IMAGE_MANIFEST.value,
        "digest": "sha256:parent",
        "size": 1234,
    }
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.OCI_IMAGE_MANIFEST.value,
        kind=ManifestKind.OCI_IMAGE,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert unified.subject is not None
    assert unified.subject.digest == "sha256:parent"


# -- Docker manifest v2 (single-arch) -------------------------------------


def test_to_unified_docker_v2(oci_image_config: ImageConfig) -> None:
    mr = _mr("manifest_v2", ManifestKind.DOCKER_V2, MediaType.DOCKER_MANIFEST_V2.value)
    expected_size = sum(layer["size"] for layer in mr.body["layers"])

    unified = to_unified(mr, oci_image_config)

    assert unified.type == "image"
    assert unified.size == expected_size
    assert len(unified.layers) == 3
    assert unified.config is not None
    assert unified.config.digest == mr.body["config"]["digest"]
    # Docker v2 manifests don't have annotations.
    assert unified.annotations == {}


# -- OCI Image Index (multi-arch) -----------------------------------------


def test_to_unified_oci_index() -> None:
    mr = _mr("manifest_index", ManifestKind.OCI_INDEX, MediaType.OCI_IMAGE_INDEX.value)

    unified = to_unified(mr)

    assert unified.type == "index"
    assert unified.config is None
    assert unified.layers == []
    assert len(unified.platforms) == 2

    archs = {p.architecture for p in unified.platforms}
    assert archs == {"amd64", "arm64"}

    arm = next(p for p in unified.platforms if p.architecture == "arm64")
    assert arm.variant == "v8"
    assert arm.digest is not None  # points at child manifest

    expected_total_size = sum(m["size"] for m in mr.body["manifests"])
    assert unified.size == expected_total_size

    assert unified.annotations.get("com.example.key1") == "value1"


def test_to_unified_index_platform_digest_points_at_child() -> None:
    mr = _mr("manifest_index", ManifestKind.OCI_INDEX, MediaType.OCI_IMAGE_INDEX.value)
    unified = to_unified(mr)
    expected_digests = {m["digest"] for m in mr.body["manifests"]}
    actual_digests = {p.digest for p in unified.platforms}
    assert actual_digests == expected_digests


def test_to_unified_index_with_missing_platform_falls_back_to_unknown() -> None:
    """Some indexes (sparse/buggy) omit platform; we mark it ``unknown``."""
    body = {
        "schemaVersion": 2,
        "mediaType": MediaType.OCI_IMAGE_INDEX.value,
        "manifests": [
            {
                "mediaType": MediaType.OCI_IMAGE_MANIFEST.value,
                "digest": "sha256:abc",
                "size": 100,
            }
        ],
    }
    mr = ManifestResponse(
        digest="sha256:idx",
        media_type=MediaType.OCI_IMAGE_INDEX.value,
        kind=ManifestKind.OCI_INDEX,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert len(unified.platforms) == 1
    assert unified.platforms[0].architecture == "unknown"
    assert unified.platforms[0].os == "unknown"
    assert unified.platforms[0].digest == "sha256:abc"


# -- Docker Manifest List (multi-arch) ------------------------------------


def test_to_unified_docker_list() -> None:
    mr = _mr(
        "manifest_docker_list",
        ManifestKind.DOCKER_LIST,
        MediaType.DOCKER_MANIFEST_LIST.value,
    )

    unified = to_unified(mr)

    assert unified.type == "index"
    assert len(unified.platforms) == 2
    arm = next(p for p in unified.platforms if p.architecture == "arm")
    assert arm.variant == "v7"
    # Docker list has no top-level annotations field.
    assert unified.annotations == {}


# -- Schema 1 (legacy) ----------------------------------------------------


def test_to_unified_schema_1_synthesizes_config_from_v1compatibility() -> None:
    """Schema 1: image config is reconstructed from history[0].v1Compatibility."""
    mr = _mr("manifest_v1", ManifestKind.DOCKER_V1, MediaType.DOCKER_MANIFEST_V1_SIGNED.value)

    unified = to_unified(mr)

    assert unified.type == "image"
    assert unified.schema_version == 1
    assert unified.size == 0  # schema 1 has no per-layer size info

    # Config now populated with synthesized data; no separate blob → digest is None.
    assert unified.config is not None
    assert unified.config.digest is None
    assert unified.config.size == 0
    assert unified.config.data is not None
    assert unified.config.data.architecture == "amd64"
    assert unified.config.data.os == "linux"

    # History was decoded - both entries surface with created_by from container_config.Cmd.
    assert len(unified.config.data.history) == 2
    assert unified.config.data.history[0].created_by is not None
    assert "CMD" in unified.config.data.history[0].created_by
    assert "ADD" in (unified.config.data.history[1].created_by or "")

    assert len(unified.platforms) == 1
    assert unified.platforms[0].architecture == "amd64"
    assert unified.platforms[0].os == "linux"
    assert len(unified.layers) == len(mr.body["fsLayers"])


def test_to_unified_schema_1_falls_back_to_container_config() -> None:
    """Most schema 1 manifests in the wild populate only ``container_config``."""
    mr = _mr("manifest_v1", ManifestKind.DOCKER_V1, MediaType.DOCKER_MANIFEST_V1_SIGNED.value)
    unified = to_unified(mr)

    # The fixture's v1Compatibility has only container_config (no `config`).
    # The synthesized ImageConfig.config should pick up its Cmd.
    assert unified.config is not None
    assert unified.config.data is not None
    assert unified.config.data.config.cmd is not None
    assert any("/bin/sh" in c for c in unified.config.data.config.cmd)


def test_to_unified_schema_1_prefers_image_config_over_container_config() -> None:
    """When both `config` and `container_config` are present, prefer `config`."""
    body = {
        "schemaVersion": 1,
        "name": "library/ubuntu",
        "tag": "16.04",
        "architecture": "amd64",
        "fsLayers": [],
        "history": [
            {
                "v1Compatibility": (
                    '{"id":"a","created":"2017-06-23T21:54:48Z",'
                    '"config":{"Cmd":["from-image-config"]},'
                    '"container_config":{"Cmd":["from-container-config"]}}'
                )
            }
        ],
    }
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
        kind=ManifestKind.DOCKER_V1,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert unified.config is not None
    assert unified.config.data is not None
    assert unified.config.data.config.cmd == ["from-image-config"]


def test_to_unified_schema_1_throwaway_marks_empty_layer() -> None:
    body = {
        "schemaVersion": 1,
        "name": "foo",
        "architecture": "amd64",
        "fsLayers": [],
        "history": [
            {
                "v1Compatibility": (
                    '{"id":"a","created":"2017-06-23T21:54:48Z",'
                    '"throwaway":true,"container_config":{"Cmd":["#(nop) CMD"]}}'
                )
            }
        ],
    }
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
        kind=ManifestKind.DOCKER_V1,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert unified.config is not None
    assert unified.config.data is not None
    assert unified.config.data.history[0].empty_layer is True


def test_to_unified_schema_1_empty_history_yields_no_config() -> None:
    body = {
        "schemaVersion": 1,
        "name": "foo",
        "architecture": "amd64",
        "fsLayers": [{"blobSum": "sha256:abc"}],
        "history": [],
    }
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
        kind=ManifestKind.DOCKER_V1,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert unified.config is None
    assert unified.platforms[0].architecture == "amd64"  # falls back to manifest field
    assert unified.platforms[0].os == "linux"
    assert len(unified.layers) == 1


def test_to_unified_schema_1_malformed_v1compatibility_returns_no_config() -> None:
    body = {
        "schemaVersion": 1,
        "name": "foo",
        "architecture": "amd64",
        "fsLayers": [],
        "history": [{"v1Compatibility": "not json at all"}],
    }
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
        kind=ManifestKind.DOCKER_V1,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert unified.config is None
    # Architecture falls back to manifest-level field, still useful.
    assert unified.platforms[0].architecture == "amd64"


def test_to_unified_schema_1_uses_v1_architecture_when_present() -> None:
    """If v1Compatibility specifies architecture, prefer that over manifest top-level."""
    body = {
        "schemaVersion": 1,
        "name": "foo",
        "architecture": "amd64",
        "fsLayers": [],
        "history": [
            {
                "v1Compatibility": (
                    '{"architecture":"arm64","os":"linux",'
                    '"created":"2017-06-23T21:54:48Z","container_config":{}}'
                )
            }
        ],
    }
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
        kind=ManifestKind.DOCKER_V1,
        body=body,
        raw_body=b"{}",
    )
    unified = to_unified(mr)
    assert unified.platforms[0].architecture == "arm64"


# -- Pull command pass-through --------------------------------------------


def test_to_unified_pull_command_optional() -> None:
    mr = _mr("manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value)
    assert to_unified(mr).pull_command is None
    assert to_unified(mr, pull_command="docker pull foo").pull_command == "docker pull foo"


# -- Unknown / unsupported kind -------------------------------------------


def test_to_unified_unknown_kind_raises() -> None:
    mr = ManifestResponse(
        digest=None,
        media_type="application/json",
        kind=ManifestKind.UNKNOWN,
        body={},
        raw_body=b"{}",
    )
    with pytest.raises(RegistryError, match="unknown content type"):
        to_unified(mr)


# -- End-to-end coverage check: every fixture -----------------------------


@pytest.mark.parametrize(
    ("fixture", "kind", "media_type", "expected_type"),
    [
        ("manifest_v2", ManifestKind.DOCKER_V2, MediaType.DOCKER_MANIFEST_V2.value, "image"),
        ("manifest_oci", ManifestKind.OCI_IMAGE, MediaType.OCI_IMAGE_MANIFEST.value, "image"),
        ("manifest_index", ManifestKind.OCI_INDEX, MediaType.OCI_IMAGE_INDEX.value, "index"),
        (
            "manifest_docker_list",
            ManifestKind.DOCKER_LIST,
            MediaType.DOCKER_MANIFEST_LIST.value,
            "index",
        ),
        (
            "manifest_v1",
            ManifestKind.DOCKER_V1,
            MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
            "image",
        ),
    ],
)
def test_to_unified_every_fixture_carries_expected_fields(
    fixture: str, kind: ManifestKind, media_type: str, expected_type: str
) -> None:
    mr = _mr(fixture, kind, media_type)
    unified = to_unified(mr)

    assert unified.type == expected_type
    # Every variant must produce these fields without error.
    assert unified.digest == "sha256:fixturedigest"
    assert unified.media_type == media_type
    assert isinstance(unified.size, int)
    assert isinstance(unified.platforms, list)
    assert isinstance(unified.layers, list)
    assert isinstance(unified.annotations, dict)
