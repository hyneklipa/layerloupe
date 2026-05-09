"""Tests for the image info panel: tabs, multi-arch dropdown, humanized values."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import MediaType, RegistryClient
from tests.conftest import load_fixture_bytes


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


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


# -- Single-arch image: tabs visible, no platform dropdown ----------------


def test_single_arch_image_renders_sections(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Each sub-section is rendered as a stacked block (no tabs).
    assert "config-section" in body
    assert "layers-section" in body
    assert "annotations-section" in body
    # Section headers carry the column-style title style.
    assert "info-section-title" in body


def test_single_arch_image_hides_platform_pills(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Single-arch images don't get the multi-arch picker section.
    assert "platform-pills" not in body
    assert "data-platform-pill" not in body
    # The single-platform inline label should appear instead.
    assert "Platform:" in body
    assert "amd64" in body


def test_single_arch_image_shows_layer_count_and_size(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # OCI fixture has 2 layers totalling 32654 + 16724 = 49378 bytes ~= 48.2 KB.
    assert "2 layers" in body
    assert "KB" in body  # human_size produces "48.2 KB"


def test_single_arch_image_renders_config_in_config_section(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Configuration tab content from image_config.json fixture.
    assert "Cmd" in body
    assert "/bin/bash" in body  # cfg.cmd
    assert "Entrypoint" in body
    assert "/usr/local/bin/entrypoint.sh" in body
    assert "LayerLoupe test fixtures" in body  # author


def test_single_arch_image_shows_layers_with_sizes(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Each layer row should carry a humanized size (32654 B → 31.9 KB).
    assert "layer-list" in body
    assert "layer-row" in body
    assert "31.9 KB" in body
    # OCI fixture annotations rendered in the annotations section.
    assert "org.opencontainers.image.licenses" in body
    assert "Apache-2.0" in body


# -- Multi-arch index: pills visible, no per-image sections --------------


def test_multi_arch_index_shows_platform_pills(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_index", MediaType.OCI_IMAGE_INDEX.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Pill list rendered with both architectures from the fixture.
    assert "platform-pills" in body
    assert "platform-pill" in body
    assert "amd64" in body
    assert "arm64" in body
    # Each pill carries the child-manifest digest (sha256:...).
    assert "sha256:e692418e" in body  # first 8 hex chars of fixture digest


def test_multi_arch_index_hides_image_sections(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_index", MediaType.OCI_IMAGE_INDEX.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Index has no layers / config of its own — those sections aren't shown.
    assert "config-section" not in body
    assert "layers-section" not in body
    # But the platform count is surfaced in the meta dl.
    assert "2 platforms" in body


def test_multi_arch_index_pills_use_plain_anchor_navigation(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Pill is a plain ``<a href>`` so browser back/forward stay reliable.

    htmx in-place swap was tried earlier but proved flaky on history
    restore when combined with the OOB auto-select swap.
    """
    use_handler["handler"] = _make_handler("manifest_index", MediaType.OCI_IMAGE_INDEX.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    assert "platform-pill" in body
    assert "?platform=sha256:" in body
    assert 'href="/repositories/foo/manifests/latest?platform=sha256:' in body
    # No htmx plumbing on pills — full reload is the design.
    pill_section = body.split('class="platform-pills"')[1].split("</div>")[0]
    assert "hx-get" not in pill_section
    assert "hx-push-url" not in pill_section


# -- Header always present, regardless of variant -------------------------


def test_header_includes_digest_and_pull_command(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Digest visible in header.
    assert "manifest-info-head" in body
    # Pull command rendered.
    assert "docker pull" in body
    assert "foo:latest" in body
    # Copy buttons present (their JS click-handler is wired separately).
    assert "copy-btn" in body
    assert "data-copy=" in body


def test_header_humanizes_created_when_image_config_present(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """The header should render a relative time alongside the absolute timestamp."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Either "ago" or a future "in" string — depends on the fixture's date
    # vs. test-time wall clock; both are valid.
    assert "ago" in body or " in " in body


# -- Schema 1 fallback ----------------------------------------------------


def test_schema_1_renders_without_crashing(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Schema 1 manifests have no separate config blob; render must not fail."""
    use_handler["handler"] = _make_handler("manifest_v1", MediaType.DOCKER_MANIFEST_V1_SIGNED.value)

    with TestClient(app) as client:
        response = client.get("/partials/repositories/foo/manifests/latest")
    assert response.status_code == 200
    body = response.text
    # Synthesized config from v1Compatibility surfaces architecture/os.
    assert "amd64" in body
    assert "linux" in body


# -- htmx + widgets wired in static asset --------------------------------


def test_layerloupe_js_includes_widget_bindings() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert "data-tabs" in js  # info-section reuses the tab binding
    assert "data-copy" in js  # copy-to-clipboard handler
    assert "htmx:afterSwap" in js  # re-binds widgets after fragment swap


def test_platform_pill_anchor_href_includes_platform_query(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Pill href carries ``?platform=<digest>`` so refresh / share works."""
    use_handler["handler"] = _make_handler("manifest_index", MediaType.OCI_IMAGE_INDEX.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    assert "platform-pill" in body
    assert "?platform=sha256:" in body
