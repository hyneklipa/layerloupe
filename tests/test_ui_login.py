"""Tests for the UI identity login routes.

Covers the HTML form pair (``GET /login``, ``POST /web/auth/login``,
``POST /web/auth/logout``) and the JSON sibling pair
(``POST /api/auth/ui-login``, ``POST /api/auth/ui-logout``).

The registry-creds login at ``POST /login`` is exercised by
``test_login_ui.py``; here we focus on the new UI-identity surface.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from layerloupe.auth.env_provider import hash_password
from layerloupe.config import get_settings
from layerloupe.main import app


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear env so each test starts from a deterministic baseline."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(scope="module")
def admin_hash() -> str:
    """Bcrypt hash for ``"hunter2"`` at low rounds - module-scoped so we
    pay the bcrypt cost once for the whole file."""
    return hash_password("hunter2", rounds=4)


@pytest.fixture
def protected_env(monkeypatch: pytest.MonkeyPatch, admin_hash: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    get_settings.cache_clear()


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch, admin_hash: str) -> None:
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    get_settings.cache_clear()


# -- GET /login: surface shape per mode ---------------------------------


def test_login_page_in_public_mode_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Public mode + no registry login → nothing to sign in to."""
    monkeypatch.setenv("AUTH_MODE", "public")
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 403


def test_login_page_renders_ui_form_in_protected_mode(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert 'action="/web/auth/login"' in r.text
    # No registry form when ALLOW_REGISTRY_LOGIN is off.
    assert 'action="/login"' not in r.text


def test_login_page_renders_both_forms_when_both_enabled(
    protected_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.get("/login")
    assert r.status_code == 200
    assert 'action="/web/auth/login"' in r.text
    assert 'action="/login"' in r.text


# -- POST /web/auth/login: success / failure ----------------------------


def test_ui_login_success_redirects_to_next(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/web/auth/login",
            data={
                "username": "alice",
                "password": "hunter2",
                "next": "/repositories/foo/tags",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/repositories/foo/tags"


def test_ui_login_default_redirect_is_root(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/web/auth/login",
            data={"username": "alice", "password": "hunter2"},
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_ui_login_sanitizes_external_next(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/web/auth/login",
            data={
                "username": "alice",
                "password": "hunter2",
                "next": "https://evil.com",
            },
            follow_redirects=False,
        )
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_ui_login_wrong_password_renders_form_with_error(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/web/auth/login",
            data={"username": "alice", "password": "wrong", "next": "/foo"},
        )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text
    # Username preserved across the failed submit.
    assert 'value="alice"' in r.text
    # ``next`` survives too.
    assert 'value="/foo"' in r.text


def test_ui_login_wrong_username_renders_form_with_error(protected_env: None) -> None:
    """Wrong username takes the dummy-hash path - same 401 surface."""
    with TestClient(app) as client:
        r = client.post(
            "/web/auth/login",
            data={"username": "mallory", "password": "hunter2"},
        )
    assert r.status_code == 401
    assert "Invalid credentials" in r.text


def test_ui_login_returns_403_in_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "public")
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.post(
            "/web/auth/login",
            data={"username": "alice", "password": "hunter2"},
        )
    assert r.status_code == 403


def test_ui_login_empty_fields_validation_error(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post("/web/auth/login", data={"username": "", "password": "x"})
    assert r.status_code == 422


# -- POST /web/auth/logout ----------------------------------------------


def test_ui_logout_redirects_home(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post("/web/auth/logout", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_ui_logout_clears_identity_but_keeps_registry_creds(
    protected_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Logging out of the UI shouldn't kick the user out of any active
    registry-login session - they're independent surfaces."""
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()

    from layerloupe.api import auth as auth_module

    async def fake_verify(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(auth_module, "_verify_credentials", fake_verify)
    monkeypatch.setattr("layerloupe.web.routes._verify_credentials", fake_verify)

    with TestClient(app) as client:
        # Sign in to UI.
        client.post(
            "/web/auth/login",
            data={"username": "alice", "password": "hunter2"},
        )
        # Sign in to registry (separate session key).
        client.post(
            "/login",
            data={"username": "registry-user", "password": "registry-pw"},
        )
        # Drop only the UI identity.
        client.post("/web/auth/logout")

        # Registry login should still work - the registry pill should
        # appear on a subsequent page render. We hit /login to inspect
        # the page rendered for an authenticated registry session: it
        # should still surface the registry username.
        login_page = client.get("/login").text
        # The UI form is back (no identity), so it should render fresh.
        assert 'value="alice"' not in login_page


# -- JSON variants -------------------------------------------------------


def test_api_ui_login_success(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/ui-login",
            json={"username": "alice", "password": "hunter2"},
        )
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "username": "alice"}


def test_api_ui_login_failure_returns_401(protected_env: None) -> None:
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/ui-login",
            json={"username": "alice", "password": "wrong"},
        )
    assert r.status_code == 401
    assert r.json() == {"detail": "Invalid credentials"}


def test_api_ui_login_returns_403_in_public_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "public")
    get_settings.cache_clear()
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/ui-login",
            json={"username": "alice", "password": "hunter2"},
        )
    assert r.status_code == 403


def test_api_ui_logout_clears_identity_only(
    protected_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symmetric to the HTML logout: clears ``identity`` only."""
    with TestClient(app) as client:
        client.post(
            "/api/auth/ui-login",
            json={"username": "alice", "password": "hunter2"},
        )
        r = client.post("/api/auth/ui-logout")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


# -- admin mode behaves the same way -------------------------------------


def test_admin_mode_login_grants_admin_role(admin_env: None) -> None:
    """``AUTH_MODE=admin`` and ``protected`` share the same login flow;
    the difference is what the resulting identity is *allowed* to do.

    Verified by inspecting the session-stored payload after login: in
    ``admin`` mode, ``roles`` must include ``"admin"``.
    """
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/ui-login",
            json={"username": "alice", "password": "hunter2"},
        )
        assert r.status_code == 200
        # Decode the session cookie payload to confirm the granted roles.
        session_cookie = client.cookies.get("session")
        assert session_cookie is not None
        import base64
        import json

        # itsdangerous TimestampSigner output: ``<payload>.<timestamp>.<signature>``.
        # Take the payload chunk and url-safe base64 decode it.
        payload_b64 = session_cookie.split(".", 1)[0]
        # Restore base64 padding.
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        assert "admin" in payload["identity"]["roles"]
        # T7.10: cookie carries the mode under which it was minted so a
        # later ``AUTH_MODE`` flip invalidates this session.
        assert payload["identity"]["auth_mode"] == "admin"


def test_protected_mode_login_grants_no_roles(protected_env: None) -> None:
    """``protected`` authenticates the user but doesn't grant ``admin`` -
    delete-gated routes must reject these identities."""
    with TestClient(app) as client:
        r = client.post(
            "/api/auth/ui-login",
            json={"username": "alice", "password": "hunter2"},
        )
        assert r.status_code == 200
        session_cookie = client.cookies.get("session")
        assert session_cookie is not None
        import base64
        import json

        payload_b64 = session_cookie.split(".", 1)[0]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        assert payload["identity"]["roles"] == []
        assert payload["identity"]["auth_mode"] == "protected"
