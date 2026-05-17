"""Health / readiness endpoint hardening.

On top of the basic 200/503 contract, the endpoints add:

* ``Cache-Control: no-store`` so a flaky proxy can't return last-known-good
  liveness for a process that's actually down.
* Filtering ``/api/healthz`` + ``/api/readyz`` out of the structured access
  log - k8s probes hit them every few seconds and would otherwise drown
  production stdout.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.logging import configure_logging
from layerloupe.main import app
from layerloupe.registry import RegistryClient


@pytest.fixture
def healthy_registry() -> Iterator[None]:
    """Override the registry client so /readyz can probe a fake healthy server."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/":
            return httpx.Response(200, headers={"docker-distribution-api-version": "registry/2.0"})
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


@pytest.fixture
def unreachable_registry() -> Iterator[None]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage")

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


# -- Liveness contract ----------------------------------------------------


def test_healthz_always_returns_200() -> None:
    """Liveness must succeed regardless of registry state."""
    with TestClient(app) as client:
        response = client.get("/api/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "version" in body


def test_healthz_carries_no_store_cache_control() -> None:
    with TestClient(app) as client:
        response = client.get("/api/healthz")
    assert response.headers.get("cache-control") == "no-store"


# -- Readiness contract --------------------------------------------------


def test_readyz_returns_200_when_registry_healthy(healthy_registry: None) -> None:
    with TestClient(app) as client:
        response = client.get("/api/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["registry"]["authenticated"] is True


def test_readyz_returns_503_when_registry_unreachable(
    unreachable_registry: None,
) -> None:
    """An upstream outage must surface as 503."""
    with TestClient(app) as client:
        response = client.get("/api/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["registry"]["reachable"] is False


def test_readyz_carries_no_store_cache_control(healthy_registry: None) -> None:
    with TestClient(app) as client:
        response = client.get("/api/readyz")
    assert response.headers.get("cache-control") == "no-store"


def test_readyz_503_also_carries_no_store(unreachable_registry: None) -> None:
    """Cache headers must apply on the failure path too."""
    with TestClient(app) as client:
        response = client.get("/api/readyz")
    assert response.headers.get("cache-control") == "no-store"


# -- Probe paths are filtered from the access log ------------------------


def _capture_logs(
    client_call: Callable[[TestClient], httpx.Response],
    capfd: pytest.CaptureFixture[str],
) -> list[dict[str, object]]:
    configure_logging(level="info", json=True)
    with TestClient(app) as test_client:
        configure_logging(level="info", json=True)  # lifespan reconfigured; redo
        capfd.readouterr()  # discard startup chatter
        client_call(test_client)
    out, _ = capfd.readouterr()
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_healthz_does_not_emit_access_log(
    capfd: pytest.CaptureFixture[str],
    healthy_registry: None,
) -> None:
    """``/api/healthz`` is hit every few seconds - silence its access line."""
    try:
        entries = _capture_logs(lambda c: c.get("/api/healthz"), capfd)
        completed = [e for e in entries if e.get("event") == "request_completed"]
        assert completed == []
    finally:
        structlog.reset_defaults()


def test_readyz_does_not_emit_access_log(
    capfd: pytest.CaptureFixture[str],
    healthy_registry: None,
) -> None:
    try:
        entries = _capture_logs(lambda c: c.get("/api/readyz"), capfd)
        completed = [e for e in entries if e.get("event") == "request_completed"]
        assert completed == []
    finally:
        structlog.reset_defaults()


def test_other_endpoints_still_log(
    capfd: pytest.CaptureFixture[str],
    healthy_registry: None,
) -> None:
    """Sanity: filtering is path-specific, not a global mute."""
    try:
        entries = _capture_logs(lambda c: c.get("/api/info"), capfd)
        completed = [e for e in entries if e.get("event") == "request_completed"]
        assert len(completed) == 1
        assert completed[0]["path"] == "/api/info"
    finally:
        structlog.reset_defaults()


# -- Probes still get an X-Request-ID for inner-log correlation ----------


def test_healthz_still_has_request_id_header() -> None:
    """We don't log the access line, but inbound tracing still works:
    the response carries a request-id so caller logs can correlate."""
    with TestClient(app) as client:
        response = client.get("/api/healthz")
    assert "x-request-id" in response.headers
