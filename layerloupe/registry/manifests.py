"""Manifest media types, classifier, and the response shape returned by
:meth:`RegistryClient.get_manifest`.

Six manifest media types exist in the wild. They split into three
semantic groups:

* **Index / manifest list** — multi-arch pointer (OCI Image Index, Docker
  Manifest List).
* **Image manifest v2 / OCI** — single-arch with separate config blob
  (OCI Image Manifest, Docker Manifest v2).
* **Schema 1 (legacy)** — single-arch with embedded ``v1Compatibility``
  history strings (Docker Manifest v1, with or without prettyjws signatures).

The :class:`ManifestKind` enum collapses to those three plus an "unknown"
escape hatch for unexpected ``Content-Type`` values.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final


class MediaType(StrEnum):
    """Manifest media-type identifiers used in ``Accept`` and ``Content-Type``."""

    OCI_IMAGE_INDEX = "application/vnd.oci.image.index.v1+json"
    OCI_IMAGE_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
    DOCKER_MANIFEST_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
    DOCKER_MANIFEST_V2 = "application/vnd.docker.distribution.manifest.v2+json"
    DOCKER_MANIFEST_V1_SIGNED = "application/vnd.docker.distribution.manifest.v1+prettyjws+json"
    DOCKER_MANIFEST_V1 = "application/vnd.docker.distribution.manifest.v1+json"


# Order matters when sent in ``Accept``: most preferred first. Modern formats
# come first so a registry that supports OCI returns OCI rather than v1.
MANIFEST_ACCEPT_TYPES: Final[tuple[str, ...]] = (
    MediaType.OCI_IMAGE_INDEX.value,
    MediaType.OCI_IMAGE_MANIFEST.value,
    MediaType.DOCKER_MANIFEST_LIST.value,
    MediaType.DOCKER_MANIFEST_V2.value,
    MediaType.DOCKER_MANIFEST_V1_SIGNED.value,
    MediaType.DOCKER_MANIFEST_V1.value,
)

MANIFEST_ACCEPT_HEADER: Final[str] = ", ".join(MANIFEST_ACCEPT_TYPES)


class ManifestKind(StrEnum):
    """Coarse classification by content type."""

    OCI_INDEX = "oci_index"
    OCI_IMAGE = "oci_image"
    DOCKER_LIST = "docker_list"
    DOCKER_V2 = "docker_v2"
    DOCKER_V1 = "docker_v1"
    UNKNOWN = "unknown"

    @property
    def is_index(self) -> bool:
        """``True`` for multi-arch indexes / manifest lists."""
        return self in (ManifestKind.OCI_INDEX, ManifestKind.DOCKER_LIST)


_KIND_BY_MEDIA_TYPE: Final[dict[str, ManifestKind]] = {
    MediaType.OCI_IMAGE_INDEX.value: ManifestKind.OCI_INDEX,
    MediaType.OCI_IMAGE_MANIFEST.value: ManifestKind.OCI_IMAGE,
    MediaType.DOCKER_MANIFEST_LIST.value: ManifestKind.DOCKER_LIST,
    MediaType.DOCKER_MANIFEST_V2.value: ManifestKind.DOCKER_V2,
    MediaType.DOCKER_MANIFEST_V1_SIGNED.value: ManifestKind.DOCKER_V1,
    MediaType.DOCKER_MANIFEST_V1.value: ManifestKind.DOCKER_V1,
}


def classify_media_type(content_type: str | None) -> ManifestKind:
    """Map a ``Content-Type`` value to a :class:`ManifestKind`.

    Strips parameters (``; charset=utf-8`` etc.) and is case-insensitive.
    Unknown values fall through to :attr:`ManifestKind.UNKNOWN` rather than
    raising — lets callers decide the policy.
    """
    if not content_type:
        return ManifestKind.UNKNOWN
    base = content_type.split(";", 1)[0].strip().lower()
    return _KIND_BY_MEDIA_TYPE.get(base, ManifestKind.UNKNOWN)


@dataclass(frozen=True)
class ManifestResponse:
    """A manifest as returned by the registry, with the metadata needed to
    parse / display / address it.

    ``digest`` is the registry's own canonical digest (from
    ``Docker-Content-Digest``); ``raw_body`` is preserved so callers can
    re-hash to verify or compute a digest themselves if the header was
    missing on a non-conformant registry.
    """

    digest: str | None
    media_type: str
    kind: ManifestKind
    body: dict[str, Any]
    raw_body: bytes

    @property
    def is_index(self) -> bool:
        return self.kind.is_index
