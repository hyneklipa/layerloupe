"""Tests for copy-to-clipboard buttons + tag/digest pull commands."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import (
    ManifestKind,
    ManifestResponse,
    MediaType,
    RegistryClient,
    to_unified,
)
from tests.conftest import load_fixture, load_fixture_bytes


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


# -- UnifiedManifest carries both pull commands ---------------------------


def test_unified_manifest_has_both_pull_commands() -> None:
    body = load_fixture("manifest_oci")
    raw = load_fixture_bytes("manifest_oci")
    mr = ManifestResponse(
        digest="sha256:fixture",
        media_type=MediaType.OCI_IMAGE_MANIFEST.value,
        kind=ManifestKind.OCI_IMAGE,
        body=body,
        raw_body=raw,
    )
    unified = to_unified(
        mr,
        pull_command="docker pull host/foo:latest",
        pull_command_digest="docker pull host/foo@sha256:fixture",
    )
    assert unified.pull_command == "docker pull host/foo:latest"
    assert unified.pull_command_digest == "docker pull host/foo@sha256:fixture"


def test_unified_manifest_pull_command_digest_optional() -> None:
    body = load_fixture("manifest_oci")
    raw = load_fixture_bytes("manifest_oci")
    mr = ManifestResponse(
        digest="sha256:fixture",
        media_type=MediaType.OCI_IMAGE_MANIFEST.value,
        kind=ManifestKind.OCI_IMAGE,
        body=body,
        raw_body=raw,
    )
    unified = to_unified(mr, pull_command="docker pull host/foo:latest")
    assert unified.pull_command_digest is None


# -- Web: tag-referenced manifest renders both commands -------------------


def test_tag_reference_renders_both_pull_commands(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Tag-pinned variant: ``foo:latest``.
    assert 'data-pull-variant="tag"' in body
    assert "foo:latest" in body
    # Digest-pinned variant: ``foo@sha256:...``.
    assert 'data-pull-variant="digest"' in body
    assert "foo@sha256:" in body
    # Both copy buttons available.
    assert body.count("copy-btn") >= 3  # digest header + two pull commands


# -- Web: digest-referenced manifest only shows digest variant ------------


def test_digest_reference_only_shows_digest_command(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """When the URL is already digest-pinned, don't duplicate the same string."""
    raw = load_fixture_bytes("manifest_oci")
    digest = _digest_of(raw)
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get(f"/partials/repositories/foo/manifests/{digest}").text

    # Only the digest variant block should appear.
    assert 'data-pull-variant="digest"' in body
    assert 'data-pull-variant="tag"' not in body


# -- Copy buttons no longer disabled, carry data-copy ---------------------


def test_copy_buttons_are_enabled(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # No disabled attribute on copy buttons in this panel.
    assert "copy-btn" in body
    # The presence of "disabled" only on the Delete confirm button is fine;
    # copy buttons must not carry it.
    import re

    copy_btns = re.findall(r'<button[^>]*class="copy-btn"[^>]*>', body)
    assert copy_btns, "expected at least one copy button"
    for btn in copy_btns:
        assert "disabled" not in btn


def test_copy_buttons_carry_correct_data_copy_values(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # The digest copy button has the full sha256 digest string.
    assert 'data-copy="sha256:' in body
    # The tag-pull copy button quotes the full ``docker pull foo:latest`` string.
    assert 'data-copy="docker pull' in body


# -- layerloupe.js wiring ----------------------------------------------------


def test_layerloupe_js_implements_copy_and_toast() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text

    # Clipboard helpers + toast plumbing.
    assert "navigator.clipboard" in js
    assert "fallbackCopy" in js
    assert "showToast" in js
    assert "Copied" in js
    assert "data-copy" in js
    # The toast element id is created lazily in the DOM.
    assert "layerloupe-toast" in js


def test_layerloupe_js_rebinds_after_htmx_swap() -> None:
    """Copy buttons in newly-fetched fragments must work too."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    # Already covered indirectly by the info-panel tests - make explicit here.
    assert "htmx:afterSwap" in js
    assert "bindCopyButtons" in js


# -- Toast styles present in CSS ------------------------------------------


def test_css_includes_toast_styles() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert ".toast" in css
    assert ".toast--visible" in css


# -- API endpoint emits both pull commands --------------------------------


def test_api_manifest_returns_both_pull_commands(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["pull_command"] is not None
    assert body["pull_command_digest"] is not None
    assert body["pull_command_digest"].startswith("docker pull ")
    assert "@sha256:" in body["pull_command_digest"]
