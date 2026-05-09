"""Tests for the TTL cache layer in front of ``RegistryClient``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
import pytest

from layerloupe.registry import (
    ManifestKind,
    ManifestResponse,
    MediaType,
    RegistryClient,
    TTLCache,
    to_unified,
)
from layerloupe.registry.cache import TTLCache as _TTLCacheImpl

# -- TTLCache primitives --------------------------------------------------


def test_cache_miss_returns_false() -> None:
    cache = TTLCache()
    hit, value = cache.get("nope")
    assert hit is False
    assert value is None


def test_cache_hit_after_set() -> None:
    cache = TTLCache()
    cache.set("k", [1, 2, 3], ttl=60.0)
    hit, value = cache.get("k")
    assert hit is True
    assert value == [1, 2, 3]


def test_cache_expired_entry_returns_miss() -> None:
    cache = TTLCache()
    cache.set("k", "v", ttl=-1.0)  # already in the past
    hit, value = cache.get("k")
    assert hit is False
    assert value is None
    # And the expired entry was pruned on read.
    assert len(cache) == 0


def test_cache_invalidate() -> None:
    cache = TTLCache()
    cache.set("k", 1, ttl=60.0)
    cache.invalidate("k")
    hit, _ = cache.get("k")
    assert hit is False


def test_cache_clear() -> None:
    cache = TTLCache()
    for i in range(5):
        cache.set(i, i, ttl=60.0)
    cache.clear()
    assert len(cache) == 0


def test_cache_overwrite_does_not_evict() -> None:
    cache = TTLCache(max_size=2)
    cache.set("a", 1, ttl=60.0)
    cache.set("b", 2, ttl=60.0)
    cache.set("a", 11, ttl=60.0)  # update, not insert
    assert len(cache) == 2
    hit, value = cache.get("a")
    assert hit is True
    assert value == 11


def test_cache_eviction_when_full() -> None:
    cache = TTLCache(max_size=2)
    cache.set("a", 1, ttl=60.0)
    cache.set("b", 2, ttl=60.0)
    cache.set("c", 3, ttl=60.0)  # evicts oldest
    assert len(cache) == 2


def test_cache_eviction_prefers_expired_entries() -> None:
    """Eviction scans for an already-expired key first."""
    cache = TTLCache(max_size=2)
    cache.set("expired", "x", ttl=-1.0)
    cache.set("fresh", "y", ttl=60.0)
    cache.set("new", "z", ttl=60.0)  # should drop the expired one
    assert len(cache) == 2
    fresh_hit, _ = cache.get("fresh")
    new_hit, _ = cache.get("new")
    assert fresh_hit is True
    assert new_hit is True


# -- End-to-end: cache hits skip registry calls ---------------------------


def _instrumented_handler(
    counter: dict[str, int],
) -> Callable[[httpx.Request], httpx.Response]:
    """Counts calls to /v2/_catalog and the manifest path."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        counter[path] = counter.get(path, 0) + 1
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["alpine", "redis"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "x", "tags": ["latest", "1.0"]})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=b'{"schemaVersion":2,"mediaType":"application/vnd.oci.image.manifest.v1+json","config":{"mediaType":"application/vnd.oci.image.config.v1+json","digest":"sha256:cfg","size":1},"layers":[]}',
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": "sha256:abc",
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=b'{"architecture":"amd64","os":"linux"}')
        return httpx.Response(404)

    return handler


async def test_iter_repositories_caches_full_list() -> None:
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
    ) as client:
        first = [r async for r in client.iter_repositories()]
        second = [r async for r in client.iter_repositories()]

    assert first == second == ["alpine", "redis"]
    # Acceptance criterion: the second call hit the cache, not the registry.
    assert counter["/v2/_catalog"] == 1


async def test_iter_repositories_cache_disabled_by_default() -> None:
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
    ) as client:
        async for _ in client.iter_repositories():
            pass
        async for _ in client.iter_repositories():
            pass
    # Cache is opt-in (cache_ttl=0); both calls touch the registry.
    assert counter["/v2/_catalog"] == 2


async def test_iter_repositories_query_filter_replays_against_cache() -> None:
    """Different ``query`` values share the unfiltered cache entry."""
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
    ) as client:
        # First fetch (no filter): warms the cache.
        all_repos = [r async for r in client.iter_repositories()]
        # Subsequent calls with different filters reuse the cached fetch.
        red = [r async for r in client.iter_repositories(query="red")]
        alp = [r async for r in client.iter_repositories(query="alp")]

    assert all_repos == ["alpine", "redis"]
    assert red == ["redis"]
    assert alp == ["alpine"]
    # Three logical calls, one registry hit.
    assert counter["/v2/_catalog"] == 1


async def test_iter_tags_caches_full_list() -> None:
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
    ) as client:
        first = [t async for t in client.iter_tags("foo")]
        second = [t async for t in client.iter_tags("foo")]

    assert first == second == ["latest", "1.0"]
    assert counter["/v2/foo/tags/list"] == 1


async def test_iter_tags_separate_repos_have_separate_cache_entries() -> None:
    """Cache key includes the path — ``foo/tags`` and ``bar/tags`` don't collide."""
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
    ) as client:
        async for _ in client.iter_tags("foo"):
            pass
        async for _ in client.iter_tags("bar"):
            pass
        async for _ in client.iter_tags("foo"):
            pass

    assert counter["/v2/foo/tags/list"] == 1
    assert counter["/v2/bar/tags/list"] == 1


async def test_get_manifest_caches_response() -> None:
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
    ) as client:
        m1 = await client.get_manifest("foo", "latest")
        m2 = await client.get_manifest("foo", "latest")

    assert m1.digest == m2.digest == "sha256:abc"
    assert counter["/v2/foo/manifests/latest"] == 1


async def test_get_manifest_no_cache_when_disabled() -> None:
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
    ) as client:
        await client.get_manifest("foo", "latest")
        await client.get_manifest("foo", "latest")
    assert counter["/v2/foo/manifests/latest"] == 2


async def test_image_config_cached_with_long_ttl() -> None:
    """Image config blobs are content-addressed; reuse aggressively."""
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
        blob_cache_ttl=3600.0,
    ) as client:
        manifest = await client.get_manifest("foo", "latest")
        c1 = await client.get_image_config("foo", manifest)
        c2 = await client.get_image_config("foo", manifest)

    assert c1.architecture == c2.architecture == "amd64"
    assert counter["/v2/foo/blobs/sha256:cfg"] == 1


async def test_cache_expiry_triggers_refetch() -> None:
    """After TTL, the next call goes back to the registry."""
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=0.05,  # 50ms
    ) as client:
        await client.get_manifest("foo", "latest")
        await asyncio.sleep(0.1)
        await client.get_manifest("foo", "latest")

    assert counter["/v2/foo/manifests/latest"] == 2


async def test_cache_unaffected_when_iteration_breaks_early() -> None:
    """Caller breaking out of the iter early still gets a complete cached list."""
    counter: dict[str, int] = {}
    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter)),
        cache_ttl=60.0,
    ) as client:
        # First call: break after 1 item — but the cache must have stored
        # the full list that ``_iter_paginated_cached`` materialized.
        async for _ in client.iter_repositories():
            break
        # Second call iterates fully and gets BOTH items from cache.
        full = [r async for r in client.iter_repositories()]

    assert full == ["alpine", "redis"]
    # First call touched the registry; second was a pure cache hit.
    assert counter["/v2/_catalog"] == 1


# -- No cross-talk between RegistryClient instances ----------------------


async def test_caches_are_per_client_instance() -> None:
    """Each RegistryClient has its own cache — session client doesn't see global."""
    counter_a: dict[str, int] = {}
    counter_b: dict[str, int] = {}

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter_a)),
        cache_ttl=60.0,
    ) as client_a:
        async for _ in client_a.iter_repositories():
            pass

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(_instrumented_handler(counter_b)),
        cache_ttl=60.0,
    ) as client_b:
        async for _ in client_b.iter_repositories():
            pass

    # Each instance hit its own registry transport once.
    assert counter_a["/v2/_catalog"] == 1
    assert counter_b["/v2/_catalog"] == 1


# -- deps.build_registry_client wires settings.cache_ttl -----------------


def test_build_registry_client_passes_cache_ttl_from_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CACHE_TTL", "120")
    monkeypatch.setenv("REGISTRY_URL", "https://registry.example.com")
    from layerloupe.config import Settings, get_settings

    get_settings.cache_clear()
    try:
        from layerloupe.deps import build_registry_client

        client = build_registry_client(Settings())
        assert client._cache is not None  # type: ignore[attr-defined]
        assert client._cache_ttl == 120.0  # type: ignore[attr-defined]
    finally:
        get_settings.cache_clear()


def test_session_client_skips_cache_even_when_setting_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-request session clients never cache (single-request lifetime)."""
    monkeypatch.setenv("CACHE_TTL", "120")
    monkeypatch.setenv("REGISTRY_URL", "https://registry.example.com")
    from layerloupe.config import Settings, get_settings

    get_settings.cache_clear()
    try:
        from layerloupe.deps import build_registry_client

        client = build_registry_client(
            Settings(),
            override_username="alice",
            override_password="x",
        )
        assert client._cache is None  # type: ignore[attr-defined]
        assert client._cache_ttl == 0.0  # type: ignore[attr-defined]
    finally:
        get_settings.cache_clear()


# -- Touch the implementation symbol so it stays exported ----------------


def test_ttlcache_class_is_re_exported() -> None:
    assert TTLCache is _TTLCacheImpl


# -- Ensure to_unified still works (import sanity) ------------------------


def test_to_unified_after_cache_layer_added() -> None:
    raw = b"{}"
    mr = ManifestResponse(
        digest="sha256:x",
        media_type=MediaType.OCI_IMAGE_MANIFEST.value,
        kind=ManifestKind.OCI_IMAGE,
        body={
            "schemaVersion": 2,
            "mediaType": MediaType.OCI_IMAGE_MANIFEST.value,
            "config": {"mediaType": "x", "digest": "sha256:c", "size": 1},
            "layers": [],
        },
        raw_body=raw,
    )
    unified = to_unified(mr)
    assert unified.type == "image"
    # Sanity: time module still imports correctly (used by cache.py).
    assert time.monotonic() > 0
