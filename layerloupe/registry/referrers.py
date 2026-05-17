"""Parse the OCI 1.1 ``/v2/<name>/referrers/<digest>`` endpoint.

The Referrers API lets clients ask "which manifests point at this one as
their ``subject``?" - that's how cosign signatures, SBOMs, and in-toto
attestations attach themselves to an image. Older registries don't
implement it (Docker Distribution gained it in v2.8 / spec 1.1); the API
gracefully degrades to an empty list when the registry returns 404/405/501.

This module classifies the entries by ``artifactType`` so the UI can
render a friendly label ("Cosign signature", "CycloneDX SBOM") instead of
the raw media-type string.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ArtifactKind = Literal["signature", "sbom", "attestation", "other"]


@dataclass(frozen=True)
class KnownArtifactType:
    kind: ArtifactKind
    label: str


# Media types recognized in the wild. Keys match the ``artifactType`` field
# (preferred) or the inner manifest's ``mediaType`` as a fallback.
KNOWN_ARTIFACT_TYPES: dict[str, KnownArtifactType] = {
    # Cosign - the most common signature flavor on registries today.
    "application/vnd.dev.cosign.simplesigning.v1+json": KnownArtifactType(
        "signature", "Cosign signature"
    ),
    "application/vnd.dev.cosign.artifact.sig.v1+json": KnownArtifactType(
        "signature", "Cosign signature"
    ),
    # Sigstore bundles (newer cosign + sigstore-go format).
    "application/vnd.dev.sigstore.bundle.v0.3+json": KnownArtifactType(
        "signature", "Sigstore bundle"
    ),
    "application/vnd.dev.sigstore.bundle+json;version=0.3": KnownArtifactType(
        "signature", "Sigstore bundle"
    ),
    # Notary v2 - older signing scheme that's still around.
    "application/vnd.cncf.notary.signature": KnownArtifactType("signature", "Notary signature"),
    # SBOMs.
    "application/vnd.cyclonedx+json": KnownArtifactType("sbom", "CycloneDX SBOM"),
    "application/spdx+json": KnownArtifactType("sbom", "SPDX SBOM"),
    "application/vnd.syft+json": KnownArtifactType("sbom", "Syft SBOM"),
    # In-toto attestations + DSSE envelopes (build provenance).
    "application/vnd.in-toto+json": KnownArtifactType("attestation", "in-toto attestation"),
    "application/vnd.dsse.envelope.v1+json": KnownArtifactType("attestation", "DSSE envelope"),
}


@dataclass(frozen=True)
class Referrer:
    """One row of the referrers tab."""

    digest: str
    media_type: str
    size: int
    artifact_type: str | None
    """``artifactType`` field from the OCI 1.1 spec (may be ``None`` on older registries)."""

    kind: ArtifactKind
    """Coarse classification used for icon / color in the UI."""

    label: str
    """Human-friendly artifact name (``"Cosign signature"`` for known types,
    else the raw ``artifact_type`` or ``"Unknown"``)."""

    annotations: dict[str, str] = field(default_factory=dict)


def _classify(artifact_type: str | None, media_type: str) -> tuple[ArtifactKind, str]:
    """Pick (kind, label) using artifactType first, then mediaType fallback."""
    for candidate in (artifact_type, media_type):
        if candidate and candidate in KNOWN_ARTIFACT_TYPES:
            entry = KNOWN_ARTIFACT_TYPES[candidate]
            return entry.kind, entry.label
    if artifact_type:
        return "other", artifact_type
    return "other", "Unknown artifact"


def parse_referrers(body: dict[str, Any] | None) -> list[Referrer]:
    """Convert a raw referrers response body into a typed list.

    The OCI 1.1 referrers endpoint returns an image index whose
    ``manifests[]`` list each carries ``digest``, ``mediaType``, ``size``,
    optional ``artifactType``, and optional ``annotations``. We accept and
    pass through anything we can read, and silently drop malformed rows.
    """
    if not isinstance(body, dict):
        return []
    raw_manifests = body.get("manifests")
    if not isinstance(raw_manifests, list):
        return []

    out: list[Referrer] = []
    for entry in raw_manifests:
        if not isinstance(entry, dict):
            continue
        digest = entry.get("digest")
        media_type = entry.get("mediaType")
        size = entry.get("size", 0)
        if not isinstance(digest, str) or not isinstance(media_type, str):
            continue
        if not isinstance(size, int):
            size = 0
        artifact_type = entry.get("artifactType")
        if artifact_type is not None and not isinstance(artifact_type, str):
            artifact_type = None
        kind, label = _classify(artifact_type, media_type)
        annotations = entry.get("annotations") or {}
        if not isinstance(annotations, dict):
            annotations = {}
        out.append(
            Referrer(
                digest=digest,
                media_type=media_type,
                size=size,
                artifact_type=artifact_type,
                kind=kind,
                label=label,
                annotations={
                    k: v
                    for k, v in annotations.items()
                    if isinstance(k, str) and isinstance(v, str)
                },
            )
        )
    return out
