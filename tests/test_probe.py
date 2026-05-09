from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import (
    BearerAuth,
    RegistryClient,
    RegistryProbe,
)

# -- Pure unit: probe() ---------------------------------------------------


async def test_probe_returns_authenticated_on_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/"
        return httpx.Response(200, headers={"docker-distribution-api-version": "registry/2.0"})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        probe = await client.probe()

    assert probe.reachable is True
    assert probe.authenticated is True
    assert probe.status_code == 200
    assert probe.version == "registry/2.0"
    assert probe.error is None


async def test_probe_reports_bearer_challenge_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            headers={
                "docker-distribution-api-version": "registry/2.0",
                "www-authenticate": (
                    'Bearer realm="https://auth.example.com/token",service="registry.example.com"'
                ),
            },
        )

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        probe = await client.probe()

    assert probe.reachable is True
    assert probe.authenticated is False
    assert probe.status_code == 401
    assert probe.auth_scheme == "Bearer"
    assert probe.version == "registry/2.0"


async def test_probe_reports_basic_challenge_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="registry"'})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        probe = await client.probe()

    assert probe.reachable is True
    assert probe.authenticated is False
    assert probe.status_code == 401
    assert probe.auth_scheme == "Basic"


async def test_probe_handles_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with RegistryClient(
        "https://registry.example.com:5000",
        transport=httpx.MockTransport(handler),
    ) as client:
        probe = await client.probe()

    assert probe.reachable is False
    assert probe.authenticated is False
    assert probe.error is not None
    assert "refused" in probe.error.lower()


async def test_probe_handles_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("read timeout")

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
        timeout=0.1,
    ) as client:
        probe = await client.probe()

    assert probe.reachable is False
    assert "timeout" in (probe.error or "").lower()


async def test_probe_succeeds_when_bearer_auth_resolves_401() -> None:
    """If BearerAuth is configured and works, probe sees 200, not 401."""
    state = {"hit_count": 0}

    def registry_handler(request: httpx.Request) -> httpx.Response:
        state["hit_count"] += 1
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            return httpx.Response(200, headers={"docker-distribution-api-version": "registry/2.0"})
        return httpx.Response(
            401,
            headers={
                "www-authenticate": ('Bearer realm="https://auth.example.com/token",service="x"')
            },
        )

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": "valid", "expires_in": 60})

    bearer = BearerAuth(token_transport=httpx.MockTransport(token_handler))
    async with RegistryClient(
        "https://registry.example.com",
        auth=bearer,
        transport=httpx.MockTransport(registry_handler),
    ) as client:
        probe = await client.probe()

    assert probe.authenticated is True
    assert probe.status_code == 200


# -- /api/readyz endpoint -------------------------------------------------


@pytest.fixture
def override_registry() -> Iterator[dict[str, RegistryClient]]:
    """Yield a slot the test fills with a custom RegistryClient.

    The dependency override returns whatever the test put in ``box["client"]``
    so we can swap probe behavior per test without touching the lifespan.
    """
    box: dict[str, RegistryClient] = {}

    def _override() -> RegistryClient:
        return box["client"]

    app.dependency_overrides[get_registry_client] = _override
    try:
        yield box
    finally:
        app.dependency_overrides.pop(get_registry_client, None)


def test_readyz_returns_200_when_authenticated(
    override_registry: dict[str, RegistryClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"docker-distribution-api-version": "registry/2.0"})

    override_registry["client"] = RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as test_client:
        response = test_client.get("/api/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["registry"]["authenticated"] is True
    assert body["registry"]["version"] == "registry/2.0"


def test_readyz_returns_503_when_unauthenticated(
    override_registry: dict[str, RegistryClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="registry"'})

    override_registry["client"] = RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as test_client:
        response = test_client.get("/api/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["registry"]["authenticated"] is False
    assert body["registry"]["auth_scheme"] == "Basic"


def test_readyz_returns_503_when_unreachable(
    override_registry: dict[str, RegistryClient],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    override_registry["client"] = RegistryClient(
        "https://registry.example.com:5000",
        transport=httpx.MockTransport(handler),
    )

    with TestClient(app) as test_client:
        response = test_client.get("/api/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["registry"]["reachable"] is False
    assert "refused" in (body["registry"]["error"] or "").lower()


# -- RegistryProbe model --------------------------------------------------


def test_registry_probe_to_dict_round_trip() -> None:
    probe = RegistryProbe(
        reachable=True,
        authenticated=True,
        status_code=200,
        version="registry/2.0",
    )
    assert probe.to_dict() == {
        "reachable": True,
        "authenticated": True,
        "status_code": 200,
        "version": "registry/2.0",
        "auth_scheme": None,
        "error": None,
    }
