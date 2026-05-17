"""Layer-row view-model: pair manifest layers with image-config history.

The layer info panel shows one row per Dockerfile-ish step. Some steps
produce blobs (``RUN``, ``COPY``, ``ADD``); others are metadata-only
(``CMD``, ``ENV``, ``LABEL`` - Docker calls these ``empty_layer``). The
manifest's ``layers[]`` only carries the blob-producing rows, while the
image config's ``history[]`` carries every step. Pairing them by walking
history and consuming layers in order recreates the build's narrative.

This module stays UI-agnostic: it builds typed dataclasses; the template
in :mod:`layerloupe.web.templates.partials.manifest_info` decides how to
render them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from layerloupe.registry.models import HistoryEntry
from layerloupe.registry.parser import UnifiedLayer

# Large-layer threshold - used to flag rows the user might want to optimize.
# 100 MiB is the conventional "your image is fat" line for base images, and
# it matches what ``docker history`` highlights in the Docker Desktop UI.
LARGE_LAYER_THRESHOLD: int = 100 * 1024 * 1024

_DOCKERFILE_INSTRUCTIONS: frozenset[str] = frozenset(
    {
        "ADD",
        "ARG",
        "CMD",
        "COPY",
        "ENTRYPOINT",
        "ENV",
        "EXPOSE",
        "FROM",
        "HEALTHCHECK",
        "LABEL",
        "MAINTAINER",
        "ONBUILD",
        "RUN",
        "SHELL",
        "STOPSIGNAL",
        "USER",
        "VOLUME",
        "WORKDIR",
    }
)

# Older Docker builds wrap shell commands in ``/bin/sh -c …`` and prefix
# metadata-only steps with ``#(nop)``.
_SHELL_PREFIX_RE = re.compile(r"^/bin/sh\s+-c\s+")
_NOP_RE = re.compile(r"^#\(nop\)\s*(?P<rest>.*)", re.DOTALL)
_INSTRUCTION_RE = re.compile(r"^(?P<inst>[A-Z][A-Z0-9_]*)\b\s*(?P<body>.*)", re.DOTALL)


@dataclass(frozen=True)
class ParsedInstruction:
    """Parsed form of an image config ``history[i].created_by`` string."""

    instruction: str | None
    """Dockerfile instruction in upper-case (``"RUN"``, ``"COPY"``, …) or ``None``."""

    body: str
    """Everything after the instruction, with the prefix noise stripped."""


def parse_created_by(raw: str | None) -> ParsedInstruction:
    """Identify the Dockerfile instruction behind a ``created_by`` string.

    Handles three formats commonly seen in the wild:

    * Modern buildkit: ``"RUN apt-get update"`` - instruction is the
      first whitespace-separated word.
    * Legacy shell-wrap (real layer): ``"/bin/sh -c apt-get update"`` →
      classified as ``RUN``.
    * Legacy shell-wrap (metadata): ``"/bin/sh -c #(nop)  CMD ['…']"`` →
      classifies the post-``#(nop)`` instruction.

    Falls back to ``ParsedInstruction(None, raw)`` for content we can't
    confidently classify.
    """
    if not raw:
        return ParsedInstruction(None, "")
    s = raw.strip()

    # Legacy shell-wrap. Strip ``/bin/sh -c `` and re-parse the inner.
    inner = _SHELL_PREFIX_RE.sub("", s, count=1)
    if inner != s:
        nop = _NOP_RE.match(inner)
        if nop:
            # ``#(nop)  CMD …`` → treat as the inner instruction (metadata-only).
            return _from_dockerfile_form(nop.group("rest").strip(), fallback=inner)
        # Plain shell command after /bin/sh -c is logically a RUN.
        return ParsedInstruction("RUN", inner.strip())

    # Modern buildkit form.
    return _from_dockerfile_form(s, fallback=s)


def _from_dockerfile_form(s: str, *, fallback: str) -> ParsedInstruction:
    match = _INSTRUCTION_RE.match(s)
    if match:
        word = match.group("inst").upper()
        if word in _DOCKERFILE_INSTRUCTIONS:
            return ParsedInstruction(word, match.group("body").strip())
    return ParsedInstruction(None, fallback)


@dataclass(frozen=True)
class LayerRow:
    """One row in the Layers tab - either a real layer or a metadata step."""

    instruction: str | None
    """Dockerfile instruction (``"RUN"``, ``"COPY"``, …) or ``None``."""

    body: str
    """The command body (``apt-get update`` for ``RUN``, etc.)."""

    raw_created_by: str
    """Original unparsed ``created_by`` - useful for debugging / copy-out."""

    created: datetime | None

    is_empty: bool
    """``True`` for metadata-only steps (no blob; Docker's ``empty_layer``)."""

    digest: str | None
    """``None`` for empty rows, else the layer blob digest."""

    size: int
    """``0`` for empty rows, otherwise blob size in bytes."""

    media_type: str
    """Layer media type, or ``""`` for empty rows."""

    is_large: bool
    """``size >= LARGE_LAYER_THRESHOLD`` - flagged in the UI."""


def build_layer_rows(
    layers: list[UnifiedLayer] | None,
    history: list[HistoryEntry] | None,
) -> list[LayerRow]:
    """Pair manifest layers with history entries into a single timeline.

    Walks history in declaration order. Empty (metadata-only) steps emit a
    row without a blob. Non-empty steps consume the next layer from
    ``layers[]``. Any leftover layers (no matching history) get appended as
    bare rows so they don't disappear from the UI.

    When history is missing (Schema 1 manifests with malformed
    ``v1Compatibility``, or registries that didn't return a config blob)
    we fall back to one row per layer with no parsed instruction.
    """
    layers_list = list(layers or [])
    history_list = list(history or [])

    if not history_list:
        return [_row_from_layer(layer, instruction=None, body="", raw="") for layer in layers_list]

    rows: list[LayerRow] = []
    layer_iter = iter(layers_list)
    for entry in history_list:
        parsed = parse_created_by(entry.created_by)
        if entry.empty_layer:
            rows.append(
                LayerRow(
                    instruction=parsed.instruction,
                    body=parsed.body,
                    raw_created_by=entry.created_by or "",
                    created=entry.created,
                    is_empty=True,
                    digest=None,
                    size=0,
                    media_type="",
                    is_large=False,
                )
            )
            continue
        layer = next(layer_iter, None)
        rows.append(
            LayerRow(
                instruction=parsed.instruction,
                body=parsed.body,
                raw_created_by=entry.created_by or "",
                created=entry.created,
                is_empty=False,
                digest=layer.digest if layer else None,
                size=layer.size if layer else 0,
                media_type=layer.media_type if layer else "",
                is_large=bool(layer and layer.size >= LARGE_LAYER_THRESHOLD),
            )
        )

    # Layers without a matching history entry: registry returned more
    # blobs than the config narrates. Tail them on so they stay visible.
    for leftover in layer_iter:
        rows.append(_row_from_layer(leftover, instruction=None, body="", raw=""))

    return rows


def _row_from_layer(
    layer: UnifiedLayer, *, instruction: str | None, body: str, raw: str
) -> LayerRow:
    return LayerRow(
        instruction=instruction,
        body=body,
        raw_created_by=raw,
        created=None,
        is_empty=False,
        digest=layer.digest,
        size=layer.size,
        media_type=layer.media_type,
        is_large=layer.size >= LARGE_LAYER_THRESHOLD,
    )
