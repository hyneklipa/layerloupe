import time

import httpx
import pytest

from layerloupe.registry import (
    BasicAuth,
    BearerAuth,
    BearerChallenge,
    RegistryClient,
    RegistryError,
    RegistryHTTPError,
    TokenCache,
    infer_scope,
    parse_bearer_challenge,
)
from layerloupe.registry.bearer import CachedToken

# -- Challenge parser -----------------------------------------------------


def test_parse_bearer_challenge_full() -> None:
    header = (
        'Bearer realm="https://auth.example.com/token",'
        'service="registry.example.com",scope="repository:foo:pull"'
    )
    challenge = parse_bearer_challenge(header)
    assert challenge == BearerChallenge(
        realm="https://auth.example.com/token",
        service="registry.example.com",
        scope="repository:foo:pull",
    )


def test_parse_bearer_challenge_without_scope() -> None:
    header = 'Bearer realm="https://auth.example.com/token",service="registry.example.com"'
    challenge = parse_bearer_challenge(header)
    assert challenge is not None
    assert challenge.scope is None


def test_parse_bearer_challenge_unquoted_values() -> None:
    """Some servers omit quotes around params (RFC 7235 allows it for tokens)."""
    header = "Bearer realm=https://auth.example.com,service=registry.example.com"
    challenge = parse_bearer_challenge(header)
    assert challenge is not None
    assert challenge.realm == "https://auth.example.com"
    assert challenge.service == "registry.example.com"


def test_parse_bearer_challenge_case_insensitive_scheme() -> None:
    header = 'BEARER realm="https://auth.example.com",service="x"'
    assert parse_bearer_challenge(header) is not None


def test_parse_bearer_challenge_basic_scheme_returns_none() -> None:
    assert parse_bearer_challenge('Basic realm="registry"') is None


def test_parse_bearer_challenge_missing_realm_returns_none() -> None:
    header = 'Bearer service="registry.example.com",scope="repository:foo:pull"'
    assert parse_bearer_challenge(header) is None


def test_parse_bearer_challenge_empty_returns_none() -> None:
    assert parse_bearer_challenge("") is None
    assert parse_bearer_challenge(None) is None


# -- Scope inference ------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/v2/", None),
        ("GET", "/v2", None),
        ("GET", "/v2/_catalog", "registry:catalog:*"),
        ("GET", "/v2/_catalog?n=100&last=foo", "registry:catalog:*"),
        ("GET", "/v2/foo/manifests/latest", "repository:foo:pull"),
        ("HEAD", "/v2/foo/manifests/sha256:abc", "repository:foo:pull"),
        ("GET", "/v2/library/ubuntu/manifests/22.04", "repository:library/ubuntu:pull"),
        ("GET", "/v2/foo/blobs/sha256:abc", "repository:foo:pull"),
        ("GET", "/v2/foo/tags/list", "repository:foo:pull"),
        ("DELETE", "/v2/foo/manifests/sha256:abc", "repository:foo:*"),
        ("GET", "/v2/foo/some-unknown-thing", None),
        ("GET", "/random/path", None),
    ],
)
def test_infer_scope(method: str, path: str, expected: str | None) -> None:
    assert infer_scope(method, path) == expected


# -- Token cache ----------------------------------------------------------


def test_token_cache_get_after_put() -> None:
    cache = TokenCache()
    cache.put("svc", "scope:foo:pull", "token-abc", ttl=60.0)
    cached = cache.get("svc", "scope:foo:pull")
    assert cached is not None
    assert cached.value == "token-abc"


def test_token_cache_miss_returns_none() -> None:
    cache = TokenCache()
    assert cache.get("svc", "missing") is None


def test_token_cache_expired_returns_none() -> None:
    cache = TokenCache()
    cache.put("svc", "scope", "token", ttl=-1.0)  # already expired
    assert cache.get("svc", "scope") is None


def test_token_cache_grace_period_treats_near_expiry_as_expired() -> None:
    """Tokens about to expire are considered expired (avoid mid-flight expiry)."""
    cache = TokenCache()
    cache.put("svc", "scope", "token", ttl=1.0)  # within 5s grace
    assert cache.get("svc", "scope") is None


def test_token_cache_find_any_matches_scope_across_services() -> None:
    cache = TokenCache()
    cache.put("svc-a", "scope:foo:pull", "token-1", ttl=60.0)
    cache.put("svc-b", "scope:bar:pull", "token-2", ttl=60.0)
    found = cache.find_any("scope:foo:pull")
    assert found is not None
    assert found.value == "token-1"


def test_token_cache_none_scope_normalized_to_empty_string() -> None:
    cache = TokenCache()
    cache.put("svc", None, "token", ttl=60.0)
    assert cache.get("svc", None) is not None
    assert cache.get("svc", "") is not None


def test_cached_token_is_expired_with_explicit_now() -> None:
    tok = CachedToken(value="x", expires_at=100.0)
    assert tok.is_expired(now=200.0)
    assert not tok.is_expired(now=50.0)


# -- End-to-end: 401 → token fetch → retry --------------------------------


REGISTRY_URL = "https://registry.example.com"
TOKEN_URL = "https://auth.example.com/token"


def _registry_handler(
    counters: dict[str, int],
    *,
    expected_token: str = "valid-token",
) -> "callable[[httpx.Request], httpx.Response]":  # type: ignore[name-defined]
    def handler(request: httpx.Request) -> httpx.Response:
        counters[request.method + " " + request.url.path] = (
            counters.get(request.method + " " + request.url.path, 0) + 1
        )
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {expected_token}":
            return httpx.Response(200, json={"repositories": ["foo"]})
        return httpx.Response(
            401,
            headers={
                "www-authenticate": (
                    f'Bearer realm="{TOKEN_URL}",service="registry.example.com",'
                    f'scope="registry:catalog:*"'
                )
            },
        )

    return handler


def _token_handler(
    counters: dict[str, int],
    *,
    token: str = "valid-token",
    expires_in: int = 300,
    expect_basic_creds: bool = False,
) -> "callable[[httpx.Request], httpx.Response]":  # type: ignore[name-defined]
    def handler(request: httpx.Request) -> httpx.Response:
        counters["token_calls"] = counters.get("token_calls", 0) + 1
        if expect_basic_creds:
            assert request.headers.get("authorization", "").startswith("Basic ")
        assert dict(request.url.params).get("service") == "registry.example.com"
        return httpx.Response(200, json={"token": token, "expires_in": expires_in})

    return handler


async def test_bearer_flow_401_fetches_token_and_retries() -> None:
    counters: dict[str, int] = {}
    bearer = BearerAuth(token_transport=httpx.MockTransport(_token_handler(counters)))

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        data = await client.get_json("/v2/_catalog")

    assert data == {"repositories": ["foo"]}
    assert counters["token_calls"] == 1
    # Original request was sent twice: 401 then retry-with-token.
    assert counters["GET /v2/_catalog"] == 2


async def test_bearer_flow_caches_token_across_requests() -> None:
    counters: dict[str, int] = {}
    bearer = BearerAuth(token_transport=httpx.MockTransport(_token_handler(counters)))

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        await client.get_json("/v2/_catalog")
        await client.get_json("/v2/_catalog")
        await client.get_json("/v2/_catalog")

    # First request: 401 + retry. Subsequent: pre-attached token, no 401.
    assert counters["token_calls"] == 1
    assert counters["GET /v2/_catalog"] == 1 + 1 + 1 + 1  # 1x401 + 3x success


async def test_bearer_flow_passes_basic_creds_to_token_server() -> None:
    counters: dict[str, int] = {}
    upstream = BasicAuth("alice", "s3cret")
    bearer = BearerAuth(
        upstream,
        token_transport=httpx.MockTransport(_token_handler(counters, expect_basic_creds=True)),
    )

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        await client.get_json("/v2/_catalog")

    assert counters["token_calls"] == 1


async def test_bearer_flow_basic_only_challenge_propagates_401() -> None:
    """Registry that only does Basic auth must surface its 401, not loop."""

    def registry_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, headers={"www-authenticate": 'Basic realm="registry"'})

    bearer = BearerAuth(token_transport=httpx.MockTransport(lambda r: httpx.Response(500)))

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(registry_handler),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_json("/v2/_catalog")
    assert exc_info.value.status_code == 401


async def test_bearer_flow_token_server_500_raises() -> None:
    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    counters: dict[str, int] = {}
    bearer = BearerAuth(token_transport=httpx.MockTransport(token_handler))

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        with pytest.raises(RegistryHTTPError) as exc_info:
            await client.get_json("/v2/_catalog")
    assert exc_info.value.status_code == 500


async def test_bearer_flow_token_response_missing_token_raises() -> None:
    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unrelated": "field"})

    counters: dict[str, int] = {}
    bearer = BearerAuth(token_transport=httpx.MockTransport(token_handler))

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        with pytest.raises(RegistryError, match=r"missing.*token"):
            await client.get_json("/v2/_catalog")


async def test_bearer_flow_accepts_access_token_alias() -> None:
    """Some auth servers return `access_token` instead of `token` (Docker Hub)."""

    def token_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "valid-token", "expires_in": 60})

    counters: dict[str, int] = {}
    bearer = BearerAuth(token_transport=httpx.MockTransport(token_handler))

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        data = await client.get_json("/v2/_catalog")
    assert data == {"repositories": ["foo"]}


async def test_bearer_aclose_closes_token_client() -> None:
    bearer = BearerAuth(token_transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert bearer._token_client.is_closed is False  # type: ignore[attr-defined]
    await bearer.aclose()
    assert bearer._token_client.is_closed is True  # type: ignore[attr-defined]


async def test_registry_client_aclose_closes_bearer_subclient() -> None:
    bearer = BearerAuth(token_transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    client = RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    await client.aclose()
    assert bearer._token_client.is_closed is True  # type: ignore[attr-defined]


# -- Cache TTL plumbing ---------------------------------------------------


async def test_token_ttl_from_response_expires_in() -> None:
    counters: dict[str, int] = {}
    bearer = BearerAuth(
        token_transport=httpx.MockTransport(_token_handler(counters, expires_in=300))
    )

    async with RegistryClient(
        REGISTRY_URL,
        auth=bearer,
        transport=httpx.MockTransport(_registry_handler(counters)),
    ) as client:
        await client.get_json("/v2/_catalog")

    cached = bearer.cache.get("registry.example.com", "registry:catalog:*")
    assert cached is not None
    # Should expire ~300s in the future, allow some slack for test execution.
    assert 250 < cached.expires_at - time.time() < 305
