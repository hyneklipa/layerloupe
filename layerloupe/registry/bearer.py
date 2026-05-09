"""Docker Token Authentication Specification — bearer flow.

Implements the auth dance described at
https://distribution.github.io/distribution/spec/auth/token/:

1. Client sends an unauthenticated request.
2. Registry responds ``401`` with
   ``Www-Authenticate: Bearer realm="...",service="...",scope="..."``.
3. Client GETs ``<realm>?service=<service>&scope=<scope>`` (optionally with
   Basic auth from a configured upstream credential) and receives a JSON
   token response.
4. Client retries the original request with ``Authorization: Bearer <token>``.

The token is cached by ``(service, scope)`` so subsequent requests for the
same scope skip steps 1-3 (pre-attached on first try, falling back to the
401 dance if the cache misses).
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
import structlog

from layerloupe.registry.exceptions import RegistryError, RegistryHTTPError

if TYPE_CHECKING:
    from layerloupe.registry.auth import BasicAuth

logger = structlog.get_logger()

_TOKEN_DEFAULT_TTL = 60.0  # seconds, when auth server omits expires_in
_TOKEN_EXPIRY_GRACE = 5.0  # treat tokens as expired this many seconds early
_BEARER_BODY_BYTES = 4096


# -- Challenge parser -----------------------------------------------------


@dataclass(frozen=True)
class BearerChallenge:
    realm: str
    service: str
    scope: str | None = None


_PARAM_RE = re.compile(r'(?P<key>\w+)=(?:"(?P<qval>[^"]*)"|(?P<val>[^,\s]+))')


def parse_bearer_challenge(www_authenticate: str | None) -> BearerChallenge | None:
    """Parse a ``Www-Authenticate: Bearer ...`` header.

    Returns ``None`` if the scheme is not Bearer or if ``realm`` / ``service``
    are missing — in either case the caller should propagate the original 401.
    """
    if not www_authenticate:
        return None
    header = www_authenticate.strip()
    if not header[:7].lower().startswith("bearer "):
        return None
    params: dict[str, str] = {}
    for match in _PARAM_RE.finditer(header[7:]):
        key = match.group("key")
        value = match.group("qval") if match.group("qval") is not None else match.group("val")
        params[key] = value
    realm = params.get("realm")
    service = params.get("service")
    if not realm or not service:
        return None
    scope = params.get("scope") or None
    return BearerChallenge(realm=realm, service=service, scope=scope)


# -- Scope inference ------------------------------------------------------


_REPO_SCOPED_RE = re.compile(r"^/v2/(?P<repo>.+?)/(manifests|blobs|tags)(/|$)")


def infer_scope(method: str, path: str) -> str | None:
    """Best-effort scope guess from the request URL.

    Used for pre-attaching cached tokens to a request before the registry
    has a chance to challenge with 401. If our guess is wrong, the 401 path
    refetches with the correct scope from the challenge — so a wrong
    inference is at most one wasted round-trip.
    """
    path = path.split("?", 1)[0]
    if path in ("/v2", "/v2/"):
        return None  # probe — registry tells us the service, no scope yet
    if path == "/v2/_catalog":
        return "registry:catalog:*"
    match = _REPO_SCOPED_RE.match(path)
    if match:
        repo = match.group("repo")
        action = "pull" if method.upper() in ("GET", "HEAD") else "*"
        return f"repository:{repo}:{action}"
    return None


# -- Token cache ----------------------------------------------------------


@dataclass(frozen=True)
class CachedToken:
    value: str
    expires_at: float

    def is_expired(self, now: float | None = None) -> bool:
        return (now if now is not None else time.time()) >= self.expires_at - _TOKEN_EXPIRY_GRACE


class TokenCache:
    """In-memory cache keyed by ``(service, scope)``."""

    def __init__(self) -> None:
        self._tokens: dict[tuple[str, str], CachedToken] = {}

    def get(self, service: str, scope: str | None) -> CachedToken | None:
        token = self._tokens.get((service, scope or ""))
        if token is None or token.is_expired():
            return None
        return token

    def put(self, service: str, scope: str | None, value: str, ttl: float) -> None:
        self._tokens[(service, scope or "")] = CachedToken(
            value=value, expires_at=time.time() + ttl
        )

    def find_any(self, scope: str | None) -> CachedToken | None:
        """Return any non-expired token matching ``scope``, ignoring service.

        Used for pre-attaching when we don't yet know the service (first
        request before a probe). If multiple match, returns one arbitrarily.
        """
        target = scope or ""
        for (_svc, scp), tok in self._tokens.items():
            if scp == target and not tok.is_expired():
                return tok
        return None

    def __len__(self) -> int:
        return len(self._tokens)


# -- httpx Auth implementation --------------------------------------------


class BearerAuth(httpx.Auth):
    """``httpx.Auth`` that drives the Docker bearer-token flow.

    Composes with :class:`layerloupe.registry.auth.BasicAuth` as the *upstream*
    credential — the user/password pair the auth server expects when minting
    a token. Without an upstream, the auth server is asked for an anonymous
    token (works for public scopes on Docker Hub-style registries).
    """

    requires_response_body = True

    def __init__(
        self,
        upstream: BasicAuth | None = None,
        *,
        verify: bool = True,
        timeout: float = 30.0,
        cache: TokenCache | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._upstream = upstream
        self._cache = cache or TokenCache()
        self._token_client = httpx.AsyncClient(
            verify=verify,
            timeout=timeout,
            transport=token_transport,
            follow_redirects=True,
        )

    @property
    def cache(self) -> TokenCache:
        return self._cache

    async def aclose(self) -> None:
        await self._token_client.aclose()

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        # 1. Pre-attach if we have a token for the inferred scope. We don't
        #    know the service from the URL alone, so scan the cache for any
        #    matching scope. If wrong (different service than expected), the
        #    registry will still 401 and we recover below.
        scope = infer_scope(request.method, request.url.path)
        cached = self._cache.find_any(scope)
        if cached is not None:
            request.headers["Authorization"] = f"Bearer {cached.value}"

        response = yield request

        if response.status_code != 401:
            return

        challenge = parse_bearer_challenge(response.headers.get("www-authenticate"))
        if challenge is None:
            # Either missing header or non-Bearer scheme (e.g. Basic-only
            # registry) — pass the 401 through to the caller.
            return

        token, ttl = await self._fetch_token(challenge)
        self._cache.put(challenge.service, challenge.scope, token, ttl=ttl)

        request.headers["Authorization"] = f"Bearer {token}"
        yield request

    async def _fetch_token(self, challenge: BearerChallenge) -> tuple[str, float]:
        params: dict[str, str] = {"service": challenge.service}
        if challenge.scope:
            params["scope"] = challenge.scope

        headers: dict[str, str] = {}
        if self._upstream is not None:
            headers.update(self._upstream.as_headers())

        try:
            response = await self._token_client.get(challenge.realm, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise RegistryError(f"Token fetch to {challenge.realm} failed: {e}") from e

        if response.status_code != 200:
            body = response.content[:_BEARER_BODY_BYTES].decode("utf-8", errors="replace")
            raise RegistryHTTPError(
                response.status_code,
                f"Token server {challenge.realm} returned {response.status_code}",
                body=body,
            )

        try:
            data = response.json()
        except ValueError as e:
            raise RegistryError(f"Token server returned non-JSON body: {e}") from e

        token = data.get("token") or data.get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryError("Token server response is missing a 'token' / 'access_token' field")
        expires_in = data.get("expires_in")
        ttl = float(expires_in) if isinstance(expires_in, (int, float)) else _TOKEN_DEFAULT_TTL
        return token, ttl
