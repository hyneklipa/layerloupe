"""Single-admin auth provider configured via environment variables.

The "env" provider is the MVP path: one admin user defined by
``ADMIN_USERNAME`` + ``ADMIN_PASSWORD_HASH`` (or their ``_FILE``
counterparts resolved through ``layerloupe.config_secrets``).
Multi-user is intentionally not supported here; teams that need that
plug in OIDC.

In-memory the password is always a bcrypt hash, regardless of input
shape - so the verification path is uniform and the rest of the app
never sees plaintext.
"""

from __future__ import annotations

import hmac

import bcrypt

from layerloupe.auth import ADMIN_ROLE, Identity

_DEFAULT_GRANTED_ROLES = frozenset({ADMIN_ROLE})

# Precomputed bcrypt hash of a long random byte string - used as the
# decoy for timing-safe verification when the supplied username
# doesn't match. Generated once at import time so the (expensive) salt
# generation runs at startup, not during a login request.
_DUMMY_HASH = bcrypt.hashpw(b"timing-decoy-not-a-real-password", bcrypt.gensalt(rounds=12))


class EnvAdminProvider:
    """Verifies credentials against a single env-configured admin.

    Construction takes the resolved bcrypt hash directly - the caller
    (``Settings`` validators) is responsible for hashing any plaintext
    that came in via ``ADMIN_PASSWORD_FILE``. Keeping that conversion
    out of this class means the provider has one job: verify.
    """

    name = "env"

    def __init__(
        self,
        *,
        username: str,
        password_hash: str,
        granted_roles: frozenset[str] = _DEFAULT_GRANTED_ROLES,
    ) -> None:
        if not username:
            raise ValueError("EnvAdminProvider requires a non-empty username")
        if not password_hash:
            raise ValueError("EnvAdminProvider requires a non-empty password hash")
        self._username = username
        # bcrypt operates on bytes. We accept ``$2b$...`` hashes only;
        # any other shape simply won't match in ``bcrypt.checkpw`` and
        # the user gets a generic "invalid credentials" response.
        self._password_hash = password_hash.encode("utf-8")
        # The successful-login Identity carries these roles. Caller
        # decides what's granted: ``admin`` mode → ``{"admin"}``,
        # ``protected`` mode → empty (logged in but no destructive
        # capability). Defaulting to ``{"admin"}`` keeps the
        # standalone-provider use case (CLI / scripts) ergonomic.
        self._granted_roles = granted_roles

    async def authenticate(self, username: str, password: str) -> Identity | None:
        # ``hmac.compare_digest`` is the standard constant-time compare
        # for same-length inputs. Different lengths leak length (an
        # acceptable tradeoff - bcrypt verification dominates total
        # request time by orders of magnitude).
        username_matches = hmac.compare_digest(
            username.encode("utf-8"),
            self._username.encode("utf-8"),
        )
        # Always run bcrypt - against the real hash on a username match,
        # against the dummy hash otherwise - so request duration doesn't
        # tell an attacker whether ``username`` is the admin's.
        target_hash = self._password_hash if username_matches else _DUMMY_HASH
        try:
            password_matches = bcrypt.checkpw(password.encode("utf-8"), target_hash)
        except ValueError:
            # ``bcrypt.checkpw`` raises ``ValueError("Invalid salt")`` if
            # the stored hash isn't in the bcrypt ``$2b$...`` format. We
            # treat that as "verification failed" - operator gave us a
            # malformed hash, login fails closed, app keeps running.
            password_matches = False
        if username_matches and password_matches:
            return Identity(
                username=self._username,
                roles=self._granted_roles,
                provider=self.name,
            )
        return None


def hash_password(plaintext: str, *, rounds: int = 12) -> str:
    """Bcrypt-hash a plaintext password.

    Used at startup to normalize a ``ADMIN_PASSWORD_FILE`` value into
    the same on-the-wire shape ``ADMIN_PASSWORD_HASH`` accepts, and
    available as a public API for ``scripts/hash-password.py`` (T7.8)
    so the helper doesn't reach into a private module.
    """
    return bcrypt.hashpw(plaintext.encode("utf-8"), bcrypt.gensalt(rounds=rounds)).decode("ascii")


__all__ = ["EnvAdminProvider", "hash_password"]
