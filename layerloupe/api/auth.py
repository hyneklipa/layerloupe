"""Per-user registry login / logout.

Validates credentials against the registry by running a probe with them,
then stores ``(username, encrypted_password)`` in the signed session
cookie. On every subsequent request, :func:`layerloupe.deps.get_registry_client`
sees the session creds and constructs a fresh, per-request registry client
that uses them - so the user's personal credentials transparently replace
any env-configured ones for the duration of the session.

The password is **encrypted** with Fernet (key derived from
``settings.session_secret``) before going into the cookie. The
SessionMiddleware's signature alone protects against forgery but not
against eavesdropping on the cookie itself - Fernet closes that gap.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from layerloupe.config import Settings, SettingsDep
from layerloupe.deps import AuthProviderDep, build_registry_client
from layerloupe.sessions import encrypt_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginResponse(BaseModel):
    status: str
    username: str


class LogoutResponse(BaseModel):
    status: str


async def _verify_credentials(settings: Settings, username: str, password: str) -> bool:
    """Probe the registry with the supplied creds. ``True`` = creds work.

    Uses :func:`build_registry_client` so a test that monkeypatches it
    (e.g. to inject a ``MockTransport``) automatically affects the login
    flow as well.
    """
    client = build_registry_client(
        settings,
        override_username=username,
        override_password=password,
    )
    try:
        probe = await client.probe()
        return probe.authenticated
    finally:
        await client.aclose()


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    settings: SettingsDep,
) -> LoginResponse:
    """Validate credentials, then store ``(username, encrypted_password)`` in session."""
    if not settings.allow_registry_login:
        raise HTTPException(
            status_code=403,
            detail="Per-user registry login is disabled (set ALLOW_REGISTRY_LOGIN=true to enable).",
        )

    ok = await _verify_credentials(settings, payload.username, payload.password)
    if not ok:
        # Drop any stale session creds before reporting failure.
        request.session.pop("registry_username", None)
        request.session.pop("registry_password_enc", None)
        raise HTTPException(status_code=401, detail="Invalid registry credentials")

    request.session["registry_username"] = payload.username
    request.session["registry_password_enc"] = encrypt_password(
        settings.session_secret.get_secret_value(),
        payload.password,
    )
    return LoginResponse(status="ok", username=payload.username)


@router.post("/logout", response_model=LogoutResponse)
async def logout(request: Request) -> LogoutResponse:
    """Clear all session-stored credentials, falling back to env auth."""
    request.session.clear()
    return LogoutResponse(status="ok")


# -- UI identity login (orthogonal to registry creds above) ---------------


@router.post("/ui-login", response_model=LoginResponse)
async def ui_login(
    payload: LoginRequest,
    request: Request,
    settings: SettingsDep,
    provider: AuthProviderDep,
) -> LoginResponse:
    """JSON sibling of ``POST /web/auth/login``.

    Same login flow as the HTML form route but for machine consumers
    (CI scripts, smoke tests). Writes ``session["identity"]`` on success
    and leaves any registry creds untouched.
    """
    if settings.auth_mode == "public" or provider is None:
        raise HTTPException(
            status_code=403,
            detail="UI login is not enabled (set AUTH_MODE=protected or admin to enable).",
        )
    identity = await provider.authenticate(payload.username, payload.password)
    if identity is None:
        request.session.pop("identity", None)
        raise HTTPException(status_code=401, detail="Invalid credentials")
    request.session["identity"] = identity.to_session(auth_mode=settings.auth_mode)
    return LoginResponse(status="ok", username=identity.username)


@router.post("/ui-logout", response_model=LogoutResponse)
async def ui_logout(request: Request) -> LogoutResponse:
    """Drop the UI identity, keep registry creds in place."""
    request.session.pop("identity", None)
    return LogoutResponse(status="ok")
