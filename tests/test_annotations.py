"""Tests for the annotations panel (merge + classify + render)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import (
    KNOWN_OCI_ANNOTATIONS,
    AnnotationRow,
    KnownAnnotation,
    MediaType,
    RegistryClient,
    merge_annotations,
)
from layerloupe.registry.annotations import is_url
from tests.conftest import load_fixture_bytes

# -- Pure unit: classifier + URL detection + merge -----------------------


def test_known_oci_annotations_includes_source() -> None:
    """The acceptance criterion expects ``image.source`` to be a known key."""
    assert "org.opencontainers.image.source" in KNOWN_OCI_ANNOTATIONS
    info = KNOWN_OCI_ANNOTATIONS["org.opencontainers.image.source"]
    assert isinstance(info, KnownAnnotation)
    assert info.label == "Source"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://github.com/foo/bar", True),
        ("http://example.com", True),
        ("ftp://example.com", False),
        ("just a string", False),
        ("", False),
    ],
)
def test_is_url(value: str, expected: bool) -> None:
    assert is_url(value) is expected


def test_merge_with_only_manifest_annotations() -> None:
    rows = merge_annotations(
        {"org.opencontainers.image.source": "https://github.com/foo/bar"},
        None,
    )
    assert len(rows) == 1
    assert rows[0].key == "org.opencontainers.image.source"
    assert rows[0].label == "Source"
    assert rows[0].is_known is True
    assert rows[0].is_url is True


def test_merge_with_only_labels() -> None:
    rows = merge_annotations(
        None,
        {"org.opencontainers.image.licenses": "Apache-2.0"},
    )
    assert len(rows) == 1
    assert rows[0].label == "Licenses"
    assert rows[0].is_known is True
    assert rows[0].is_url is False


def test_manifest_annotations_win_over_labels() -> None:
    """Same key in both → manifest annotation is the value rendered."""
    rows = merge_annotations(
        {"org.opencontainers.image.version": "from-manifest"},
        {"org.opencontainers.image.version": "from-labels"},
    )
    assert len(rows) == 1
    assert rows[0].value == "from-manifest"


def test_known_keys_come_before_unknown_keys() -> None:
    rows = merge_annotations(
        {
            "com.example.unknown": "x",
            "org.opencontainers.image.title": "LayerLoupe",
            "z.last.alphabetically": "y",
        },
        None,
    )
    keys = [r.key for r in rows]
    # Known one first, then alphabetical.
    assert keys[0] == "org.opencontainers.image.title"
    assert keys[1:] == ["com.example.unknown", "z.last.alphabetically"]


def test_known_keys_render_in_spec_defined_order() -> None:
    rows = merge_annotations(
        {
            "org.opencontainers.image.licenses": "MIT",
            "org.opencontainers.image.source": "https://example.com",
            "org.opencontainers.image.title": "Foo",
        },
        None,
    )
    keys = [r.key for r in rows]
    # Spec order: title, description, source, url, documentation, version, ...
    assert keys.index("org.opencontainers.image.title") < keys.index(
        "org.opencontainers.image.source"
    )
    assert keys.index("org.opencontainers.image.source") < keys.index(
        "org.opencontainers.image.licenses"
    )


def test_unknown_keys_sorted_alphabetically() -> None:
    rows = merge_annotations(
        {"zzz.com": "z", "aaa.com": "a", "mmm.com": "m"},
        None,
    )
    keys = [r.key for r in rows]
    assert keys == ["aaa.com", "mmm.com", "zzz.com"]


def test_url_detection_propagates_for_known_keys() -> None:
    """A known key whose value happens to be a URL is_url=True."""
    rows = merge_annotations(
        {"org.opencontainers.image.documentation": "https://docs.example.com"},
        None,
    )
    assert rows[0].is_url is True


def test_empty_inputs_return_empty_list() -> None:
    assert merge_annotations(None, None) == []
    assert merge_annotations({}, {}) == []


def test_non_string_values_filtered_out() -> None:
    """Defensive: registries occasionally serve non-string values."""
    rows = merge_annotations(
        {"good.key": "x", "bad.key": 42},  # type: ignore[dict-item]
        None,
    )
    keys = [r.key for r in rows]
    assert "good.key" in keys
    assert "bad.key" not in keys


def test_annotation_row_carries_description_for_known_keys() -> None:
    rows = merge_annotations({"org.opencontainers.image.source": "https://example.com"}, None)
    assert rows[0].description  # non-empty


def test_annotation_row_for_unknown_has_empty_description() -> None:
    rows = merge_annotations({"some.vendor.key": "val"}, None)
    assert rows[0].description == ""


# -- End-to-end: template rendering --------------------------------------


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


def test_oci_image_renders_friendly_labels(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """OCI fixture has annotations + labels; both should surface with friendly names."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # Friendly labels for known keys (OCI fixture has source, revision,
    # created, licenses).
    assert ">Source<" in body
    assert ">Revision<" in body
    assert ">Created<" in body
    assert ">Licenses<" in body
    # The raw key is shown alongside as a small caption.
    assert "org.opencontainers.image.source" in body


def test_source_annotation_is_a_clickable_link(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Acceptance criterion: ``image.source`` must render as ``<a href>``."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # The OCI fixture's source URL is https://github.com/example/repo.
    assert 'href="https://github.com/example/repo"' in body
    # Opens in a new tab without leaking referrer.
    assert 'target="_blank"' in body
    assert 'rel="noopener noreferrer"' in body


def test_known_rows_carry_friendly_label(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Known OCI keys surface their friendly name via ``.annotation-label``;
    the ``--known`` row variant was retired so every row reads the same."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "annotations-table" in body
    assert "annotation-label" in body
    assert "annotation-row--known" not in body


def test_image_with_only_labels_still_renders_annotations(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Images with no manifest.annotations but config.Labels still get the table."""
    # The Docker v2 fixture has no manifest.annotations; image_config fixture
    # has labels. Merging should produce rows from labels alone.
    use_handler["handler"] = _make_handler("manifest_v2", MediaType.DOCKER_MANIFEST_V2.value)
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    # image_config.json has org.opencontainers.image.source + version labels.
    assert ">Source<" in body
    assert "annotations-table" in body


def test_no_annotations_renders_empty_state(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Image with truly no annotations or labels — show a hint, not an empty table."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if "/manifests/" in path:
            # Minimal OCI manifest, no annotations, config has no labels.
            body = (
                b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json",'
                b'"config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:cfg","size":1},'
                b'"layers":[]}'
            )
            return httpx.Response(
                200,
                content=body,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": "sha256:abc",
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=b'{"architecture":"amd64","os":"linux"}')
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "No annotations or labels" in body
    assert "annotations-table" not in body


def test_labels_no_longer_in_configuration_tab(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Labels were moved to Annotations — Config tab shouldn't double-render them."""
    use_handler["handler"] = _make_handler("manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value)
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text

    # The Configuration <dl> no longer carries a "Labels" <dt>.
    assert "<dt>Labels</dt>" not in body


def test_api_annotations_field_unchanged() -> None:
    """Annotations panel is web-only; the JSON API still returns raw annotations."""
    # The unified manifest schema in the API has ``annotations: dict[str, str]``;
    # the *table* lives in the HTML view, not in the API contract.
    # Sanity check via the existing OpenAPI doc.
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    schema = spec["components"]["schemas"]["UnifiedManifest"]
    assert "annotations" in schema["properties"]


# -- CSS hooks for the table ---------------------------------------------


def test_annotations_css_present() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert ".annotations-table" in css
    assert ".annotation-row--known" in css
    assert ".annotation-link" in css


# -- Touch the row dataclass ---------------------------------------------


def test_annotation_row_is_immutable() -> None:
    row = AnnotationRow(
        key="k",
        label="L",
        value="v",
        description="",
        is_known=False,
        is_url=False,
    )
    with pytest.raises(Exception):  # noqa: B017 - dataclass(frozen=True) raises FrozenInstanceError
        row.value = "other"  # type: ignore[misc]
