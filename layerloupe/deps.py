"""FastAPI dependencies — registry client wiring + per-user session creds.

Two clients live side-by-side at runtime:

* A long-lived **global** :class:`RegistryClient`, built at app startup
  (lifespan) with the env-configured credentials. Lives on
  ``app.state.registry_client`` and is shared across all anonymous /
  un-logged-in requests.
* A per-request **session** client, freshly built when the request's
  session has decryptable :func:`override creds <get_registry_client>`.
  This client is closed via the dependency's ``finally`` block as soon as
  the response is returned.

The session client wins over the global one for the duration of any single
authenticated request — so once the user logs in via ``/api/auth/login``,
every subsequent registry call goes through their personal credentials.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import httpx
from fastapi import Depends, Request

from layerloupe.config import Settings, get_settings
from layerloupe.registry import (
    BasicAuth,
    BearerAuth,
    RegistryClient,
    basic_auth_from_settings,
)
from layerloupe.sessions import decrypt_password


def build_registry_client(
    settings: Settings,
    *,
    override_username: str | None = None,
    override_password: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> RegistryClient:
    """Construct a :class:`RegistryClient` matching the current settings.

    When ``override_username`` is provided, those credentials replace any
    env-configured ones — used for the per-user session client and for
    :func:`layerloupe.api.auth._verify_credentials` during login probes.
    These per-user clients live for the duration of one request, so they
    skip caching (``cache_ttl=0``); only the long-lived global client
    actually benefits from caching.

    ``transport`` is for tests (``httpx.MockTransport``); production code
    leaves it ``None``.
    """
    if override_username is not None:
        basic: BasicAuth | None = BasicAuth(override_username, override_password)
        cache_ttl = 0.0  # per-request session client — caching is pointless
    else:
        basic = basic_auth_from_settings(settings)
        cache_ttl = float(settings.cache_ttl)
    bearer = BearerAuth(upstream=basic, verify=settings.ssl_verify, timeout=30.0)
    default_headers = basic.as_headers() if basic else None
    return RegistryClient(
        str(settings.registry_url).rstrip("/"),
        verify=settings.ssl_verify,
        timeout=30.0,
        default_headers=default_headers,
        auth=bearer,
        transport=transport,
        cache_ttl=cache_ttl,
    )


def _session_credentials(request: Request, settings: Settings) -> tuple[str, str] | None:
    """Pull and decrypt registry credentials from the session cookie.

    Returns ``None`` for any of the cases that should silently fall back to
    env auth: missing session, missing fields, wrong types, undecryptable
    ciphertext (rotated secret / tampered cookie).
    """
    if not hasattr(request, "session"):
        return None
    username = request.session.get("registry_username")
    encrypted = request.session.get("registry_password_enc")
    if not isinstance(username, str) or not isinstance(encrypted, str):
        return None
    password = decrypt_password(settings.session_secret.get_secret_value(), encrypted)
    if password is None:
        return None
    return username, password


async def get_registry_client(request: Request) -> AsyncIterator[RegistryClient]:
    """Yield the right :class:`RegistryClient` for this request.

    Session creds (if any) take precedence; their client is freshly built
    and closed when the request finishes. Otherwise the global client from
    :data:`app.state.registry_client` is yielded — that one's lifetime is
    managed by the lifespan and we don't close it here.
    """
    settings = get_settings()
    creds = _session_credentials(request, settings)
    if creds is not None:
        username, password = creds
        client = build_registry_client(
            settings,
            override_username=username,
            override_password=password,
        )
        try:
            yield client
        finally:
            await client.aclose()
        return

    fallback: RegistryClient | None = getattr(request.app.state, "registry_client", None)
    if fallback is None:  # pragma: no cover - misconfiguration safety net
        raise RuntimeError("registry_client is not initialized on app.state")
    yield fallback


RegistryClientDep = Annotated[RegistryClient, Depends(get_registry_client)]
"""Dependency annotation for endpoints needing the registry client."""
