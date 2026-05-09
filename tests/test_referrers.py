"""Tests for referrers parser, client method, API endpoint, UI tab."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import (
    KNOWN_ARTIFACT_TYPES,
    MediaType,
    Referrer,
    RegistryClient,
    parse_referrers,
)
from tests.conftest import load_fixture_bytes

# -- parse_referrers — pure unit -----------------------------------------


def test_parse_empty_body() -> None:
    assert parse_referrers(None) == []
    assert parse_referrers({}) == []
    assert parse_referrers({"manifests": None}) == []
    assert parse_referrers({"manifests": "not-a-list"}) == []  # type: ignore[arg-type]


def test_parse_single_cosign_signature() -> None:
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:" + "a" * 64,
                "size": 1234,
                "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
            }
        ]
    }
    rows = parse_referrers(body)
    assert len(rows) == 1
    r = rows[0]
    assert r.kind == "signature"
    assert r.label == "Cosign signature"
    assert r.digest == "sha256:" + "a" * 64
    assert r.size == 1234
    assert r.artifact_type == "application/vnd.dev.cosign.simplesigning.v1+json"


@pytest.mark.parametrize(
    ("artifact_type", "expected_kind", "expected_label"),
    [
        ("application/vnd.dev.cosign.simplesigning.v1+json", "signature", "Cosign signature"),
        ("application/vnd.cncf.notary.signature", "signature", "Notary signature"),
        ("application/vnd.cyclonedx+json", "sbom", "CycloneDX SBOM"),
        ("application/spdx+json", "sbom", "SPDX SBOM"),
        ("application/vnd.in-toto+json", "attestation", "in-toto attestation"),
        ("application/vnd.dsse.envelope.v1+json", "attestation", "DSSE envelope"),
    ],
)
def test_classifier_recognizes_well_known_types(
    artifact_type: str, expected_kind: str, expected_label: str
) -> None:
    assert artifact_type in KNOWN_ARTIFACT_TYPES
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:abc",
                "size": 1,
                "artifactType": artifact_type,
            }
        ]
    }
    rows = parse_referrers(body)
    assert rows[0].kind == expected_kind
    assert rows[0].label == expected_label


def test_unknown_artifact_type_falls_back_to_other() -> None:
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:x",
                "size": 100,
                "artifactType": "application/vnd.acme.custom-signature+json",
            }
        ]
    }
    rows = parse_referrers(body)
    assert rows[0].kind == "other"
    # Falls back to the artifactType string itself as the label.
    assert rows[0].label == "application/vnd.acme.custom-signature+json"


def test_classifier_uses_media_type_when_artifact_type_missing() -> None:
    """Older registries omit ``artifactType``; we fall back to mediaType."""
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.cyclonedx+json",
                "digest": "sha256:y",
                "size": 50,
            }
        ]
    }
    rows = parse_referrers(body)
    assert rows[0].kind == "sbom"
    assert rows[0].label == "CycloneDX SBOM"
    assert rows[0].artifact_type is None


def test_unknown_no_artifact_no_match_returns_other_unknown() -> None:
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:z",
                "size": 1,
            }
        ]
    }
    rows = parse_referrers(body)
    assert rows[0].kind == "other"
    assert rows[0].label == "Unknown artifact"


def test_parse_preserves_annotations() -> None:
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:a",
                "size": 1,
                "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
                "annotations": {
                    "dev.sigstore.cosign/signature": "MEUCIQ...",
                    "dev.sigstore.cosign/bundle": "{}",
                },
            }
        ]
    }
    rows = parse_referrers(body)
    assert "dev.sigstore.cosign/signature" in rows[0].annotations
    assert rows[0].annotations["dev.sigstore.cosign/bundle"] == "{}"


def test_parse_drops_malformed_rows() -> None:
    body = {
        "manifests": [
            "not-a-dict",  # intentionally malformed
            {"digest": "sha256:a"},  # missing mediaType
            {"mediaType": "x"},  # missing digest
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:good",
                "size": 1,
            },
        ]
    }
    rows = parse_referrers(body)  # type: ignore[arg-type]
    assert len(rows) == 1
    assert rows[0].digest == "sha256:good"


def test_parse_size_defaults_to_zero_when_missing_or_wrong_type() -> None:
    body = {
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:a",
            },
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": "sha256:b",
                "size": "lots",  # type: ignore[dict-item]
            },
        ]
    }
    rows = parse_referrers(body)
    assert all(r.size == 0 for r in rows)


# -- RegistryClient.get_referrers ----------------------------------------


async def test_get_referrers_returns_typed_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v2/foo/referrers/"):
            return httpx.Response(
                200,
                json={
                    "manifests": [
                        {
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": "sha256:sig",
                            "size": 1234,
                            "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        rows = await client.get_referrers("foo", "sha256:abc")

    assert len(rows) == 1
    assert isinstance(rows[0], Referrer)
    assert rows[0].kind == "signature"


@pytest.mark.parametrize("status", [404, 405, 501])
async def test_get_referrers_soft_fails_on_unsupported(status: int) -> None:
    """Registries without the OCI 1.1 endpoint return one of these — we want []."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        rows = await client.get_referrers("foo", "sha256:abc")

    assert rows == []


async def test_get_referrers_propagates_other_errors() -> None:
    """A 500 from the registry is not a "not implemented" signal — surface it."""
    from layerloupe.registry import RegistryHTTPError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"errors": [{"code": "INTERNAL"}]})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_referrers("foo", "sha256:abc")
    assert exc_info.value.status_code == 500


# -- End-to-end: API endpoint --------------------------------------------


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


def test_api_referrers_returns_typed_items(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/referrers/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "manifests": [
                        {
                            "mediaType": "application/vnd.oci.image.manifest.v1+json",
                            "digest": "sha256:sig",
                            "size": 1234,
                            "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/sha256:abc/referrers")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["kind"] == "signature"
    assert item["label"] == "Cosign signature"
    assert item["digest"] == "sha256:sig"
    assert item["artifact_type"] == "application/vnd.dev.cosign.simplesigning.v1+json"


def test_api_referrers_soft_fails_when_unsupported(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/referrers/" in request.url.path:
            return httpx.Response(404)
        if "/manifests/" in request.url.path:
            return httpx.Response(200, headers={"docker-content-digest": "sha256:abc"})
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest/referrers")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


# -- End-to-end: UI tab --------------------------------------------------


def _make_handler_with_referrers(
    referrers_body: dict[str, object] | None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve manifest_oci + image_config + a configurable referrers response."""
    manifest_bytes = load_fixture_bytes("manifest_oci")
    config_bytes = load_fixture_bytes("image_config")
    digest = _digest_of(manifest_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if "/referrers/" in path:
            if referrers_body is None:
                return httpx.Response(404)
            return httpx.Response(200, json=referrers_body)
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": digest,
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=config_bytes)
        return httpx.Response(404)

    return handler


def test_ui_tab_hidden_when_registry_lacks_referrers(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Acceptance criterion: registry without referrers → tab is not rendered."""
    use_handler["handler"] = _make_handler_with_referrers(referrers_body=None)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "referrers-section" not in body
    assert "Signatures &amp; Attestations" not in body


def test_ui_tab_hidden_when_referrers_empty(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    use_handler["handler"] = _make_handler_with_referrers(referrers_body={"manifests": []})
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "referrers-section" not in body


def test_ui_tab_rendered_when_referrers_present(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Acceptance criterion: cosign signature shows up as a Signature row."""
    use_handler["handler"] = _make_handler_with_referrers(
        referrers_body={
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:cosignsig",
                    "size": 2048,
                    "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
                    "annotations": {
                        "dev.sigstore.cosign/signature": "MEUCIQDxyz...",
                    },
                }
            ]
        }
    )

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    assert "referrers-section" in body
    assert "Signatures &amp; Attestations" in body
    assert "Cosign signature" in body
    assert "sha256:cosignsig" in body
    # Color-coded kind badge.
    assert "referrer-kind--signature" in body
    # Annotations come through too.
    assert "dev.sigstore.cosign/signature" in body


def test_ui_referrer_digest_links_to_referrer_manifest(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Clicking a referrer digest navigates to that manifest."""
    use_handler["handler"] = _make_handler_with_referrers(
        referrers_body={
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": "sha256:abcdef",
                    "size": 100,
                    "artifactType": "application/vnd.cyclonedx+json",
                }
            ]
        }
    )

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    assert 'href="/repositories/foo/manifests/sha256:abcdef"' in body
    assert "referrer-kind--sbom" in body
    assert "CycloneDX SBOM" in body


def test_ui_no_referrers_for_index_manifest(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Index manifests skip the referrers fetch (per-platform child has them)."""
    manifest_bytes = load_fixture_bytes("manifest_index")
    digest = _digest_of(manifest_bytes)
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
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
                    "content-type": MediaType.OCI_IMAGE_INDEX.value,
                    "docker-content-digest": digest,
                },
            )
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Tab not rendered for indexes.
    assert "referrers-section" not in body
    # And we didn't even hit the registry's referrers endpoint.
    assert all("/referrers/" not in p for p in seen_paths)


# -- CSS hooks -----------------------------------------------------------


def test_referrer_css_hooks_present() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    for hook in (
        ".referrer-list",
        ".referrer-row",
        ".referrer-kind--signature",
        ".referrer-kind--sbom",
        ".referrer-kind--attestation",
    ):
        assert hook in css, f"missing CSS hook: {hook}"
