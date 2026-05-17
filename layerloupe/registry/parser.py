"""Unified manifest view-model + ``to_unified()`` parser.

The registry can return a manifest in five different shapes (OCI image, OCI
index, Docker v2, Docker manifest list, Docker schema 1). The UI / API
shouldn't have to branch on ``mediaType`` strings every time it wants to
render a "size" or a "platform" - :func:`to_unified` flattens the variants
into a single :class:`UnifiedManifest` shape that downstream code can
consume directly.

The unified shape:

* ``type``: ``"image"`` for single-arch, ``"index"`` for multi-arch.
* ``platforms``: 1-element list for an image (architecture from the image
  config blob, when fetched), N-element for an index.
* ``config`` / ``layers``: only for single-arch images.
* ``annotations``, ``subject``: propagated from the source manifest.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from layerloupe.registry.exceptions import RegistryError
from layerloupe.registry.manifests import ManifestKind, ManifestResponse
from layerloupe.registry.models import (
    Descriptor,
    DockerManifestList,
    DockerManifestV2,
    DockerSchema1Manifest,
    ImageConfig,
    OciImageIndex,
    OciImageManifest,
)

logger = structlog.get_logger()

# -- Default values used as fallbacks for incomplete manifests ------------

_UNKNOWN_PLATFORM_OS = "unknown"
_UNKNOWN_PLATFORM_ARCH = "unknown"
_SCHEMA_1_DEFAULT_OS = "linux"  # Docker schema 1 predates cross-OS images
_SCHEMA_1_LAYER_MEDIA_TYPE = "application/vnd.docker.image.rootfs.diff.tar.gzip"


_BASE_CONFIG = ConfigDict(extra="ignore")


class UnifiedPlatform(BaseModel):
    """Per-platform pointer.

    For an image, this describes the image's own platform (one entry).
    For an index, each entry references a child manifest by ``digest``.
    """

    model_config = _BASE_CONFIG

    architecture: str
    os: str
    variant: str | None = None
    digest: str | None = None  # set when this platform points at a child manifest


class UnifiedLayer(BaseModel):
    """Single layer descriptor - minimal subset the UI needs to render layers."""

    model_config = _BASE_CONFIG

    media_type: str
    digest: str
    size: int


class UnifiedConfig(BaseModel):
    """Reference to (and optionally the contents of) the image config blob.

    On schema 2 / OCI manifests the config is a separate content-addressable
    blob, so ``digest`` and ``size`` are populated. Schema 1 manifests embed
    the config inside the manifest body itself - there is no separate blob,
    so ``digest`` is ``None`` and ``size`` is 0 in that case. Callers that
    care about the distinction can branch on ``digest is None``.
    """

    model_config = _BASE_CONFIG

    digest: str | None
    size: int
    data: ImageConfig | None = None  # populated only when caller fetched the blob


class UnifiedManifest(BaseModel):
    """Manifest as the rest of the application sees it - variant-agnostic."""

    model_config = _BASE_CONFIG

    digest: str | None
    media_type: str
    schema_version: int
    type: Literal["image", "index"]
    size: int  # for images: sum of layer sizes; for indexes: sum of child manifest sizes
    platforms: list[UnifiedPlatform] = Field(default_factory=list)
    config: UnifiedConfig | None = None
    layers: list[UnifiedLayer] = Field(default_factory=list)
    annotations: dict[str, str] = Field(default_factory=dict)
    subject: Descriptor | None = None
    pull_command: str | None = None
    """Tag-pinned ``docker pull`` (e.g. ``docker pull host/repo:latest``)."""
    pull_command_digest: str | None = None
    """Digest-pinned ``docker pull`` (immutable: ``docker pull host/repo@sha256:…``)."""


# -- Public entry point ---------------------------------------------------


def to_unified(
    manifest_response: ManifestResponse,
    image_config: ImageConfig | None = None,
    *,
    pull_command: str | None = None,
    pull_command_digest: str | None = None,
) -> UnifiedManifest:
    """Convert a fetched :class:`ManifestResponse` into :class:`UnifiedManifest`.

    Args:
        manifest_response: What :meth:`RegistryClient.get_manifest` returned.
        image_config: Optional, but recommended for single-arch images -
            populates ``config.data`` and the platform info. Ignored for
            indexes (no config blob to attach).
        pull_command: Optional pre-rendered tag-pinned ``docker pull …``
            string. The parser doesn't know the public registry URL, so the
            caller assembles this. ``None`` is fine - the UI can hide it.
        pull_command_digest: Optional immutable digest-pinned variant
            (``docker pull host/repo@sha256:…``). Recommended for any
            production "pin this image" workflow.

    Raises:
        :class:`RegistryError` for unknown / non-manifest content types.
    """
    kind = manifest_response.kind
    if kind is ManifestKind.OCI_INDEX:
        m = _from_oci_index(manifest_response, pull_command)
    elif kind is ManifestKind.DOCKER_LIST:
        m = _from_docker_list(manifest_response, pull_command)
    elif kind is ManifestKind.OCI_IMAGE:
        m = _from_oci_image(manifest_response, image_config, pull_command)
    elif kind is ManifestKind.DOCKER_V2:
        m = _from_docker_v2(manifest_response, image_config, pull_command)
    elif kind is ManifestKind.DOCKER_V1:
        m = _from_schema_1(manifest_response, pull_command)
    else:
        raise RegistryError(
            f"Cannot unify manifest of unknown content type: {manifest_response.media_type!r}"
        )
    if pull_command_digest is not None:
        m = m.model_copy(update={"pull_command_digest": pull_command_digest})
    return m


# -- Per-variant builders -------------------------------------------------


def _from_oci_index(mr: ManifestResponse, pull_command: str | None) -> UnifiedManifest:
    idx = OciImageIndex.model_validate(mr.body)
    return UnifiedManifest(
        digest=mr.digest,
        media_type=mr.media_type,
        schema_version=idx.schema_version,
        type="index",
        size=sum(entry.size for entry in idx.manifests),
        platforms=_platforms_from_index(idx.manifests),
        annotations=idx.annotations or {},
        subject=idx.subject,
        pull_command=pull_command,
    )


def _from_docker_list(mr: ManifestResponse, pull_command: str | None) -> UnifiedManifest:
    lst = DockerManifestList.model_validate(mr.body)
    return UnifiedManifest(
        digest=mr.digest,
        media_type=mr.media_type,
        schema_version=lst.schema_version,
        type="index",
        size=sum(entry.size for entry in lst.manifests),
        platforms=_platforms_from_index(lst.manifests),
        pull_command=pull_command,
    )


def _from_oci_image(
    mr: ManifestResponse,
    image_config: ImageConfig | None,
    pull_command: str | None,
) -> UnifiedManifest:
    m = OciImageManifest.model_validate(mr.body)
    return UnifiedManifest(
        digest=mr.digest,
        media_type=mr.media_type,
        schema_version=m.schema_version,
        type="image",
        size=sum(layer.size for layer in m.layers),
        platforms=_image_platform(image_config),
        config=UnifiedConfig(digest=m.config.digest, size=m.config.size, data=image_config),
        layers=[
            UnifiedLayer(media_type=layer.media_type, digest=layer.digest, size=layer.size)
            for layer in m.layers
        ],
        annotations=m.annotations or {},
        subject=m.subject,
        pull_command=pull_command,
    )


def _from_docker_v2(
    mr: ManifestResponse,
    image_config: ImageConfig | None,
    pull_command: str | None,
) -> UnifiedManifest:
    m = DockerManifestV2.model_validate(mr.body)
    return UnifiedManifest(
        digest=mr.digest,
        media_type=mr.media_type,
        schema_version=m.schema_version,
        type="image",
        size=sum(layer.size for layer in m.layers),
        platforms=_image_platform(image_config),
        config=UnifiedConfig(digest=m.config.digest, size=m.config.size, data=image_config),
        layers=[
            UnifiedLayer(media_type=layer.media_type, digest=layer.digest, size=layer.size)
            for layer in m.layers
        ],
        pull_command=pull_command,
    )


def _from_schema_1(mr: ManifestResponse, pull_command: str | None) -> UnifiedManifest:
    """Schema 1 fallback: read-only synthesis from embedded ``v1Compatibility``.

    Schema 1 manifests embed everything an :class:`ImageConfig` would carry
    (architecture, os, created, author, runtime config, history) inside
    JSON-string entries in ``history[]``. We decode the first entry to
    populate the image-level config and walk the rest to build a history
    with ``created_by`` strings (the Dockerfile commands that built each
    layer).

    Best-effort: a malformed ``v1Compatibility`` string falls back to a
    bare-bones unified manifest with just the manifest-level ``architecture``.
    """
    m = DockerSchema1Manifest.model_validate(mr.body)
    image_config = _synthesize_image_config_from_v1(m)

    architecture = image_config.architecture if image_config else m.architecture
    os = image_config.os if image_config else _SCHEMA_1_DEFAULT_OS
    config: UnifiedConfig | None = (
        UnifiedConfig(digest=None, size=0, data=image_config) if image_config else None
    )

    return UnifiedManifest(
        digest=mr.digest,
        media_type=mr.media_type,
        schema_version=m.schema_version,
        type="image",
        size=0,  # schema 1 has no per-layer size info
        platforms=[UnifiedPlatform(architecture=architecture, os=os)],
        config=config,
        layers=[
            UnifiedLayer(media_type=_SCHEMA_1_LAYER_MEDIA_TYPE, digest=fl.blob_sum, size=0)
            for fl in m.fs_layers
        ],
        pull_command=pull_command,
    )


def _synthesize_image_config_from_v1(
    manifest: DockerSchema1Manifest,
) -> ImageConfig | None:
    """Decode ``v1Compatibility`` JSON strings into an :class:`ImageConfig`.

    Returns ``None`` if there is no history at all, or if the first entry
    can't be JSON-decoded (then the unified manifest will simply lack a
    config; the caller can still render layers + the manifest-level
    architecture).
    """
    if not manifest.history:
        return None

    first = _decode_v1_compatibility(manifest.history[0].v1_compatibility)
    if first is None:
        logger.warning("schema_1_v1compatibility_decode_failed", repository=manifest.name)
        return None

    # Image config can live in either ``config`` (the Docker spec) or
    # ``container_config`` (older builds, often the only one populated). The
    # former is preferred but both have the same shape.
    config_section: dict[str, Any] = first.get("config") or first.get("container_config") or {}

    payload: dict[str, Any] = {
        "architecture": first.get("architecture") or manifest.architecture,
        "os": first.get("os") or _SCHEMA_1_DEFAULT_OS,
        "created": first.get("created"),
        "author": first.get("author"),
        "config": config_section,
        "history": _build_v1_history(manifest),
    }

    try:
        return ImageConfig.model_validate(payload)
    except ValidationError as e:
        logger.warning(
            "schema_1_image_config_validation_failed",
            repository=manifest.name,
            error=str(e),
        )
        return None


def _build_v1_history(manifest: DockerSchema1Manifest) -> list[dict[str, Any]]:
    """Walk ``history[]`` and produce :class:`HistoryEntry`-shaped dicts.

    ``created_by`` is reconstructed from the first command in
    ``container_config.Cmd`` - that's where Docker builds stored the
    Dockerfile instruction (often prefixed with ``/bin/sh -c #(nop) `` for
    metadata-only ops; the parser leaves the prefix for the UI to clean up).
    """
    history: list[dict[str, Any]] = []
    for entry in manifest.history:
        decoded = _decode_v1_compatibility(entry.v1_compatibility)
        if decoded is None:
            continue
        container_config = decoded.get("container_config") or {}
        cmd = container_config.get("Cmd") if isinstance(container_config, dict) else None
        created_by: str | None = None
        if isinstance(cmd, list) and cmd and isinstance(cmd[0], str):
            created_by = cmd[0]
        history.append(
            {
                "created": decoded.get("created"),
                "created_by": created_by,
                "author": decoded.get("author"),
                "comment": decoded.get("comment"),
                # Docker spec calls this `throwaway` in v1Compatibility but
                # `empty_layer` in the modern image config - same idea.
                "empty_layer": bool(decoded.get("throwaway", False)),
            }
        )
    return history


def _decode_v1_compatibility(payload: str) -> dict[str, Any] | None:
    """JSON-decode a ``v1Compatibility`` string, returning ``None`` on failure."""
    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


# -- Helpers --------------------------------------------------------------


def _platforms_from_index(entries: list) -> list[UnifiedPlatform]:  # type: ignore[type-arg]
    """Flatten index entries to UnifiedPlatform list; preserves ordering."""
    platforms: list[UnifiedPlatform] = []
    for entry in entries:
        plat = entry.platform
        if plat is None:
            platforms.append(
                UnifiedPlatform(
                    architecture=_UNKNOWN_PLATFORM_ARCH,
                    os=_UNKNOWN_PLATFORM_OS,
                    digest=entry.digest,
                )
            )
        else:
            platforms.append(
                UnifiedPlatform(
                    architecture=plat.architecture,
                    os=plat.os,
                    variant=plat.variant,
                    digest=entry.digest,
                )
            )
    return platforms


def _image_platform(image_config: ImageConfig | None) -> list[UnifiedPlatform]:
    """Build the single-platform list for an image, when we know the config."""
    if image_config is None:
        return []
    return [
        UnifiedPlatform(
            architecture=image_config.architecture,
            os=image_config.os,
            variant=image_config.variant,
        )
    ]
