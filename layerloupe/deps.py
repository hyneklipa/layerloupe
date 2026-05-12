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
from fastapi import Depends, HTTPException, Request

from layerloupe.auth import ANONYMOUS, Identity
from layerloupe.config import Settings, SettingsDep, get_settings
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


# -- Identity / access control -------------------------------------------
#
# These dependencies decouple route handlers from how an identity comes
# into being. The handler asks for "current identity" or "an admin
# identity"; whether that came from an env-configured admin or (later)
# from OIDC is invisible to the handler.


def get_identity(request: Request) -> Identity:
    """Return the current request's identity. ``ANONYMOUS`` when not logged in.

    Reads the signed session cookie via :meth:`Identity.from_session`.
    Any malformed payload (cookie tampering, schema drift after an
    upgrade, rotated ``SESSION_SECRET``) silently falls back to
    ``ANONYMOUS`` — the route guards then decide whether that's
    acceptable for the route being hit.
    """
    if not hasattr(request, "session"):
        return ANONYMOUS
    payload = request.session.get("identity")
    identity = Identity.from_session(payload)
    return identity if identity is not None else ANONYMOUS


IdentityDep = Annotated[Identity, Depends(get_identity)]
"""Dependency annotation for endpoints that want to read the identity."""


def require_browse_access(
    identity: IdentityDep,
    settings: SettingsDep,
) -> Identity:
    """Allow the request through when the active ``AUTH_MODE`` permits browse.

    * ``public``: anonymous OK, returns ``identity`` unchanged.
    * ``protected`` / ``admin``: anonymous → ``401 Unauthorized``. The
      global HTML exception handler converts that to a login redirect
      for browser routes (T7.6); JSON API consumers see ``401`` + a
      JSON ``detail``.
    """
    if settings.auth_mode == "public":
        return identity
    if identity.is_anonymous:
        raise HTTPException(status_code=401, detail="Authentication required")
    return identity


def require_admin(identity: IdentityDep) -> Identity:
    """Require an identity with the ``admin`` role.

    * Anonymous → ``401`` (the user might just need to log in).
    * Authenticated but missing ``admin`` → ``403`` (defense in depth;
      in the single-admin model this branch can't fire from a normal
      flow, but a stale session against a flipped ``AUTH_MODE`` could
      hit it).
    """
    if identity.is_anonymous:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not identity.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    return identity


BrowseAccessDep = Annotated[Identity, Depends(require_browse_access)]
"""Inject on routes that need browse access (auth-mode-aware)."""

AdminDep = Annotated[Identity, Depends(require_admin)]
"""Inject on routes that mutate state — currently only manifest delete."""
