"""Image annotation classification + merging.

OCI Image Manifest carries metadata in two slots:

* ``manifest.annotations`` - the modern, manifest-level dictionary
  (`org.opencontainers.image.*` is the standardized namespace).
* ``image_config.config.Labels`` - the older Docker-style runtime labels.
  Most images set the same key in both for backwards compatibility.

Together they describe **what the image is** (source repo, version, license,
SPDX ID, base image). The annotations tab shows them as one merged table
with a curated ordering: known OCI keys first, then vendor namespaces,
then anything else, alphabetical within each bucket.

Reference: https://github.com/opencontainers/image-spec/blob/main/annotations.md
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KnownAnnotation:
    """Display metadata for a single recognized annotation key."""

    label: str
    """Short, human-friendly column label (e.g. ``"Source"``)."""

    description: str
    """One-line explanation; rendered as the ``title`` attribute on hover."""


# Order matters - the table renders these top-to-bottom in this sequence
# when present. We follow the OCI spec's reading order so the most
# operationally interesting fields surface first.
KNOWN_OCI_ANNOTATIONS: dict[str, KnownAnnotation] = {
    "org.opencontainers.image.title": KnownAnnotation(
        "Title", "Human-readable title of the image."
    ),
    "org.opencontainers.image.description": KnownAnnotation(
        "Description", "Human-readable description of the image."
    ),
    "org.opencontainers.image.source": KnownAnnotation(
        "Source", "URL of the source code repository."
    ),
    "org.opencontainers.image.url": KnownAnnotation(
        "Homepage", "URL describing the image (typically a project website)."
    ),
    "org.opencontainers.image.documentation": KnownAnnotation(
        "Documentation", "URL pointing at the image's documentation."
    ),
    "org.opencontainers.image.version": KnownAnnotation(
        "Version", "Version of the packaged software."
    ),
    "org.opencontainers.image.revision": KnownAnnotation(
        "Revision", "Source-control revision (commit hash) the image was built from."
    ),
    "org.opencontainers.image.created": KnownAnnotation("Created", "ISO 8601 build timestamp."),
    "org.opencontainers.image.authors": KnownAnnotation(
        "Authors", "Contact details of the image authors."
    ),
    "org.opencontainers.image.vendor": KnownAnnotation(
        "Vendor", "Organization or entity distributing the image."
    ),
    "org.opencontainers.image.licenses": KnownAnnotation(
        "Licenses", "SPDX license expression for the packaged contents."
    ),
    "org.opencontainers.image.ref.name": KnownAnnotation(
        "Reference", "The image's reference name (tag) when pushed."
    ),
    "org.opencontainers.image.base.name": KnownAnnotation(
        "Base image", "Reference to the base image this was built from."
    ),
    "org.opencontainers.image.base.digest": KnownAnnotation(
        "Base digest", "Digest of the base image at build time."
    ),
}


def is_url(value: str) -> bool:
    """``True`` for ``http://`` / ``https://`` strings - used to decide auto-linking."""
    return value.startswith(("http://", "https://"))


@dataclass(frozen=True)
class AnnotationRow:
    """Pre-rendered row passed to the template."""

    key: str
    """Raw annotation key (e.g. ``org.opencontainers.image.source``)."""

    label: str
    """Friendly column label - :class:`KnownAnnotation.label` for known keys, else the raw key."""

    value: str
    description: str
    """Tooltip / hover text - empty for unknown keys."""

    is_known: bool
    is_url: bool


def merge_annotations(
    manifest_annotations: dict[str, str] | None,
    image_labels: dict[str, str] | None,
) -> list[AnnotationRow]:
    """Combine manifest annotations and image-config labels into a typed table.

    Conflict resolution: ``manifest.annotations`` wins over
    ``image_config.config.Labels`` for the same key - the manifest is the
    authoritative source in modern (OCI 1.0+) images.

    Output order:

    1. Known ``org.opencontainers.image.*`` keys, in the spec-defined order
       above.
    2. Everything else, alphabetical.
    """
    merged: dict[str, str] = {}
    for k, v in (image_labels or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            merged[k] = v
    for k, v in (manifest_annotations or {}).items():
        if isinstance(k, str) and isinstance(v, str):
            merged[k] = v  # manifest precedence

    rows: list[AnnotationRow] = []

    for key, info in KNOWN_OCI_ANNOTATIONS.items():
        if key in merged:
            value = merged.pop(key)
            rows.append(
                AnnotationRow(
                    key=key,
                    label=info.label,
                    value=value,
                    description=info.description,
                    is_known=True,
                    is_url=is_url(value),
                )
            )

    for key in sorted(merged):
        value = merged[key]
        rows.append(
            AnnotationRow(
                key=key,
                label=key,
                value=value,
                description="",
                is_known=False,
                is_url=is_url(value),
            )
        )

    return rows
