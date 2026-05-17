"""Tests for empty/error states + 404/500 HTML pages."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import RegistryClient


@pytest.fixture
def web_handler() -> Iterator[dict[str, Callable[[httpx.Request], httpx.Response]]]:
    box: dict[str, Callable[[httpx.Request], httpx.Response]] = {
        "handler": lambda r: httpx.Response(200, json={"repositories": []})
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


# -- 404 differentiation: HTML for browser, JSON for /api/ ----------------


def test_404_for_browser_path_returns_html(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert "<!DOCTYPE html>" in body
    assert "404" in body
    assert "Page not found" in body
    # The brand chrome (topbar/footer) should still render so users see
    # they're still on the same site.
    assert 'class="topbar"' in body
    assert 'class="bottombar"' in body
    # Helpful "back to repositories" link.
    assert 'href="/"' in body


def test_404_for_api_path_returns_json() -> None:
    with TestClient(app) as client:
        response = client.get("/api/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "Not Found" in response.text


def test_404_for_web_path_returns_json() -> None:
    """``/web/`` is for htmx-mutating routes - htmx handles JSON details."""
    with TestClient(app) as client:
        response = client.delete("/web/repositories/foo/manifests/bogus-no-such")
    # Without allow_delete it's 403, but still JSON. Either way:
    # the content type must not be text/html.
    assert response.headers["content-type"].startswith("application/json")


def test_404_html_includes_path_in_message(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/some/missing/page")
    assert "/some/missing/page" in response.text


# -- 500 page (uncaught exception in a browser route) ---------------------


def test_500_for_browser_path_returns_html() -> None:
    """Register a route that intentionally raises, hit it, expect HTML 500."""
    boom_router = APIRouter()

    @boom_router.get("/boom-html")
    def _boom() -> None:
        raise RuntimeError("simulated explosion")

    app.include_router(boom_router)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom-html")
        assert response.status_code == 500
        assert response.headers["content-type"].startswith("text/html")
        body = response.text
        assert "Something went wrong" in body
        assert "500" in body
        # Detail (the exception message) is surfaced for debugging.
        assert "simulated explosion" in body
    finally:
        # Clean up so the boom route doesn't leak into other tests.
        app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") != "/boom-html"]


def test_500_for_api_path_returns_json() -> None:
    boom_router = APIRouter()

    @boom_router.get("/api/_boom_json")
    def _boom() -> None:
        raise RuntimeError("api explosion")

    app.include_router(boom_router)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/_boom_json")
        assert response.status_code == 500
        assert response.headers["content-type"].startswith("application/json")
        assert "api explosion" in response.text
    finally:
        app.router.routes = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/_boom_json"
        ]


# -- Empty repository list - first-run vs filtered -----------------------


def test_empty_repos_no_filter_renders_first_run_hint(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    web_handler["handler"] = lambda r: httpx.Response(200, json={"repositories": []})

    with TestClient(app) as client:
        body = client.get("/").text
    assert "no repositories yet" in body
    # Hint includes a docker push hint with the registry URL stripped of scheme.
    assert "docker push" in body


def test_empty_repos_with_filter_renders_no_match_message(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    web_handler["handler"] = lambda r: httpx.Response(
        200, json={"repositories": ["alpine", "redis"]}
    )

    with TestClient(app) as client:
        body = client.get("/", params={"q": "nonexistent"}).text
    assert "No repositories match" in body
    assert "<code>nonexistent</code>" in body
    # The "first run" copy should NOT appear when filter is active.
    assert "no repositories yet" not in body


# -- Empty tag list - repo-empty vs filter-no-match ----------------------


def test_empty_tags_no_filter(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["empty-repo"]})
        if request.url.path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "empty-repo", "tags": None})
        return httpx.Response(404)

    web_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/repositories/empty-repo/tags").text
    assert "has no tags" in body
    assert "<code>empty-repo</code>" in body


def test_empty_tags_with_filter(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if request.url.path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        return httpx.Response(404)

    web_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/repositories/foo/tags", params={"q": "no-such"}).text
    assert "No tags match" in body
    assert "<code>no-such</code>" in body


# -- Registry unreachable - error banner remains polite ------------------


def test_registry_unreachable_renders_friendly_error(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    web_handler["handler"] = boom

    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200  # shell still renders
    body = response.text
    assert "Could not load repository list" in body
    # The error appears in the page-level banner area.
    assert "error-banner" in body
    # No raw stack trace leaks out.
    assert "Traceback" not in body


# -- CSS hooks for the error pages exist ---------------------------------


def test_error_page_css_hooks_present() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert ".error-page" in css
    assert ".error-status" in css
    assert ".item-empty--hint" in css
