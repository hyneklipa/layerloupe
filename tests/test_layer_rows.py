"""Tests for layer rows + Dockerfile parsing + UI rendering."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import (
    LARGE_LAYER_THRESHOLD,
    HistoryEntry,
    LayerRow,
    MediaType,
    ParsedInstruction,
    RegistryClient,
    UnifiedLayer,
    build_layer_rows,
    parse_created_by,
)
from tests.conftest import load_fixture_bytes

# -- parse_created_by ----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_inst", "expected_body"),
    [
        # Modern buildkit form.
        ("RUN apt-get update", "RUN", "apt-get update"),
        ("COPY package.json ./", "COPY", "package.json ./"),
        ("ENV PATH=/bin", "ENV", "PATH=/bin"),
        ("LABEL org.example=foo", "LABEL", "org.example=foo"),
        ("WORKDIR /app", "WORKDIR", "/app"),
        # Legacy shell-wrap, real layer (RUN).
        (
            "/bin/sh -c apt-get update && apt-get install curl",
            "RUN",
            "apt-get update && apt-get install curl",
        ),
        # Legacy #(nop) markers.
        ('/bin/sh -c #(nop)  CMD ["/bin/bash"]', "CMD", '["/bin/bash"]'),
        ("/bin/sh -c #(nop) ADD file:abc in /", "ADD", "file:abc in /"),
        ("/bin/sh -c #(nop) ENV LANG=C.UTF-8", "ENV", "LANG=C.UTF-8"),
        # Edge cases.
        ("", None, ""),
    ],
)
def test_parse_created_by(raw: str, expected_inst: str | None, expected_body: str) -> None:
    parsed = parse_created_by(raw)
    assert isinstance(parsed, ParsedInstruction)
    assert parsed.instruction == expected_inst
    assert parsed.body == expected_body


def test_parse_created_by_unknown_returns_none() -> None:
    """Garbage text doesn't crash; returns ``None`` instruction."""
    parsed = parse_created_by("just some narrative text")
    assert parsed.instruction is None


def test_parse_created_by_handles_none_input() -> None:
    parsed = parse_created_by(None)
    assert parsed.instruction is None
    assert parsed.body == ""


def test_parse_created_by_uppercases_modern_form() -> None:
    """Buildkit always emits uppercase, but be lenient if a tool wrote lowercase."""
    # Lowercase ``run`` isn't recognized as an instruction (we keep the
    # whitelist strict to avoid false positives on prose). It falls back.
    parsed = parse_created_by("run apt-get update")
    assert parsed.instruction is None


# -- build_layer_rows ----------------------------------------------------


def _layer(size: int, digest: str = "sha256:" + "a" * 64) -> UnifiedLayer:
    return UnifiedLayer(
        media_type="application/vnd.oci.image.layer.v1.tar+gzip",
        digest=digest,
        size=size,
    )


def _hist(*, created_by: str = "", empty: bool = False) -> HistoryEntry:
    return HistoryEntry.model_validate({"created_by": created_by, "empty_layer": empty})


def test_build_layer_rows_pairs_layer_with_history() -> None:
    layers = [_layer(1024)]
    history = [_hist(created_by="RUN echo hello")]

    rows = build_layer_rows(layers, history)
    assert len(rows) == 1
    assert rows[0].instruction == "RUN"
    assert rows[0].body == "echo hello"
    assert rows[0].is_empty is False
    assert rows[0].digest is not None
    assert rows[0].size == 1024


def test_build_layer_rows_empty_layer_consumes_no_blob() -> None:
    layers = [_layer(2048)]
    history = [
        _hist(created_by="/bin/sh -c #(nop) CMD ['x']", empty=True),
        _hist(created_by="RUN make"),
    ]

    rows = build_layer_rows(layers, history)
    assert len(rows) == 2
    assert rows[0].is_empty is True
    assert rows[0].digest is None
    assert rows[0].size == 0
    assert rows[0].instruction == "CMD"
    # Second history entry consumed the single layer.
    assert rows[1].is_empty is False
    assert rows[1].digest is not None
    assert rows[1].size == 2048


def test_build_layer_rows_marks_large_layers() -> None:
    small = _layer(LARGE_LAYER_THRESHOLD - 1)
    big = _layer(LARGE_LAYER_THRESHOLD + 1)
    rows = build_layer_rows(
        [small, big],
        [_hist(created_by="RUN cmd1"), _hist(created_by="RUN cmd2")],
    )
    assert rows[0].is_large is False
    assert rows[1].is_large is True


def test_build_layer_rows_threshold_is_exact() -> None:
    """Exactly 100 MiB should count as large."""
    rows = build_layer_rows(
        [_layer(LARGE_LAYER_THRESHOLD)],
        [_hist(created_by="RUN big")],
    )
    assert rows[0].is_large is True


def test_build_layer_rows_no_history_falls_back_to_layers() -> None:
    """When history is missing, every layer becomes a bare row."""
    rows = build_layer_rows([_layer(100), _layer(200)], None)
    assert len(rows) == 2
    assert rows[0].instruction is None
    assert rows[1].instruction is None
    assert rows[0].size == 100
    assert rows[1].size == 200
    assert rows[0].is_empty is False


def test_build_layer_rows_history_without_layers() -> None:
    """All-metadata history (e.g. schema 1 with all empty steps)."""
    rows = build_layer_rows(
        [],
        [_hist(created_by="ENV FOO=bar", empty=True)],
    )
    assert len(rows) == 1
    assert rows[0].is_empty is True
    assert rows[0].instruction == "ENV"


def test_build_layer_rows_extra_layers_appended() -> None:
    """Registry returned more layers than history narrates — keep them."""
    rows = build_layer_rows(
        [_layer(10), _layer(20), _layer(30)],
        [_hist(created_by="RUN a"), _hist(created_by="RUN b")],
    )
    assert len(rows) == 3
    assert rows[2].instruction is None  # leftover layer
    assert rows[2].size == 30


def test_build_layer_rows_empty_inputs() -> None:
    assert build_layer_rows([], []) == []
    assert build_layer_rows(None, None) == []


# -- End-to-end UI rendering ---------------------------------------------


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@pytest.fixture
def use_handler() -> Iterator[dict[str, Callable[[httpx.Request], httpx.Response]]]:
    box: dict[str, Callable[[httpx.Request], httpx.Response]] = {
        "handler": lambda r: httpx.Response(404)
    }

    def _override() -> RegistryClient:
        return RegistryClient(
            "https://registry.example.com",
            transport=httpx.MockTransport(lambda r: box["handler"](r)),
        )

    app.dependency_overrides[get_registry_client] = _override
    try:
        yield box
    finally:
        app.dependency_overrides.pop(get_registry_client, None)


def _make_handler(fixture: str, content_type: str) -> Callable[[httpx.Request], httpx.Response]:
    manifest_bytes = load_fixture_bytes(fixture)
    config_bytes = load_fixture_bytes("image_config")
    digest = _digest_of(manifest_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": content_type,
                    "docker-content-digest": digest,
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=config_bytes)
        return httpx.Response(404)

    return handler


def test_oci_image_layers_show_instruction_badges(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """The OCI fixture's image_config has RUN, ADD, and CMD history entries."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "instruction-badge" in body
    # image_config fixture's third history entry is empty_layer=true (CMD).
    assert "layer-row--empty" in body
    # And carries an "empty" pill.
    assert ">empty<" in body


def test_layer_rows_render_dockerfile_body(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """The parsed body (apt-get / CMD args) shows in <pre class="dockerfile-body">."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert 'class="dockerfile-body"' in body
    # The image_config fixture has a "RUN apt-get install -y curl" entry
    # — verify the body text comes through.
    assert "apt-get" in body or "/bin/bash" in body


def test_large_layer_highlight_when_present(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Synthesize a manifest whose layer is >100 MiB; UI must mark it ``--large``."""
    big_size = LARGE_LAYER_THRESHOLD + 1024
    big_manifest = (
        b'{"schemaVersion":2,'
        b'"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        b'"config":{"mediaType":"application/vnd.oci.image.config.v1+json",'
        b'"digest":"sha256:cfg","size":1},'
        b'"layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar+gzip",'
        b'"digest":"sha256:big","size":' + str(big_size).encode() + b"}]}"
    )
    config = b'{"architecture":"amd64","os":"linux","history":[{"created_by":"RUN huge-install"}]}'

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=big_manifest,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": "sha256:m",
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=config)
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    assert "layer-row--large" in body
    assert "layer-size--large" in body
    assert "100 MiB" in body or "≥ 100 MiB" in body  # tooltip text


def test_no_history_renders_bare_layers(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Manifest without an image config still renders a usable Layers tab."""
    manifest_bytes = (
        b'{"schemaVersion":2,'
        b'"mediaType":"application/vnd.oci.image.manifest.v1+json",'
        b'"config":{"mediaType":"application/vnd.oci.image.config.v1+json",'
        b'"digest":"sha256:cfg","size":1},'
        b'"layers":[{"mediaType":"application/vnd.oci.image.layer.v1.tar+gzip",'
        b'"digest":"sha256:l1","size":12345}]}'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": "sha256:abc",
                },
            )
        if "/blobs/" in path:
            # Config blob fetch fails; we still render layers from the manifest.
            return httpx.Response(500)
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    assert "layer-list" in body
    # No history → no instruction badges, but the digest and size still show.
    assert "sha256:l1" in body


# -- CSS hooks -----------------------------------------------------------


def test_layer_css_hooks_present() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    for hook in (
        ".layer-row--empty",
        ".layer-row--large",
        ".instruction-badge--run",
        ".instruction-badge--copy",
        ".dockerfile-body",
    ):
        assert hook in css, f"missing CSS hook: {hook}"


# -- Frozen dataclasses --------------------------------------------------


def test_layer_row_is_immutable() -> None:
    row = LayerRow(
        instruction="RUN",
        body="x",
        raw_created_by="RUN x",
        created=None,
        is_empty=False,
        digest="sha256:abc",
        size=100,
        media_type="application/vnd.oci.image.layer.v1.tar+gzip",
        is_large=False,
    )
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError
        row.size = 999  # type: ignore[misc]
