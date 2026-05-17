from collections.abc import Callable

import httpx
import pytest

from layerloupe.registry import (
    RegistryClient,
    RegistryConnectionError,
    RegistryHTTPError,
)


def _mock_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _make_client(
    handler: Callable[[httpx.Request], httpx.Response], **kwargs: object
) -> RegistryClient:
    return RegistryClient(
        kwargs.pop("base_url", "https://registry.example.com"),  # type: ignore[arg-type]
        transport=_mock_transport(handler),
        **kwargs,  # type: ignore[arg-type]
    )


# -- Construction ---------------------------------------------------------


def test_verify_false_propagates_to_ssl_context() -> None:
    """`verify=False` must reach the underlying SSL context.

    httpx still creates a context, but with verify_mode=CERT_NONE and
    check_hostname=False - the only states a self-signed registry will accept.
    """
    import ssl

    client = RegistryClient("https://registry.example.com", verify=False)
    assert client.verify is False
    ctx = client._client._transport._pool._ssl_context  # type: ignore[attr-defined]
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


def test_verify_true_default_enforces_certs() -> None:
    import ssl

    client = RegistryClient("https://registry.example.com")
    assert client.verify is True
    ctx = client._client._transport._pool._ssl_context  # type: ignore[attr-defined]
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_base_url_property() -> None:
    client = RegistryClient("https://registry.example.com:5000")
    assert client.base_url == "https://registry.example.com:5000"


def test_timeout_property() -> None:
    client = RegistryClient("https://registry.example.com", timeout=5.5)
    assert client.timeout == 5.5


# -- get_json happy path --------------------------------------------------


async def test_get_json_returns_parsed_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v2/_catalog"
        return httpx.Response(200, json={"repositories": ["foo", "bar"]})

    async with _make_client(handler) as client:
        data = await client.get_json("/v2/_catalog")
    assert data == {"repositories": ["foo", "bar"]}


async def test_get_json_passes_query_params() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"n": "100", "last": "foo"}
        return httpx.Response(200, json={"repositories": []})

    async with _make_client(handler) as client:
        await client.get_json("/v2/_catalog", params={"n": 100, "last": "foo"})


async def test_get_json_per_request_headers_merge_with_defaults() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={})

    async with _make_client(
        handler,
        default_headers={"Authorization": "Basic Zm9vOmJhcg==", "User-Agent": "layerloupe/test"},
    ) as client:
        await client.get_json("/v2/_catalog", headers={"Accept": "application/json"})

    assert seen["authorization"] == "Basic Zm9vOmJhcg=="
    assert seen["user-agent"] == "layerloupe/test"
    assert seen["accept"] == "application/json"


async def test_per_request_header_overrides_default() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={})

    async with _make_client(handler, default_headers={"Accept": "application/json"}) as client:
        await client.get_json(
            "/v2/foo", headers={"Accept": "application/vnd.oci.image.index.v1+json"}
        )

    assert seen["accept"] == "application/vnd.oci.image.index.v1+json"


# -- get_json error paths -------------------------------------------------


async def test_get_json_raises_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "NOT_FOUND"}]})

    async with _make_client(handler) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_json("/v2/missing/manifests/latest")

    assert exc_info.value.status_code == 404
    assert "NOT_FOUND" in exc_info.value.body


async def test_get_json_raises_on_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="registry"'})

    async with _make_client(handler) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_json("/v2/_catalog")
    assert exc_info.value.status_code == 401


async def test_get_json_raises_on_non_json_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    async with _make_client(handler) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_json("/v2/_catalog")
    assert "non-JSON" in str(exc_info.value)


async def test_get_json_raises_on_array_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["a", "b"])

    async with _make_client(handler) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_json("/v2/_catalog")
    assert "non-object" in str(exc_info.value)


# -- head / delete --------------------------------------------------------


async def test_head_returns_response_with_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"docker-content-digest": "sha256:abc123"})

    async with _make_client(handler) as client:
        response = await client.head("/v2/foo/manifests/latest")
    assert response.status_code == 200
    assert response.headers["docker-content-digest"] == "sha256:abc123"


async def test_head_raises_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    async with _make_client(handler) as client:
        with pytest.raises(RegistryHTTPError):
            await client.head("/v2/foo/manifests/missing")


async def test_delete_returns_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v2/foo/manifests/sha256:abc"
        return httpx.Response(202)

    async with _make_client(handler) as client:
        response = await client.delete("/v2/foo/manifests/sha256:abc")
    assert response.status_code == 202


async def test_delete_raises_on_405_method_not_allowed() -> None:
    """Some registries return 405 when delete isn't enabled."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(405, json={"errors": [{"code": "UNSUPPORTED"}]})

    async with _make_client(handler) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.delete("/v2/foo/manifests/sha256:abc")
    assert exc_info.value.status_code == 405


# -- Connection-level failures --------------------------------------------


async def test_timeout_raises_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("simulated timeout", request=request)

    async with _make_client(handler) as client:
        with pytest.raises(RegistryConnectionError) as exc_info:
            await client.get_json("/v2/_catalog")
    assert "Timeout" in str(exc_info.value)


async def test_network_error_raises_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _make_client(handler) as client:
        with pytest.raises(RegistryConnectionError):
            await client.get_json("/v2/_catalog")


# -- Async context manager ------------------------------------------------


async def test_aclose_closes_underlying_client() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _make_client(handler)
    assert client._client.is_closed is False  # type: ignore[attr-defined]
    await client.aclose()
    assert client._client.is_closed is True  # type: ignore[attr-defined]


async def test_async_context_manager_closes_on_exit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _make_client(handler)
    async with client:
        pass
    assert client._client.is_closed is True  # type: ignore[attr-defined]
