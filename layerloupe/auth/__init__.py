"""Authentication primitives: ``Identity`` model + ``AuthProvider`` Protocol.

The split keeps the route guards (in ``deps.py``) decoupled from how an
identity actually comes into being. ``EnvAdminProvider`` is today's
only implementation; an OIDC provider lands later as another class
that populates the same ``Identity`` dataclass and is selected by
config.

A note on terminology: "identity" is *who the user is*, "provider" is
*who attested to that*, and "roles" is *what they're authorized to do*.
The three are deliberately orthogonal so an OIDC group claim can map
straight onto roles without touching guard logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog

ADMIN_ROLE = "admin"

_logger = structlog.get_logger("layerloupe.auth")


@dataclass(frozen=True)
class Identity:
    """One logged-in (or anonymous) user as seen by route guards.

    Immutable so it can be safely cached on the request and passed
    around without defensive copies.
    """

    username: str
    roles: frozenset[str] = frozenset()
    provider: str = "anonymous"

    @property
    def is_anonymous(self) -> bool:
        return self.provider == "anonymous"

    @property
    def is_admin(self) -> bool:
        return ADMIN_ROLE in self.roles

    def to_session(self, *, auth_mode: str) -> dict[str, Any]:
        """Serialize for inclusion in the signed session cookie.

        ``frozenset`` is not JSON-serializable, so roles become a sorted
        list - sorted (not just listed) for stable cookie bytes across
        equivalent identities.

        ``auth_mode`` is the value of ``settings.auth_mode`` at the
        moment the session was minted. It travels with the cookie so
        :meth:`from_session` can invalidate stale identities once the
        operator flips the mode in env (see T7.10 in
        ``_docs/06-ui-access-control-redesign.md``).
        """
        return {
            "username": self.username,
            "roles": sorted(self.roles),
            "provider": self.provider,
            "auth_mode": auth_mode,
        }

    @classmethod
    def from_session(cls, payload: object, *, expected_auth_mode: str) -> Identity | None:
        """Reconstruct from a cookie payload, ``None`` for invalid shapes.

        Refuses anything that doesn't match the expected schema rather
        than coercing - a malformed identity is more useful upstream as
        "no identity at all" than as a half-broken value that smuggles
        e.g. ``None`` into ``username``.

        Also invalidates when ``payload["auth_mode"]`` doesn't match
        ``expected_auth_mode`` - the operator flipped ``AUTH_MODE`` in
        env between cookie issuance and this request, and the cached
        ``roles`` no longer reflect what the active provider would
        grant. We log a WARNING for that case (operators want to see
        when sessions get invalidated by config change), but stay
        silent for plain malformed payloads - those are routine after
        a ``SESSION_SECRET`` rotation or schema upgrade and would
        otherwise drown the log.
        """
        if not isinstance(payload, dict):
            return None
        username = payload.get("username")
        roles = payload.get("roles")
        provider = payload.get("provider")
        payload_auth_mode = payload.get("auth_mode")
        if not isinstance(username, str) or not isinstance(provider, str):
            return None
        if not isinstance(roles, list) or not all(isinstance(r, str) for r in roles):
            return None
        if not isinstance(payload_auth_mode, str):
            return None
        if payload_auth_mode != expected_auth_mode:
            _logger.warning(
                "session_auth_mode_mismatch",
                username=username,
                provider=provider,
                payload_auth_mode=payload_auth_mode,
                expected_auth_mode=expected_auth_mode,
            )
            return None
        return cls(
            username=username,
            roles=frozenset(roles),
            provider=provider,
        )


ANONYMOUS = Identity(username="", roles=frozenset(), provider="anonymous")
"""Singleton for "no logged-in user" - used as the fallback in route guards."""


class AuthProvider(Protocol):
    """Verifies credentials and produces an ``Identity``.

    Implementations attach themselves to login routes; the route guards
    in ``deps.py`` only ever see the resulting ``Identity`` from the
    session, never the provider directly.

    The Protocol is intentionally narrow. Anything else a specific
    provider needs (OIDC redirect callback, group sync, …) is exposed
    as routes the provider mounts, not through this interface.
    """

    name: str

    async def authenticate(self, username: str, password: str) -> Identity | None:
        """Verify the credential pair. ``None`` means "creds don't match".

        Must run in (effectively) constant time with respect to the
        username - e.g. bcrypt verification against a dummy hash when
        the username doesn't exist - so an attacker can't probe valid
        usernames via response timing.
        """
        ...


__all__ = ["ADMIN_ROLE", "ANONYMOUS", "AuthProvider", "Identity"]
