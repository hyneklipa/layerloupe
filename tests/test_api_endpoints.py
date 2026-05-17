"""Integration tests covering happy path of every REST endpoint from §6."""

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


@pytest.fixture
def registry_handler() -> Iterator[dict[str, Callable[[httpx.Request], httpx.Response]]]:
    """Slot the test fills with a request handler - wired into the registry client."""
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


# -- /docs and OpenAPI ----------------------------------------------------


def test_openapi_lists_all_required_paths() -> None:
    """Every endpoint from design §6 must appear in the OpenAPI schema."""
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    paths = set(spec["paths"].keys())
    expected = {
        "/api/healthz",
        "/api/readyz",
        "/api/info",
        "/api/repositories",
        "/api/repositories/{repository}/tags",
        "/api/repositories/{repository}/manifests/{reference}",
        "/api/repositories/{repository}/manifests/{reference}/config",
        "/api/repositories/{repository}/manifests/{reference}/referrers",
        "/api/auth/login",
        "/api/auth/logout",
    }
    missing = expected - paths
    assert not missing, f"Missing paths in OpenAPI schema: {missing}"


def test_docs_endpoint_serves_swagger_ui() -> None:
    with TestClient(app) as client:
        response = client.get("/docs")
    assert response.status_code == 200
    assert "swagger-ui" in response.text.lower()


# -- /api/repositories ----------------------------------------------------


def test_list_repositories_happy_path(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/_catalog"
        return httpx.Response(200, json={"repositories": ["foo", "bar/baz", "qux"]})

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories")
    assert response.status_code == 200
    body = response.json()
    assert body["items"] == ["foo", "bar/baz", "qux"]
    assert body["total"] == 3


def test_list_repositories_filter_propagates(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"repositories": ["alpine", "library/ubuntu", "BUSYBOX", "node"]}
        )

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories", params={"q": "BU"})
    body = response.json()
    assert sorted(body["items"]) == ["BUSYBOX", "library/ubuntu"]


# -- /api/repositories/{repo}/tags ----------------------------------------


def test_list_tags_smart_sorted(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/library/ubuntu/tags/list"
        return httpx.Response(
            200,
            json={"name": "library/ubuntu", "tags": ["1.0", "1.10", "1.2", "latest", "edge"]},
        )

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/library/ubuntu/tags")
    assert response.status_code == 200
    body = response.json()
    assert body["repository"] == "library/ubuntu"
    # Smart sort: latest first, semver desc, codenames at bottom.
    assert body["items"] == ["latest", "1.10", "1.2", "1.0", "edge"]


def test_list_tags_404_for_missing_repo(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/missing/tags")
    # Global RegistryHTTPError handler in main.py preserves the status.
    assert response.status_code == 404


# -- /api/repositories/{repo}/manifests/{reference} -----------------------


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _manifest_and_config_handler(
    manifest_fixture: str, manifest_media_type: str
) -> Callable[[httpx.Request], httpx.Response]:
    """Serve the manifest, then the referenced config blob, then 404."""
    manifest_bytes = load_fixture_bytes(manifest_fixture)
    config_bytes = load_fixture_bytes("image_config")
    manifest_digest = _digest_of(manifest_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        if "/manifests/" in request.url.path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": manifest_media_type,
                    "docker-content-digest": manifest_digest,
                },
            )
        if "/blobs/" in request.url.path:
            return httpx.Response(200, content=config_bytes)
        return httpx.Response(404)

    return handler


def test_get_manifest_returns_unified_for_oci(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    registry_handler["handler"] = _manifest_and_config_handler(
        "manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value
    )

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "image"
    assert body["media_type"] == MediaType.OCI_IMAGE_MANIFEST.value
    # Image config was fetched and attached.
    assert body["config"]["data"]["architecture"] == "amd64"
    # Layers populated.
    assert len(body["layers"]) > 0
    # Pull command rendered.
    assert body["pull_command"] is not None
    assert body["pull_command"].startswith("docker pull ")
    assert "/foo:latest" in body["pull_command"]


def test_get_manifest_returns_unified_for_index(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    registry_handler["handler"] = _manifest_and_config_handler(
        "manifest_index", MediaType.OCI_IMAGE_INDEX.value
    )

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "index"
    assert len(body["platforms"]) == 2
    archs = {p["architecture"] for p in body["platforms"]}
    assert archs == {"amd64", "arm64"}


def test_get_manifest_pull_command_uses_at_for_digest_reference(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    registry_handler["handler"] = _manifest_and_config_handler(
        "manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value
    )

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/sha256:abcdef0123")
    body = response.json()
    assert "@sha256:abcdef0123" in body["pull_command"]


def test_get_manifest_404_propagates(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/missing")
    assert response.status_code == 404


# -- /config --------------------------------------------------------------


def test_get_manifest_config_endpoint(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    registry_handler["handler"] = _manifest_and_config_handler(
        "manifest_oci", MediaType.OCI_IMAGE_MANIFEST.value
    )

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest/config")
    assert response.status_code == 200
    config = response.json()
    assert config["architecture"] == "amd64"
    assert config["os"] == "linux"


def test_get_manifest_config_400_for_index(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    registry_handler["handler"] = _manifest_and_config_handler(
        "manifest_index", MediaType.OCI_IMAGE_INDEX.value
    )

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest/config")
    assert response.status_code == 400


# -- /referrers -----------------------------------------------------------


def test_get_referrers_returns_empty_when_endpoint_missing(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Most current registries don't implement OCI 1.1 referrers - soft-fail."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "/referrers/" in request.url.path:
            return httpx.Response(404)
        # HEAD on tag → digest header
        return httpx.Response(200, headers={"docker-content-digest": "sha256:abc"})

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/latest/referrers")
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_get_referrers_with_actual_referrers(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/referrers/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "manifests": [
                        {
                            "mediaType": "application/vnd.dev.cosign.simplesigning.v1+json",
                            "digest": "sha256:sig",
                            "size": 1234,
                            "artifactType": "application/vnd.dev.cosign.simplesigning.v1+json",
                        }
                    ]
                },
            )
        return httpx.Response(404)

    registry_handler["handler"] = handler

    with TestClient(app) as client:
        response = client.get("/api/repositories/foo/manifests/sha256:abc/referrers")
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["digest"] == "sha256:sig"


# -- DELETE /manifests/{reference} ----------------------------------------


def test_delete_manifest_disabled_by_default(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Public mode + no admin → ``require_admin`` returns 403."""
    with TestClient(app) as client:
        response = client.delete("/api/repositories/foo/manifests/latest")
    assert response.status_code == 403


def test_delete_manifest_when_enabled(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AUTH_MODE=admin`` + logged-in admin → DELETE succeeds."""
    from layerloupe.auth.env_provider import hash_password
    from layerloupe.config import get_settings

    password = "admin-pw"
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password(password, rounds=4))
    get_settings.cache_clear()

    digest = "sha256:" + "a" * 64
    requests_seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append((request.method, request.url.path))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"docker-content-digest": digest})
        if request.method == "DELETE":
            return httpx.Response(202)
        return httpx.Response(404)

    registry_handler["handler"] = handler

    try:
        with TestClient(app) as client:
            login = client.post(
                "/api/auth/ui-login",
                json={"username": "test-admin", "password": password},
            )
            assert login.status_code == 200, login.text
            response = client.delete("/api/repositories/foo/manifests/latest")
        assert response.status_code == 200
        assert response.json() == {"digest": digest}
        # Verify HEAD-then-DELETE-with-digest sequence.
        assert requests_seen[0][0] == "HEAD"
        assert requests_seen[1] == ("DELETE", f"/v2/foo/manifests/{digest}")
    finally:
        get_settings.cache_clear()


# -- /api/auth/login + /logout --------------------------------------------


def test_login_disabled_by_default(
    registry_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "alice", "password": "x"})
    assert response.status_code == 403


def test_login_validation_invalid_creds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Login enabled but registry rejects creds → 401."""
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    monkeypatch.setenv("REGISTRY_URL", "https://registry.example.com")
    from layerloupe.config import get_settings

    get_settings.cache_clear()

    # Patch _verify_credentials to return False without making real network calls.
    from layerloupe.api import auth as auth_module

    async def fake_verify(*args: object, **kwargs: object) -> bool:
        return False

    monkeypatch.setattr(auth_module, "_verify_credentials", fake_verify)

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/login", json={"username": "alice", "password": "wrong"}
            )
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()


def test_login_success_stores_username_in_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    monkeypatch.setenv("REGISTRY_URL", "https://registry.example.com")
    from layerloupe.config import get_settings

    get_settings.cache_clear()

    from layerloupe.api import auth as auth_module

    async def fake_verify(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(auth_module, "_verify_credentials", fake_verify)

    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/login", json={"username": "alice", "password": "good"}
            )
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "username": "alice"}
        # Cookie set?
        assert "session" in response.cookies or any(
            c.lower().startswith("session") for c in response.headers.get_list("set-cookie")
        )
    finally:
        get_settings.cache_clear()


def test_logout_clears_session() -> None:
    with TestClient(app) as client:
        response = client.post("/api/auth/logout")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# -- Bad payloads ---------------------------------------------------------


def test_login_rejects_empty_username() -> None:
    with TestClient(app) as client:
        response = client.post("/api/auth/login", json={"username": "", "password": "x"})
    # Pydantic min_length=1 → 422 validation error.
    assert response.status_code == 422
