"""Tests for the topbar: identity pill, registry-creds pill, sign-out / sign-in.

Pre-redesign the topbar showed a single user pill driven by the
registry-credentials session key. The access-control redesign added a
second, orthogonal session key (UI identity) - the topbar now shows
two independent pills, plus a role badge on the identity pill.

The contract pinned here:

* Anonymous in ``public`` mode → no pills, no Sign-in link (nothing to log
  into).
* Anonymous in ``protected`` / ``admin`` mode → no pills, Sign-in link
  pointing at ``/login?next=<current path>``.
* UI identity present → identity pill with username + role badge
  (``admin`` for ``AUTH_MODE=admin``, ``viewer`` for ``protected``).
* Registry creds present → registry pill with the registry username.
* Both present → both pills, in order.
* Either present → single ``Sign out`` form that clears both.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from layerloupe.auth.env_provider import hash_password
from layerloupe.config import get_settings
from layerloupe.main import app

_ADMIN_PASSWORD = "admin-pw"
_ADMIN_PASSWORD_HASH = hash_password(_ADMIN_PASSWORD, rounds=4)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _login_ui(client: TestClient) -> None:
    r = client.post(
        "/api/auth/ui-login",
        json={"username": "test-admin", "password": _ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text


def _login_registry(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch out the upstream probe and post the registry-creds form."""
    from layerloupe.api import auth as auth_module

    async def fake_verify(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(auth_module, "_verify_credentials", fake_verify)
    monkeypatch.setattr("layerloupe.web.routes._verify_credentials", fake_verify)
    r = client.post("/login", data={"username": "reg-user", "password": "x"})
    # Registry login redirects on success; success is enough here.
    assert r.status_code in (200, 303)


def _set_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _ADMIN_PASSWORD_HASH)
    get_settings.cache_clear()


def _set_protected_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _ADMIN_PASSWORD_HASH)
    get_settings.cache_clear()


# -- Anonymous -----------------------------------------------------------


def test_public_mode_anonymous_no_pill_no_signin() -> None:
    """Pure public mode has nothing to sign into → no UI affordance at all."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert "user-pill" not in body
    assert "Sign in" not in body
    assert "Sign out" not in body


def test_anonymous_with_registry_login_shows_signin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        body = client.get("/").text
    assert "Sign in" in body
    assert 'href="/login?next=/"' in body


def test_anonymous_in_admin_mode_shows_signin(monkeypatch: pytest.MonkeyPatch) -> None:
    """``AUTH_MODE=admin`` makes login mandatory; the unauthenticated GET
    is bounced via redirect, but the redirect target /login itself
    suppresses its own Sign-in link, so we check from a different angle:
    the redirect URL must carry ``next=/``."""
    _set_admin_env(monkeypatch)
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
    assert r.status_code == 303
    assert "next=" in r.headers["location"]


# -- UI identity pill ----------------------------------------------------


def test_admin_session_shows_identity_pill_with_admin_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_admin_env(monkeypatch)
    with TestClient(app) as client:
        _login_ui(client)
        body = client.get("/").text
    assert "user-menu-row--identity" in body
    assert "test-admin" in body
    # Admin role → red admin badge.
    assert "role-badge--admin" in body
    assert ">admin<" in body
    assert "role-badge--viewer" not in body


def test_protected_session_shows_identity_pill_with_viewer_badge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In ``protected`` mode the logged-in user has an empty role-set -
    the topbar surfaces this as a ``viewer`` badge so the operator
    knows delete is unavailable to them."""
    _set_protected_env(monkeypatch)
    with TestClient(app) as client:
        _login_ui(client)
        body = client.get("/").text
    assert "user-menu-row--identity" in body
    assert "test-admin" in body
    assert "role-badge--viewer" in body
    assert "role-badge--admin" not in body


def test_identity_pill_persists_across_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pill is rendered by ``_shell_context``, which feeds every page
    route. A simple check that landing on different routes keeps the
    state visible - caches stale identity bugs."""
    _set_admin_env(monkeypatch)
    with TestClient(app) as client:
        _login_ui(client)
        for path in ["/", "/repositories"]:
            body = client.get(path).text
            assert "user-menu-row--identity" in body, path
            assert "test-admin" in body, path


# -- Registry creds pill ------------------------------------------------


def test_registry_login_shows_registry_pill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        _login_registry(client, monkeypatch)
        body = client.get("/").text
    assert "user-menu-row--registry" in body
    assert "reg-user" in body
    # No identity pill because there's no UI auth.
    assert "user-menu-row--identity" not in body


# -- Both pills at once -------------------------------------------------


def test_both_sessions_render_both_pills(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator logged into both UI and registry → two pills, one Sign-out
    button (which clears both)."""
    _set_admin_env(monkeypatch)
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        _login_ui(client)
        _login_registry(client, monkeypatch)
        body = client.get("/").text
    assert "user-menu-row--identity" in body
    assert "user-menu-row--registry" in body
    assert "test-admin" in body
    assert "reg-user" in body
    # Exactly one Sign-out form (single global logout target).
    assert body.count('action="/web/logout"') == 1


# -- Sign out form ------------------------------------------------------


def test_sign_out_form_targets_global_logout(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_admin_env(monkeypatch)
    with TestClient(app) as client:
        _login_ui(client)
        body = client.get("/").text
    assert 'action="/web/logout"' in body
    assert "Sign out" in body


def test_sign_out_clears_both_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single Sign-out is intentionally global - clears identity *and*
    registry creds. Per-flow logouts exist at /web/auth/logout for
    callers that want finer control."""
    _set_admin_env(monkeypatch)
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()
    with TestClient(app) as client:
        _login_ui(client)
        _login_registry(client, monkeypatch)
        # Confirm both are live.
        assert "test-admin" in client.get("/").text
        # Global logout.
        client.post("/web/logout")
        body = client.get("/", follow_redirects=False).text
        # In admin mode, the logged-out request bounces to /login;
        # the topbar isn't rendered on the redirect itself. The
        # important assertion is that the next browse never finds
        # the old session usernames.
        assert "test-admin" not in body
        assert "reg-user" not in body


# -- CSS hooks (regression guard) ---------------------------------------


def test_css_carries_account_menu_styles() -> None:
    """A future refactor that drops these classes would silently make
    the account menu / avatar look broken (unstyled badge, no dropdown)."""
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert ".avatar" in css
    assert ".user-menu" in css
    assert ".role-badge" in css
    assert ".role-badge--admin" in css
    assert ".role-badge--viewer" in css
