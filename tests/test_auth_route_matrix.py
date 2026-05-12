"""End-to-end access-control matrix.

The unit tests in ``test_auth_deps.py`` cover the dependencies in
isolation against tiny ad-hoc apps. This file pins down the *real*
app's behavior route-by-route — browse, fragment, delete — across the
``(auth_mode x authenticated x is_admin)`` matrix, plus the
HTML-vs-JSON discriminator.

If a future refactor accidentally drops a ``BrowseAccessDep`` /
``AdminDep`` somewhere, these tests are where it shows up first.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.auth.env_provider import hash_password
from layerloupe.config import get_settings
from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import MediaType, RegistryClient
from tests.conftest import load_fixture_bytes

_ADMIN_PASSWORD = "admin-pw"
_ADMIN_PASSWORD_HASH = hash_password(_ADMIN_PASSWORD, rounds=4)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def stub_registry() -> Iterator[None]:
    """Mount a MockTransport-backed registry client that answers basic GETs.

    Every browse-style endpoint hits the registry; without this stub
    they 503 before reaching the auth guard, which would mask whether
    the guard fired at all.
    """

    manifest_bytes = load_fixture_bytes("manifest_oci")
    config_bytes = load_fixture_bytes("image_config")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if request.method == "HEAD" and "/manifests/" in path:
            return httpx.Response(200, headers={"docker-content-digest": "sha256:" + "f" * 64})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": "sha256:" + "f" * 64,
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=config_bytes)
        return httpx.Response(404)

    def _override() -> RegistryClient:
        return RegistryClient(
            "https://registry.example.com",
            transport=httpx.MockTransport(handler),
        )

    app.dependency_overrides[get_registry_client] = _override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_registry_client, None)


def _set_mode(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    monkeypatch.setenv("AUTH_MODE", mode)
    if mode != "public":
        monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
        monkeypatch.setenv("ADMIN_PASSWORD_HASH", _ADMIN_PASSWORD_HASH)
    get_settings.cache_clear()


def _login_admin(client: TestClient) -> None:
    r = client.post(
        "/api/auth/ui-login",
        json={"username": "test-admin", "password": _ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text


# ----------------------------------------------------------------------
# Browse routes — gated by ``BrowseAccessDep`` (auth-mode-aware).
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/repositories",
        "/api/repositories/foo/tags",
        "/api/repositories/foo/manifests/latest",
    ],
)
def test_api_browse_public_mode_allows_anonymous(stub_registry: None, endpoint: str) -> None:
    """``public`` mode: every browse endpoint answers anonymous callers."""
    with TestClient(app) as client:
        r = client.get(endpoint)
    assert r.status_code == 200


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/repositories",
        "/api/repositories/foo/tags",
        "/api/repositories/foo/manifests/latest",
    ],
)
def test_api_browse_protected_mode_rejects_anonymous_with_401(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """``protected`` mode: anonymous browse → 401 + JSON detail (no redirect)."""
    _set_mode(monkeypatch, "protected")
    with TestClient(app) as client:
        r = client.get(endpoint)
    assert r.status_code == 401
    assert r.json() == {"detail": "Authentication required"}


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/repositories",
        "/api/repositories/foo/tags",
        "/api/repositories/foo/manifests/latest",
    ],
)
def test_api_browse_admin_mode_rejects_anonymous_with_401(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    _set_mode(monkeypatch, "admin")
    with TestClient(app) as client:
        r = client.get(endpoint)
    assert r.status_code == 401


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/repositories",
        "/api/repositories/foo/tags",
        "/api/repositories/foo/manifests/latest",
    ],
)
def test_api_browse_protected_mode_allows_authenticated(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    _set_mode(monkeypatch, "protected")
    with TestClient(app) as client:
        _login_admin(client)
        r = client.get(endpoint)
    assert r.status_code == 200


# ----------------------------------------------------------------------
# HTML routes — same guard, but 401 → redirect to /login?next=...
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/", "/repositories", "/repositories/foo/tags"],
)
def test_html_browse_protected_mode_redirects_anonymous_to_login(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """The global handler converts 401 on HTML routes into a 303 to /login."""
    _set_mode(monkeypatch, "protected")
    with TestClient(app) as client:
        r = client.get(path, follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/login?next=")
    # The ``next`` param round-trips back to the requested path so the
    # user lands here after signing in.
    assert path in location or path.replace("/", "%2F") in location


def test_html_browse_public_mode_allows_anonymous(stub_registry: None) -> None:
    with TestClient(app) as client:
        r = client.get("/")
    assert r.status_code == 200


def test_html_browse_admin_mode_redirects_anonymous_with_query(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``next=`` echoes back path + query so deep links survive login."""
    _set_mode(monkeypatch, "admin")
    with TestClient(app) as client:
        r = client.get("/repositories/foo/tags?q=alpine", follow_redirects=False)
    assert r.status_code == 303
    assert "next=" in r.headers["location"]


# ----------------------------------------------------------------------
# Delete routes — gated by ``AdminDep``: requires admin mode + admin role.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/repositories/foo/manifests/latest",
        "/web/repositories/foo/manifests/latest",
    ],
)
def test_delete_public_mode_returns_403(stub_registry: None, endpoint: str) -> None:
    """Public mode: delete isn't a concept → 403 (not 401 → no useless
    login redirect)."""
    with TestClient(app) as client:
        r = client.delete(endpoint)
    assert r.status_code == 403


@pytest.mark.parametrize(
    "endpoint",
    [
        "/api/repositories/foo/manifests/latest",
        "/web/repositories/foo/manifests/latest",
    ],
)
def test_delete_protected_mode_returns_403_even_for_logged_in(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
    endpoint: str,
) -> None:
    """``protected`` mode + logged-in admin account: still 403, because the
    granted role-set is empty in protected mode — the credential is the
    admin account but the identity is plain "authenticated"."""
    _set_mode(monkeypatch, "protected")
    with TestClient(app) as client:
        _login_admin(client)
        r = client.delete(endpoint)
    assert r.status_code == 403


def test_delete_admin_mode_anonymous_returns_401(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "admin")
    with TestClient(app) as client:
        r = client.delete("/api/repositories/foo/manifests/latest")
    assert r.status_code == 401


def test_delete_admin_mode_logged_in_returns_200(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_mode(monkeypatch, "admin")
    with TestClient(app) as client:
        _login_admin(client)
        r = client.delete("/api/repositories/foo/manifests/latest")
    assert r.status_code == 200


# ----------------------------------------------------------------------
# Healthchecks & static — must remain unauthenticated everywhere.
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode",
    ["public", "protected", "admin"],
)
def test_healthz_is_never_gated(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    _set_mode(monkeypatch, mode)
    with TestClient(app) as client:
        r = client.get("/api/healthz")
    assert r.status_code == 200


def test_static_assets_never_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_mode(monkeypatch, "protected")
    with TestClient(app) as client:
        r = client.get("/static/layerloupe.css")
    assert r.status_code == 200


# ----------------------------------------------------------------------
# Topbar / shell context: ``is_admin`` reflects current session, not env.
# ----------------------------------------------------------------------


def test_shell_context_is_admin_reflects_session(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trash-icon trigger in the manifest column header only renders
    when ``is_admin`` is true on the current request — which means the
    user is logged in *and* the mode is admin."""
    _set_mode(monkeypatch, "admin")
    with TestClient(app) as client:
        # Anonymous: render the login redirect happens, but the shell
        # itself never renders. We verify the post-login state has the icon.
        _login_admin(client)
        body = client.get("/repositories/foo/manifests/latest").text
    # The trash-icon button is present when is_admin is true.
    assert "icon-btn--danger" in body


def test_shell_context_no_admin_icon_in_protected_mode(
    stub_registry: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Logged-in user in protected mode → no admin role → no trash icon
    even though they're "authenticated"."""
    _set_mode(monkeypatch, "protected")
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/repositories/foo/manifests/latest").text
    assert "icon-btn--danger" not in body
