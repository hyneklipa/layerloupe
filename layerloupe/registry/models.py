"""Pydantic models for registry payloads.

The **image config blob** (:class:`ImageConfig`) plus the manifest
variants:

* :class:`OciImageManifest` and :class:`DockerManifestV2` - single-arch
  images with a separate config blob.
* :class:`OciImageIndex` and :class:`DockerManifestList` - multi-arch
  pointers (one descriptor per platform).
* :class:`DockerSchema1Manifest` - legacy schema 1 with embedded
  ``v1Compatibility`` history strings.

Field names in the JSON wire format mix camelCase (``schemaVersion``,
``mediaType``, ``fsLayers``) and Go-style CamelCase (``Cmd``, ``Env``).
We map both onto idiomatic Python ``snake_case`` via field aliases.

The unified view-model parser in :mod:`layerloupe.registry.parser` converts
whichever variant the registry returned into a single shape for the UI /
API layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

# A parent ``model_config`` shared by every model in this file.
_BASE_CONFIG = ConfigDict(
    populate_by_name=True,  # accept both alias ("Cmd") and field name ("cmd")
    extra="ignore",  # tolerate vendor-specific fields we don't model
)


class ContainerConfig(BaseModel):
    """The runtime configuration block of an image config blob.

    Maps to the ``config`` (or, on schema 1, ``container_config``) sub-object
    of an image config. All fields are optional - minimal images may set very
    few of them.
    """

    model_config = _BASE_CONFIG

    user: str | None = Field(default=None, alias="User")
    entrypoint: list[str] | None = Field(default=None, alias="Entrypoint")
    cmd: list[str] | None = Field(default=None, alias="Cmd")
    env: list[str] | None = Field(default=None, alias="Env")
    working_dir: str | None = Field(default=None, alias="WorkingDir")
    exposed_ports: dict[str, dict[str, Any]] | None = Field(default=None, alias="ExposedPorts")
    volumes: dict[str, dict[str, Any]] | None = Field(default=None, alias="Volumes")
    labels: dict[str, str] | None = Field(default=None, alias="Labels")
    stop_signal: str | None = Field(default=None, alias="StopSignal")


class HistoryEntry(BaseModel):
    """One row of the image's history.

    ``empty_layer`` flags entries that don't materialize a layer (e.g.
    ``CMD``, ``ENV``, ``LABEL`` from a Dockerfile). The UI distinguishes
    these from actual layer-producing operations.
    """

    model_config = _BASE_CONFIG

    created: datetime | None = None
    created_by: str | None = None
    author: str | None = None
    comment: str | None = None
    empty_layer: bool = False


class RootFS(BaseModel):
    """Root filesystem section pointing at uncompressed layer ``diff_ids``."""

    model_config = _BASE_CONFIG

    type: str
    diff_ids: list[str] = Field(default_factory=list)


class ImageConfig(BaseModel):
    """An OCI / Docker image config blob.

    Reachable from a manifest via ``manifest.config.digest``. Carries the
    runtime configuration, the rootfs (layer ``diff_ids``), and the build
    history - i.e. everything the UI needs to render an image's "details"
    panel beyond the manifest itself.
    """

    model_config = _BASE_CONFIG

    architecture: str
    os: str
    os_version: str | None = Field(default=None, alias="os.version")
    variant: str | None = None
    created: datetime | None = None
    author: str | None = None
    config: ContainerConfig = Field(default_factory=ContainerConfig)
    rootfs: RootFS | None = None
    history: list[HistoryEntry] = Field(default_factory=list)


# -- Shared descriptor / platform shapes ----------------------------------


class Descriptor(BaseModel):
    """An OCI / Docker descriptor: a typed, sized, content-addressable pointer.

    Used in three places:

    * ``manifest.config`` (single-arch images point at their config blob).
    * ``manifest.layers[]`` (filesystem layer blobs).
    * ``index.manifests[]`` (each entry of an index - see
      :class:`IndexManifestEntry`, which extends this with platform info).
    """

    model_config = _BASE_CONFIG

    media_type: str = Field(alias="mediaType")
    digest: str
    size: int
    urls: list[str] | None = None
    annotations: dict[str, str] | None = None


class Platform(BaseModel):
    """Platform discriminator for entries inside an image index / manifest list."""

    model_config = _BASE_CONFIG

    architecture: str
    os: str
    os_version: str | None = Field(default=None, alias="os.version")
    os_features: list[str] | None = Field(default=None, alias="os.features")
    variant: str | None = None
    features: list[str] | None = None


class IndexManifestEntry(Descriptor):
    """A descriptor extended with platform information.

    Distinct from a plain :class:`Descriptor` so the type signals the entry
    is selectable by platform (UI / parser code branches on this).
    """

    platform: Platform | None = None


# -- Single-arch manifests (image manifest schema 2 / OCI) ----------------


class OciImageManifest(BaseModel):
    """OCI Image Manifest v1 - single-arch image with a config blob and layers.

    ``subject`` lands with OCI 1.1 (referrers API). Older registries omit
    it; we tolerate that.
    """

    model_config = _BASE_CONFIG

    schema_version: int = Field(alias="schemaVersion")
    media_type: str | None = Field(default=None, alias="mediaType")
    config: Descriptor
    layers: list[Descriptor] = Field(default_factory=list)
    subject: Descriptor | None = None
    annotations: dict[str, str] | None = None


class DockerManifestV2(BaseModel):
    """Docker Image Manifest v2 schema 2 - structurally same as
    :class:`OciImageManifest`, distinct mediaType vocabulary.

    Kept as a separate class so consumers can branch on ``isinstance`` /
    classify by Python type without re-inspecting ``mediaType`` strings.
    """

    model_config = _BASE_CONFIG

    schema_version: int = Field(alias="schemaVersion")
    media_type: str | None = Field(default=None, alias="mediaType")
    config: Descriptor
    layers: list[Descriptor] = Field(default_factory=list)


# -- Multi-arch manifests (index / list) ----------------------------------


class OciImageIndex(BaseModel):
    """OCI Image Index v1 - ordered list of per-platform manifest descriptors."""

    model_config = _BASE_CONFIG

    schema_version: int = Field(alias="schemaVersion")
    media_type: str | None = Field(default=None, alias="mediaType")
    manifests: list[IndexManifestEntry] = Field(default_factory=list)
    subject: Descriptor | None = None
    annotations: dict[str, str] | None = None


class DockerManifestList(BaseModel):
    """Docker Manifest List v2 - Docker's pre-OCI multi-arch format.

    Same structure as :class:`OciImageIndex` minus ``subject`` / ``annotations``.
    """

    model_config = _BASE_CONFIG

    schema_version: int = Field(alias="schemaVersion")
    media_type: str | None = Field(default=None, alias="mediaType")
    manifests: list[IndexManifestEntry] = Field(default_factory=list)


# -- Schema 1 (legacy) ----------------------------------------------------


class FsLayer(BaseModel):
    """Single filesystem layer reference in a schema 1 manifest."""

    model_config = _BASE_CONFIG

    blob_sum: str = Field(alias="blobSum")


class V1HistoryEntry(BaseModel):
    """One schema 1 history entry; ``v1_compatibility`` is itself a JSON string.

    The string is parsed lazily by :mod:`layerloupe.registry.parser`,
    not here - this layer just carries the raw JSON over the wire.
    """

    model_config = _BASE_CONFIG

    v1_compatibility: str = Field(alias="v1Compatibility")


class V1Signature(BaseModel):
    """Schema 1 cryptographic signature (we don't validate, just preserve)."""

    model_config = _BASE_CONFIG

    header: dict[str, Any] | None = None
    signature: str | None = None
    protected: str | None = None


class DockerSchema1Manifest(BaseModel):
    """Legacy Docker manifest schema 1 with embedded ``v1Compatibility`` history.

    Read-only territory - modern registries deprecated schema 1 pushes
    long ago. We model it just well enough to fall back gracefully when an
    old registry returns it.
    """

    model_config = _BASE_CONFIG

    schema_version: int = Field(alias="schemaVersion")
    name: str
    tag: str | None = None
    architecture: str
    fs_layers: list[FsLayer] = Field(default_factory=list, alias="fsLayers")
    history: list[V1HistoryEntry] = Field(default_factory=list)
    signatures: list[V1Signature] | None = None
