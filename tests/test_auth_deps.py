"""Tests for the identity / access-control FastAPI dependencies.

These exercise ``get_identity`` (cookie → ``Identity``) and the route
guards (``require_browse_access``, ``require_admin``) against the auth
mode matrix from the redesign:

* ``public``: anonymous OK, admin not required.
* ``protected``: login required, no role check beyond authenticated.
* ``admin``: login required, admin role required for the destructive
  route.

The tests build tiny standalone FastAPI apps so they're independent of
the real route layout — the real wiring lands in T7.6, where the same
guards get attached to actual ``/repositories/...`` endpoints.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from layerloupe.auth import ADMIN_ROLE
from layerloupe.auth.env_provider import hash_password
from layerloupe.config import get_settings
from layerloupe.deps import (
    AdminDep,
    BrowseAccessDep,
    get_identity,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear env between tests so ``Settings`` picks up exactly what we set."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def admin_hash() -> str:
    return hash_password("hunter2", rounds=4)


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app that exposes every dep under test.

    The session middleware uses a fixed secret so tests can also stamp
    identities into the session (via ``POST /_login_as``) and then probe
    the guards.
    """
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="deps-test-secret")

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, Any]:
        identity = get_identity(request)
        return {
            "username": identity.username,
            "provider": identity.provider,
            "roles": sorted(identity.roles),
            "is_anonymous": identity.is_anonymous,
            "is_admin": identity.is_admin,
        }

    @app.post("/_login_as")
    def login_as(request: Request, body: dict[str, Any]) -> dict[str, bool]:
        """Test helper — drop a payload straight into ``session["identity"]``.

        Lets tests construct exactly the cookie shape they want, including
        malformed payloads, without going through the real login flow."""
        request.session["identity"] = body.get("identity")
        return {"ok": True}

    @app.post("/_clear_session")
    def clear_session(request: Request) -> dict[str, bool]:
        request.session.clear()
        return {"ok": True}

    @app.get("/browse")
    def browse(identity: BrowseAccessDep) -> dict[str, str]:
        return {"username": identity.username}

    @app.get("/admin")
    def admin_only(identity: AdminDep) -> dict[str, str]:
        return {"username": identity.username}

    return app


def _login_admin(client: TestClient) -> None:
    client.post(
        "/_login_as",
        json={
            "identity": {
                "username": "alice",
                "roles": [ADMIN_ROLE],
                "provider": "env",
            }
        },
    )


def _login_viewer(client: TestClient) -> None:
    client.post(
        "/_login_as",
        json={
            "identity": {
                "username": "bob",
                "roles": ["viewer"],
                "provider": "oidc",
            }
        },
    )


# -- get_identity --------------------------------------------------------


def test_get_identity_without_session_payload_is_anonymous() -> None:
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/whoami")
    assert r.status_code == 200
    assert r.json()["is_anonymous"] is True
    assert r.json()["username"] == ""


def test_get_identity_reads_valid_session_payload() -> None:
    app = _make_app()
    with TestClient(app) as client:
        _login_admin(client)
        r = client.get("/whoami")
    body = r.json()
    assert body["username"] == "alice"
    assert body["provider"] == "env"
    assert body["is_admin"] is True
    assert body["is_anonymous"] is False


def test_get_identity_falls_back_on_malformed_payload() -> None:
    """Tampered / wrong-shape session payload → ``ANONYMOUS``, not crash."""
    app = _make_app()
    with TestClient(app) as client:
        client.post("/_login_as", json={"identity": {"username": 42, "roles": []}})
        r = client.get("/whoami")
    assert r.json()["is_anonymous"] is True


# -- require_browse_access ----------------------------------------------


def test_browse_access_public_mode_allows_anonymous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "public")
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/browse")
    assert r.status_code == 200
    assert r.json() == {"username": ""}


def test_browse_access_public_mode_allows_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "public")
    app = _make_app()
    with TestClient(app) as client:
        _login_admin(client)
        r = client.get("/browse")
    assert r.status_code == 200
    assert r.json()["username"] == "alice"


def test_browse_access_protected_mode_rejects_anonymous(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/browse")
    assert r.status_code == 401
    assert r.json() == {"detail": "Authentication required"}


def test_browse_access_protected_mode_allows_authenticated(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    app = _make_app()
    with TestClient(app) as client:
        _login_admin(client)
        r = client.get("/browse")
    assert r.status_code == 200


def test_browse_access_admin_mode_rejects_anonymous(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    """``admin`` mode also needs a login for plain browse (not just delete)."""
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/browse")
    assert r.status_code == 401


def test_browse_access_protected_mode_accepts_non_admin(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    """Browse only needs *authenticated*, not admin — a viewer-role
    identity (relevant once OIDC lands) gets through."""
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    app = _make_app()
    with TestClient(app) as client:
        _login_viewer(client)
        r = client.get("/browse")
    assert r.status_code == 200
    assert r.json()["username"] == "bob"


# -- require_admin ------------------------------------------------------


def test_admin_guard_rejects_anonymous_with_401() -> None:
    app = _make_app()
    with TestClient(app) as client:
        r = client.get("/admin")
    assert r.status_code == 401
    assert r.json() == {"detail": "Authentication required"}


def test_admin_guard_rejects_authenticated_non_admin_with_403() -> None:
    """Stale session against ``AUTH_MODE=protected`` mid-flight, or a
    non-admin OIDC group — both hit this path. Returns 403 (not 401)
    to distinguish "needs login" from "logged in, lacks role"."""
    app = _make_app()
    with TestClient(app) as client:
        _login_viewer(client)
        r = client.get("/admin")
    assert r.status_code == 403
    assert r.json() == {"detail": "Admin role required"}


def test_admin_guard_accepts_admin_role() -> None:
    app = _make_app()
    with TestClient(app) as client:
        _login_admin(client)
        r = client.get("/admin")
    assert r.status_code == 200
    assert r.json()["username"] == "alice"
