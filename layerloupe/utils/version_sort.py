"""Tag sorting for the registry UI.

Three buckets, top-to-bottom:

1. ``latest`` — always at the top.
2. Version-like tags (``1.10``, ``v2.3.4``, ``2.0.0-rc1``), descending by
   numeric components. ``1.10`` sorts before ``1.2``; ``1.0.0`` before
   ``1.0.0-rc1`` (release beats pre-release).
3. Everything else (``alpine``, ``bookworm``, ``slim``), alphabetical
   ascending.

The version detector is intentionally permissive: any tag that starts with
an optional ``v`` followed by dot-separated digits qualifies. We don't try
to be a full PEP 440 / semver parser — Docker tags in the wild are messy
and we'd rather sort *most* of them right than reject borderline cases.
"""

from __future__ import annotations

import re

LATEST_TAG = "latest"

# v?<digits>(.<digits>)*([-+]<suffix>)?  — case-insensitive, optional v prefix.
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+)*)(?:[-+]([\w.-]+))?$", re.IGNORECASE)


def _parse_version(tag: str) -> tuple[tuple[int, ...], str] | None:
    """Return ``((components, …), suffix)`` if ``tag`` is version-like, else ``None``.

    Examples:

    * ``"1.0"`` → ``((1, 0), "")``
    * ``"v1.2.3"`` → ``((1, 2, 3), "")``
    * ``"1.0.0-rc1"`` → ``((1, 0, 0), "rc1")``
    * ``"latest"`` → ``None``
    * ``"alpine"`` → ``None``
    """
    match = _VERSION_RE.match(tag)
    if match is None:
        return None
    components = tuple(int(p) for p in match.group(1).split("."))
    suffix = match.group(2) or ""
    return components, suffix


def sort_tags(tags: list[str]) -> list[str]:
    """Sort ``tags`` for display in the UI's tag list.

    The ordering strategy is the one operators expect: pin ``latest`` at the
    top, the version waterfall in the middle (newest first), and the
    long-tail of named release codenames at the bottom.

    Stable for inputs containing duplicates, though the registry shouldn't
    return duplicates in the first place.
    """
    latest_bucket: list[str] = []
    versioned: list[tuple[tuple[int, ...], str, str]] = []
    other: list[str] = []

    for tag in tags:
        if tag == LATEST_TAG:
            latest_bucket.append(tag)
            continue
        parsed = _parse_version(tag)
        if parsed is not None:
            components, suffix = parsed
            versioned.append((components, suffix, tag))
        else:
            other.append(tag)

    # Within the version bucket: descending numeric components first; for
    # ties, no-suffix wins (release > pre-release); within suffix
    # tie-breaking, lexicographic descending so ``rc2`` outranks ``rc1``.
    versioned.sort(key=lambda v: (v[0], v[1] == "", v[1]), reverse=True)

    return [*latest_bucket, *(t for _, _, t in versioned), *sorted(other)]
