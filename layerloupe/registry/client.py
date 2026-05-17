"""Async HTTP wrapper around a Docker Registry V2 / OCI Distribution endpoint.

Single responsibility: send requests, return parsed responses, normalize
errors. Does **not** know about authentication strategies (Basic, Bearer
token) - those live in :mod:`layerloupe.registry.auth` and are plugged in via
``default_headers`` (static creds) or, in later milestones, an auth hook
callback.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import TracebackType
from typing import Any

import httpx
import structlog

from layerloupe.registry.cache import TTLCache
from layerloupe.registry.exceptions import (
    RegistryConnectionError,
    RegistryError,
    RegistryHTTPError,
)
from layerloupe.registry.manifests import (
    MANIFEST_ACCEPT_HEADER,
    ManifestKind,
    ManifestResponse,
    classify_media_type,
)
from layerloupe.registry.models import ImageConfig
from layerloupe.registry.referrers import Referrer, parse_referrers

logger = structlog.get_logger()


_DEFAULT_TIMEOUT = 30.0
_MAX_ERROR_BODY_BYTES = 4096
_DEFAULT_PAGE_SIZE = 100

_LINK_RE = re.compile(r"<([^>]+)>\s*;\s*([^,]+)")


def parse_next_link(link_header: str | None) -> str | None:
    """Extract the URL with ``rel="next"`` from an RFC 5988 ``Link`` header.

    Accepts either an absolute URL or a relative path; returns it verbatim
    so it can be passed straight back to httpx (which honors both against
    a base URL).
    """
    if not link_header:
        return None
    for match in _LINK_RE.finditer(link_header):
        url = match.group(1).strip()
        params = match.group(2).lower()
        if 'rel="next"' in params or "rel=next" in params:
            return url
    return None


def _matches_filter(item: str, query: str | None) -> bool:
    """Case-insensitive substring match. ``None`` query matches everything."""
    if query is None or query == "":
        return True
    return query.casefold() in item.casefold()


_DIGEST_RE = re.compile(r"^[a-z0-9][a-z0-9_+.-]*:[a-fA-F0-9]+$")


def _looks_like_digest(reference: str) -> bool:
    """Return ``True`` if ``reference`` parses as ``<algorithm>:<hex>``.

    Docker tags can't contain ``:`` so this distinguishes the two namespaces
    cleanly. We require hex after the colon to avoid false positives on
    pathological tag names that somehow snuck a colon in.
    """
    return bool(_DIGEST_RE.match(reference))


@dataclass(frozen=True)
class RegistryProbe:
    """Result of :meth:`RegistryClient.probe` - registry liveness + auth state.

    ``reachable`` is the binary "did we get any HTTP response" bit, useful
    for distinguishing network failures from auth failures. ``authenticated``
    is the operational "is this registry usable right now" bit and is what
    the readiness endpoint cares about.
    """

    reachable: bool
    authenticated: bool = False
    status_code: int | None = None
    version: str | None = None
    auth_scheme: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "reachable": self.reachable,
            "authenticated": self.authenticated,
            "status_code": self.status_code,
            "version": self.version,
            "auth_scheme": self.auth_scheme,
            "error": self.error,
        }


class RegistryClient:
    """Thin async wrapper around ``httpx.AsyncClient`` for a single registry.

    Args:
        base_url: e.g. ``"https://registry.example.com:5000"`` - the registry
            root, without the ``/v2/`` prefix (callers pass paths starting
            with ``/v2/...``).
        verify: TLS certificate verification. Pass ``False`` for self-signed
            development registries.
        timeout: Per-request timeout in seconds.
        default_headers: Headers attached to every outgoing request (this is
            where static auth headers go in M1.2; swap out for a hook in M1.3).
        transport: Override the underlying transport. Primarily for tests
            (``httpx.MockTransport``); leave ``None`` in production.
    """

    def __init__(
        self,
        base_url: str,
        *,
        verify: bool = True,
        timeout: float = _DEFAULT_TIMEOUT,
        default_headers: dict[str, str] | None = None,
        auth: httpx.Auth | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        cache_ttl: float = 0.0,
        blob_cache_ttl: float = 3600.0,
    ) -> None:
        self._base_url = base_url
        self._verify = verify
        self._timeout = timeout
        self._auth = auth
        self._cache_ttl = cache_ttl
        self._blob_cache_ttl = blob_cache_ttl
        # Cache is allocated only when ``cache_ttl > 0`` so the per-request
        # session client (cache_ttl=0) carries no useless overhead.
        self._cache: TTLCache | None = TTLCache() if cache_ttl > 0 else None
        self._client = httpx.AsyncClient(
            base_url=base_url,
            verify=verify,
            timeout=timeout,
            transport=transport,
            headers=default_headers or {},
            auth=auth,
            follow_redirects=True,
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def verify(self) -> bool:
        return self._verify

    @property
    def timeout(self) -> float:
        return self._timeout

    async def __aenter__(self) -> RegistryClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()
        # If the auth provider owns long-lived state (e.g. a token-fetch
        # client), let it tear it down too.
        aclose = getattr(self._auth, "aclose", None)
        if aclose is not None:
            await aclose()

    # -- Public request methods -------------------------------------------

    async def get_json(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """GET ``path`` and return parsed JSON. Raises on non-2xx."""
        response = await self._request("GET", path, headers=headers, params=params)
        try:
            data = response.json()
        except ValueError as e:
            raise RegistryHTTPError(
                response.status_code,
                f"Registry returned non-JSON for {path}: {e}",
                body=_safe_body(response),
            ) from e
        if not isinstance(data, dict):
            raise RegistryHTTPError(
                response.status_code,
                f"Registry returned non-object JSON for {path} (got {type(data).__name__})",
                body=_safe_body(response),
            )
        return data

    async def get(
        self,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """GET ``path`` and return the raw ``httpx.Response`` (for blob fetches)."""
        return await self._request("GET", path, headers=headers, params=params)

    async def head(self, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        """HEAD ``path`` and return the raw response.

        Used for fetching the ``Docker-Content-Digest`` header without
        downloading the manifest body.
        """
        return await self._request("HEAD", path, headers=headers)

    async def delete(self, path: str, *, headers: dict[str, str] | None = None) -> httpx.Response:
        """DELETE ``path`` and return the raw response."""
        return await self._request("DELETE", path, headers=headers)

    async def iter_repositories(
        self,
        query: str | None = None,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream repository names from ``/v2/_catalog`` across all pages.

        Args:
            query: Optional case-insensitive substring filter. ``None`` /
                empty string matches everything.
            page_size: ``?n=`` value sent to the registry. Servers may cap
                this; honor what they return.
            max_pages: Safety cap on iterations. ``None`` means follow until
                the server stops sending ``Link: rel="next"`` (or empty page).

        The filter runs client-side, after each page is fetched - registries
        don't support server-side filtering on this endpoint.
        """
        async for item in self._iter_paginated_cached(
            "/v2/_catalog", "repositories", page_size=page_size, max_pages=max_pages
        ):
            if _matches_filter(item, query):
                yield item

    async def iter_tags(
        self,
        repository: str,
        query: str | None = None,
        *,
        page_size: int = _DEFAULT_PAGE_SIZE,
        max_pages: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream tag names for ``repository`` across all pages.

        Same semantics as :meth:`iter_repositories` but hits
        ``/v2/<repo>/tags/list``.
        """
        # Some registries return `{"tags": null}` for empty repos - handled by
        # _iter_paginated's `data.get(items_key) or []` fallback.
        path = f"/v2/{repository}/tags/list"
        async for tag in self._iter_paginated_cached(
            path, "tags", page_size=page_size, max_pages=max_pages
        ):
            if _matches_filter(tag, query):
                yield tag

    async def get_blob(self, repository: str, digest: str) -> httpx.Response:
        """GET a blob by digest at ``/v2/<repo>/blobs/<digest>``.

        Returns the raw :class:`httpx.Response` so callers can decide how to
        consume the body - parse JSON (image config), stream to disk (large
        layer tarballs), etc. The configured auth flow runs as usual.
        """
        return await self.get(f"/v2/{repository}/blobs/{digest}")

    async def get_image_config(self, repository: str, manifest: ManifestResponse) -> ImageConfig:
        """Resolve and parse the image config blob referenced by a manifest.

        Only meaningful for **single-arch image** manifests (OCI image,
        Docker v2). Index/list manifests carry no config of their own -
        callers must pick a per-platform child manifest first. Schema 1
        manifests embed the config as ``v1Compatibility`` strings inside
        the manifest body and are handled separately in M2.3.
        """
        if manifest.is_index:
            raise RegistryError(
                "Cannot fetch image config from an index/manifest list - "
                "select a per-platform child manifest first"
            )
        if manifest.kind is ManifestKind.DOCKER_V1:
            raise RegistryError(
                "Schema 1 manifests embed config as v1Compatibility strings; "
                "use the schema 1 parser (M2.3) instead of get_image_config"
            )
        config_section = manifest.body.get("config")
        if not isinstance(config_section, dict):
            raise RegistryError(
                f"Manifest body has no 'config' object (got {type(config_section).__name__})"
            )
        digest = config_section.get("digest")
        if not isinstance(digest, str) or not digest:
            raise RegistryError("Manifest's 'config' section is missing a digest")

        # Image config blobs are content-addressed by digest - they are
        # immutable, so cache them aggressively (``blob_cache_ttl`` ≫ ``cache_ttl``).
        cache_key = ("image_config", repository, digest)
        if self._cache is not None:
            hit, cached = self._cache.get(cache_key)
            if hit:
                return cached  # type: ignore[no-any-return]

        response = await self.get_blob(repository, digest)
        try:
            data = response.json()
        except ValueError as e:
            raise RegistryHTTPError(
                response.status_code,
                f"Image config blob {digest} is not valid JSON: {e}",
                body=_safe_body(response),
            ) from e
        if not isinstance(data, dict):
            raise RegistryHTTPError(
                response.status_code,
                f"Image config blob {digest} is not a JSON object",
                body=_safe_body(response),
            )
        config = ImageConfig.model_validate(data)
        if self._cache is not None:
            self._cache.set(cache_key, config, self._blob_cache_ttl)
        return config

    async def get_referrers(self, repository: str, digest: str) -> list[Referrer]:
        """Fetch ``/v2/<repo>/referrers/<digest>`` and parse the response.

        Returns an **empty list** when the registry doesn't implement the
        OCI 1.1 referrers API (HTTP ``404`` / ``405`` / ``501``) - those
        statuses are spec-permitted ways to signal "not supported", so we
        treat them as a soft no-op rather than an error. Other HTTP errors
        propagate as :class:`RegistryHTTPError`.

        ``digest`` must be a content-addressable digest reference; the
        referrers endpoint doesn't accept tags per spec.
        """
        try:
            body = await self.get_json(f"/v2/{repository}/referrers/{digest}")
        except RegistryHTTPError as e:
            if e.status_code in (404, 405, 501):
                return []
            raise
        return parse_referrers(body)

    async def delete_manifest(self, repository: str, reference: str) -> str:
        """Delete a manifest. Returns the digest that was actually deleted.

        Per the Distribution Spec, ``DELETE /v2/<repo>/manifests/<tag>`` is
        deprecated and most registries reject it. The reliable path is:

        1. ``HEAD /v2/<repo>/manifests/<tag>`` with the full manifest
           ``Accept`` set, read ``Docker-Content-Digest``.
        2. ``DELETE /v2/<repo>/manifests/<digest>``.

        When ``reference`` is already a digest (``sha256:...``) we skip the
        HEAD round-trip and DELETE straight away.

        Note: deleting only unlinks the manifest. Layer blobs persist until
        the registry's garbage collector runs (``registry garbage-collect``)
        - UI should warn the operator about this.
        """
        digest = (
            reference
            if _looks_like_digest(reference)
            else await self._resolve_digest(repository, reference)
        )
        await self.delete(f"/v2/{repository}/manifests/{digest}")
        return digest

    async def _resolve_digest(self, repository: str, tag: str) -> str:
        head_response = await self.head(
            f"/v2/{repository}/manifests/{tag}",
            headers={"Accept": MANIFEST_ACCEPT_HEADER},
        )
        digest: str | None = head_response.headers.get("docker-content-digest")
        if not digest:
            raise RegistryError(
                f"Registry did not return Docker-Content-Digest for "
                f"{repository}:{tag} - cannot delete safely without a digest"
            )
        return digest

    async def get_manifest(self, repository: str, reference: str) -> ManifestResponse:
        """Fetch a manifest at ``/v2/<repo>/manifests/<reference>``.

        ``reference`` is a tag (``"latest"``, ``"22.04"``) or a digest
        (``"sha256:abc..."``). The full set of manifest media types is sent
        in ``Accept`` so the registry returns whatever native format the
        manifest was pushed in - schema 1, schema 2, OCI image, or OCI
        index. The response's ``Content-Type`` is what tells us which.

        Raises :class:`RegistryHTTPError` on non-2xx (including 404 for
        unknown tags). Successful responses always carry a parsed JSON body
        and the raw bytes; ``digest`` is ``None`` only when the registry
        omitted ``Docker-Content-Digest`` (rare).
        """
        # Tag-pinned manifests can mutate (a tag pointing at a different
        # digest after a push); digest-pinned ones are immutable. We cache
        # both at ``cache_ttl`` because tag→digest churn during a single
        # operator session is rare; if it bites, the operator can reload.
        cache_key = ("manifest", repository, reference)
        if self._cache is not None:
            hit, cached = self._cache.get(cache_key)
            if hit:
                return cached  # type: ignore[no-any-return]

        path = f"/v2/{repository}/manifests/{reference}"
        response = await self._request("GET", path, headers={"Accept": MANIFEST_ACCEPT_HEADER})
        content_type = response.headers.get("content-type", "")
        kind = classify_media_type(content_type)
        try:
            body = response.json()
        except ValueError as e:
            raise RegistryHTTPError(
                response.status_code,
                f"Manifest at {path} is not valid JSON: {e}",
                body=_safe_body(response),
            ) from e
        if not isinstance(body, dict):
            raise RegistryHTTPError(
                response.status_code,
                f"Manifest at {path} is not a JSON object",
                body=_safe_body(response),
            )

        digest = response.headers.get("docker-content-digest")
        if digest is None:
            logger.warning("manifest_missing_digest_header", path=path)

        manifest = ManifestResponse(
            digest=digest,
            media_type=content_type.split(";", 1)[0].strip(),
            kind=kind,
            body=body,
            raw_body=response.content,
        )
        if self._cache is not None:
            self._cache.set(cache_key, manifest, self._cache_ttl)
        return manifest

    async def probe(self) -> RegistryProbe:
        """Hit ``GET /v2/`` and report registry liveness + auth state.

        Goes through the configured auth flow, so a successful probe means
        "registry reachable and we can authenticate against it" - exactly
        what the readiness endpoint wants. Bypasses :meth:`_request`'s
        4xx-raising so we can introspect the ``Www-Authenticate`` challenge.
        """
        try:
            response = await self._client.get("/v2/")
        except (httpx.HTTPError, RegistryError) as e:
            return RegistryProbe(reachable=False, error=str(e))

        version = response.headers.get("docker-distribution-api-version")
        if response.status_code == 200:
            return RegistryProbe(
                reachable=True, authenticated=True, status_code=200, version=version
            )

        auth_header = response.headers.get("www-authenticate", "")
        scheme = auth_header.split(" ", 1)[0] if auth_header else None
        return RegistryProbe(
            reachable=True,
            authenticated=False,
            status_code=response.status_code,
            version=version,
            auth_scheme=scheme,
        )

    # -- Internals --------------------------------------------------------

    async def _iter_paginated_cached(
        self,
        initial_path: str,
        items_key: str,
        *,
        page_size: int,
        max_pages: int | None,
    ) -> AsyncIterator[str]:
        """Cache-aware wrapper over :meth:`_iter_paginated`.

        Materializes the full unfiltered list before yielding so a caller
        that ``break``s out early doesn't poison the cache with a partial
        list. The eventual filter (``query`` argument on the public
        ``iter_*`` methods) is applied *after* the cache lookup; that lets
        rapid filter typing replay the same registry-level fetch.
        """
        if self._cache is None:
            async for item in self._iter_paginated(
                initial_path, items_key, page_size=page_size, max_pages=max_pages
            ):
                yield item
            return

        cache_key = ("paginated", initial_path, items_key, page_size, max_pages)
        hit, cached = self._cache.get(cache_key)
        if hit:
            for item in cached:
                yield item
            return

        items: list[str] = []
        async for item in self._iter_paginated(
            initial_path, items_key, page_size=page_size, max_pages=max_pages
        ):
            items.append(item)
        self._cache.set(cache_key, items, self._cache_ttl)
        for item in items:
            yield item

    async def _iter_paginated(
        self,
        initial_path: str,
        items_key: str,
        *,
        page_size: int,
        max_pages: int | None,
    ) -> AsyncIterator[str]:
        """Drive pagination over a ``Link``-header-using V2 endpoint.

        Three-tier next-page strategy, in priority order:

        1. ``Link: rel="next"`` header (RFC 5988, the spec's recommendation).
        2. If absent and the page was full, retry with ``?last=<last item>``.
        3. If the page was short or empty, stop.

        ``max_pages`` is a guard against pathological registries that don't
        send ``Link`` and always return full pages - without it the loop
        could run forever.
        """
        path: str | None = initial_path
        params: dict[str, Any] | None = {"n": page_size}
        seen_pages = 0

        while path is not None:
            if max_pages is not None and seen_pages >= max_pages:
                logger.warning(
                    "pagination_max_pages_reached",
                    path=initial_path,
                    max_pages=max_pages,
                )
                return

            response = await self._request("GET", path, params=params)
            seen_pages += 1
            try:
                data = response.json()
            except ValueError as e:
                raise RegistryHTTPError(
                    response.status_code,
                    f"Registry returned non-JSON for {path}: {e}",
                    body=_safe_body(response),
                ) from e
            if not isinstance(data, dict):
                raise RegistryHTTPError(
                    response.status_code,
                    f"Registry returned non-object JSON for {path}",
                    body=_safe_body(response),
                )

            items = data.get(items_key) or []
            if not isinstance(items, list):
                raise RegistryHTTPError(
                    response.status_code,
                    f"Expected '{items_key}' to be a list in response from {path}",
                    body=_safe_body(response),
                )

            # Stagnation guard, evaluated *before* yielding: if we sent
            # ?last=X and the registry replayed a page that still ends with
            # X, the cursor isn't advancing - skip this page entirely so
            # callers don't see duplicates, and stop instead of looping.
            sent_cursor = params.get("last") if params is not None else None
            if (
                sent_cursor is not None
                and items
                and isinstance(items[-1], str)
                and items[-1] == sent_cursor
            ):
                logger.warning(
                    "pagination_cursor_stagnant",
                    path=initial_path,
                    last_item=sent_cursor,
                    page_size=page_size,
                )
                return

            for item in items:
                if isinstance(item, str):
                    yield item

            next_link = parse_next_link(response.headers.get("link"))
            if next_link is not None:
                path = next_link
                params = None  # cursor lives in the URL now
                continue
            if len(items) < page_size:
                return  # short page → done
            # Server didn't send Link but page was full - fall back to
            # explicit cursor with the last item we got.
            last_item = items[-1]
            if not isinstance(last_item, str):
                return
            path = initial_path
            params = {"n": page_size, "last": last_item}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._client.request(method, path, headers=headers, params=params)
        except httpx.TimeoutException as e:
            raise RegistryConnectionError(f"Timeout calling {method} {path}: {e}") from e
        except httpx.HTTPError as e:
            raise RegistryConnectionError(f"Connection error calling {method} {path}: {e}") from e

        if response.status_code >= 400:
            raise RegistryHTTPError(
                response.status_code,
                f"{method} {path} returned {response.status_code}",
                body=_safe_body(response),
            )
        return response


def _safe_body(response: httpx.Response) -> str:
    """Return up to a few KB of the response body, decoded best-effort."""
    raw = response.content[:_MAX_ERROR_BODY_BYTES]
    return raw.decode("utf-8", errors="replace")
