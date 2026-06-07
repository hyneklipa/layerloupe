"""Tests for the static UI shell.

Verifies that the home page renders, the static assets are mounted, and
the dark-mode toggle plumbing is present in the markup.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from layerloupe.config import get_settings
from layerloupe.main import app

# -- Home page renders ---------------------------------------------------


def test_index_returns_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TITLE", "LayerLoupe Test")
    monkeypatch.setenv("REGISTRY_URL", "https://registry.example.com")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            response = client.get("/")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "<!DOCTYPE html>" in body
        assert "LayerLoupe Test" in body
        assert "registry.example.com" in body
    finally:
        get_settings.cache_clear()


def test_index_links_to_static_css_and_js() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    # Site-relative paths so a TLS-terminating proxy (Traefik etc.)
    # doesn't introduce mixed-content from upstream ``http://``.
    assert 'href="/static/layerloupe.css"' in body
    assert 'src="/static/layerloupe.js"' in body
    assert 'href="/static/favicon.svg"' in body


def test_index_includes_brand_and_footer() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'class="topbar"' in body
    assert 'class="bottombar"' in body
    # Three-column placeholder visible on the shell.
    assert ">Repositories<" in body
    assert ">Tags<" in body
    assert ">Manifest<" in body


# -- Dark mode toggle present and functional ------------------------------


def test_index_has_theme_toggle_button() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'id="theme-toggle"' in body
    # Pre-paint script must set data-theme before any HTML renders to avoid FOUC.
    assert "data-theme" in body
    assert "prefers-color-scheme" in body
    assert "localStorage" in body


def test_index_html_has_data_theme_attribute() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    # The opening <html> tag should carry data-theme.
    match = re.search(r"<html[^>]*>", body)
    assert match is not None
    assert "data-theme=" in match.group(0)


# -- Sign-in / sign-out visibility gated by setting -----------------------


def test_signin_link_hidden_when_login_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALLOW_REGISTRY_LOGIN", raising=False)
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            body = client.get("/").text
        assert "Sign in" not in body
        assert "Sign out" not in body
    finally:
        get_settings.cache_clear()


def test_signin_link_visible_when_login_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ALLOW_REGISTRY_LOGIN", "true")
    get_settings.cache_clear()
    try:
        with TestClient(app) as client:
            body = client.get("/").text
        assert "Sign in" in body
    finally:
        get_settings.cache_clear()


# -- Static asset serving -------------------------------------------------


def test_static_css_served() -> None:
    with TestClient(app) as client:
        response = client.get("/static/layerloupe.css")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/css")
    assert "--ll-blue" in response.text  # design-token sanity check


def test_static_js_served() -> None:
    with TestClient(app) as client:
        response = client.get("/static/layerloupe.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "theme-toggle" in response.text


def test_static_favicon_served() -> None:
    with TestClient(app) as client:
        response = client.get("/static/favicon.svg")
    assert response.status_code == 200
    assert "svg" in response.headers["content-type"].lower()


def test_static_404_for_missing_file() -> None:
    with TestClient(app) as client:
        response = client.get("/static/does-not-exist.css")
    assert response.status_code == 404


# -- Web routes are NOT in the OpenAPI schema -----------------------------


def test_index_excluded_from_openapi() -> None:
    """The HTML routes shouldn't pollute the REST OpenAPI doc."""
    with TestClient(app) as client:
        spec = client.get("/openapi.json").json()
    assert "/" not in spec["paths"]
