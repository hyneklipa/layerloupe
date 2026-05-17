"""Pydantic models for the five manifest variants - happy path + edge cases.

Each fixture in ``tests/fixtures/`` should round-trip through its model
without losing data the rest of the app cares about.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from layerloupe.registry import (
    Descriptor,
    DockerManifestList,
    DockerManifestV2,
    DockerSchema1Manifest,
    FsLayer,
    IndexManifestEntry,
    OciImageIndex,
    OciImageManifest,
    Platform,
    V1HistoryEntry,
    V1Signature,
)

# -- Descriptor / Platform ------------------------------------------------


def test_descriptor_minimal_required_fields() -> None:
    desc = Descriptor.model_validate(
        {
            "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
            "digest": "sha256:abc",
            "size": 1234,
        }
    )
    assert desc.media_type == "application/vnd.oci.image.layer.v1.tar+gzip"
    assert desc.digest == "sha256:abc"
    assert desc.size == 1234
    assert desc.urls is None
    assert desc.annotations is None


def test_descriptor_rejects_missing_digest() -> None:
    with pytest.raises(ValidationError):
        Descriptor.model_validate({"mediaType": "x", "size": 1})


def test_platform_with_dotted_aliases() -> None:
    """``os.version`` and ``os.features`` are dotted JSON keys."""
    plat = Platform.model_validate(
        {
            "architecture": "amd64",
            "os": "windows",
            "os.version": "10.0.17763.1234",
            "os.features": ["win32k"],
            "variant": "v8",
        }
    )
    assert plat.os_version == "10.0.17763.1234"
    assert plat.os_features == ["win32k"]
    assert plat.variant == "v8"


def test_index_manifest_entry_carries_platform() -> None:
    entry = IndexManifestEntry.model_validate(
        {
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "digest": "sha256:abc",
            "size": 1234,
            "platform": {"architecture": "arm64", "os": "linux"},
        }
    )
    assert entry.platform is not None
    assert entry.platform.architecture == "arm64"


# -- OCI image manifest ---------------------------------------------------


def test_oci_manifest_parses_fixture(manifest_oci: dict[str, Any]) -> None:
    m = OciImageManifest.model_validate(manifest_oci)
    assert m.schema_version == 2
    assert m.media_type == "application/vnd.oci.image.manifest.v1+json"
    assert m.config.digest.startswith("sha256:")
    assert m.config.size == 1234
    assert len(m.layers) == 2
    assert all(layer.digest.startswith("sha256:") for layer in m.layers)
    assert m.annotations is not None
    assert m.annotations["org.opencontainers.image.licenses"] == "Apache-2.0"


def test_oci_manifest_subject_optional() -> None:
    """``subject`` is an OCI 1.1 addition - pre-1.1 manifests omit it."""
    m = OciImageManifest.model_validate(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "x", "digest": "sha256:c", "size": 1},
            "layers": [],
        }
    )
    assert m.subject is None


def test_oci_manifest_with_subject() -> None:
    m = OciImageManifest.model_validate(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "x", "digest": "sha256:c", "size": 1},
            "layers": [],
            "subject": {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:parent",
                "size": 5678,
            },
        }
    )
    assert m.subject is not None
    assert m.subject.digest == "sha256:parent"


def test_oci_manifest_rejects_missing_config() -> None:
    with pytest.raises(ValidationError):
        OciImageManifest.model_validate({"schemaVersion": 2, "layers": []})


# -- Docker manifest v2 ---------------------------------------------------


def test_docker_manifest_v2_parses_fixture(manifest_v2: dict[str, Any]) -> None:
    m = DockerManifestV2.model_validate(manifest_v2)
    assert m.schema_version == 2
    assert m.media_type == "application/vnd.docker.distribution.manifest.v2+json"
    assert len(m.layers) == 3
    assert m.config.media_type == "application/vnd.docker.container.image.v1+json"


def test_docker_manifest_v2_layer_sizes_round_trip(manifest_v2: dict[str, Any]) -> None:
    m = DockerManifestV2.model_validate(manifest_v2)
    expected_sizes = [layer["size"] for layer in manifest_v2["layers"]]
    assert [layer.size for layer in m.layers] == expected_sizes


# -- OCI Image Index ------------------------------------------------------


def test_oci_image_index_parses_fixture(manifest_index: dict[str, Any]) -> None:
    idx = OciImageIndex.model_validate(manifest_index)
    assert idx.schema_version == 2
    assert idx.media_type == "application/vnd.oci.image.index.v1+json"
    assert len(idx.manifests) == 2
    archs = {m.platform.architecture for m in idx.manifests if m.platform}
    assert archs == {"amd64", "arm64"}
    arm = next(m for m in idx.manifests if m.platform and m.platform.architecture == "arm64")
    assert arm.platform is not None
    assert arm.platform.variant == "v8"


def test_oci_image_index_annotations(manifest_index: dict[str, Any]) -> None:
    idx = OciImageIndex.model_validate(manifest_index)
    assert idx.annotations is not None
    assert idx.annotations["com.example.key1"] == "value1"


# -- Docker Manifest List -------------------------------------------------


def test_docker_manifest_list_parses_fixture(
    manifest_docker_list: dict[str, Any],
) -> None:
    lst = DockerManifestList.model_validate(manifest_docker_list)
    assert lst.schema_version == 2
    assert lst.media_type == "application/vnd.docker.distribution.manifest.list.v2+json"
    assert len(lst.manifests) == 2
    arm = next(m for m in lst.manifests if m.platform and m.platform.architecture == "arm")
    assert arm.platform is not None
    assert arm.platform.variant == "v7"


# -- Schema 1 -------------------------------------------------------------


def test_docker_schema_1_parses_fixture(manifest_v1: dict[str, Any]) -> None:
    m = DockerSchema1Manifest.model_validate(manifest_v1)
    assert m.schema_version == 1
    assert m.name == "library/ubuntu"
    assert m.tag == "16.04"
    assert m.architecture == "amd64"
    assert len(m.fs_layers) == 2
    assert all(isinstance(layer, FsLayer) for layer in m.fs_layers)
    assert all(layer.blob_sum.startswith("sha256:") for layer in m.fs_layers)
    assert len(m.history) == 2
    assert all(isinstance(entry, V1HistoryEntry) for entry in m.history)
    # v1Compatibility is left as a raw string here - the parser layer
    # JSON-decodes it on demand.
    assert all(entry.v1_compatibility.startswith("{") for entry in m.history)


def test_docker_schema_1_signatures_optional() -> None:
    m = DockerSchema1Manifest.model_validate(
        {
            "schemaVersion": 1,
            "name": "foo",
            "architecture": "amd64",
            "fsLayers": [],
            "history": [],
        }
    )
    assert m.signatures is None


def test_docker_schema_1_signature_shape(manifest_v1: dict[str, Any]) -> None:
    m = DockerSchema1Manifest.model_validate(manifest_v1)
    assert m.signatures is not None
    assert len(m.signatures) == 1
    sig = m.signatures[0]
    assert isinstance(sig, V1Signature)
    assert sig.header == {"alg": "ES256"}


# -- Snake-case input acceptance ------------------------------------------


def test_models_accept_snake_case_input() -> None:
    """populate_by_name=True lets callers pass already-Pythonic dicts."""
    m = OciImageManifest.model_validate(
        {
            "schema_version": 2,
            "media_type": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "media_type": "application/vnd.oci.image.config.v1+json",
                "digest": "sha256:c",
                "size": 1,
            },
            "layers": [],
        }
    )
    assert m.schema_version == 2
    assert m.config.media_type == "application/vnd.oci.image.config.v1+json"


# -- Vendor-extension tolerance -------------------------------------------


def test_unknown_top_level_fields_ignored() -> None:
    m = OciImageManifest.model_validate(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {"mediaType": "x", "digest": "sha256:c", "size": 1},
            "layers": [],
            "totallyNonStandardField": {"some": "value"},
        }
    )
    assert m.schema_version == 2
