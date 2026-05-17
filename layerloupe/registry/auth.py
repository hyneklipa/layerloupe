"""Authentication helpers for the registry client.

Currently only HTTP Basic. Bearer Token Authentication (Docker Token Auth
Specification) lands in M1.3 and will share the same plug-in point on
:class:`layerloupe.registry.client.RegistryClient`.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from pydantic import SecretStr

if TYPE_CHECKING:
    from layerloupe.config import Settings


class BasicAuth:
    """HTTP Basic ``Authorization`` header builder.

    Accepts a plain ``str`` or :class:`pydantic.SecretStr` for the password,
    so it composes naturally with :class:`layerloupe.config.Settings` fields.
    A ``None`` password is treated as the empty string - matching the
    behavior of the Docker CLI when the user only supplies a username
    (e.g. token-style auth where the password slot is unused).

    The plaintext password is **not** retained on the instance; only the
    pre-computed Base64 blob is kept. ``repr`` masks the username's password
    half so the object is safe to log.
    """

    __slots__ = ("_encoded", "_username")

    def __init__(self, username: str, password: SecretStr | str | None = None) -> None:
        if not username:
            raise ValueError("BasicAuth requires a non-empty username")
        self._username = username
        pw = password.get_secret_value() if isinstance(password, SecretStr) else (password or "")
        self._encoded = base64.b64encode(f"{username}:{pw}".encode()).decode("ascii")

    @property
    def username(self) -> str:
        return self._username

    @property
    def header_value(self) -> str:
        return f"Basic {self._encoded}"

    def as_headers(self) -> dict[str, str]:
        """Headers dict suitable for ``RegistryClient(default_headers=...)``."""
        return {"Authorization": self.header_value}

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"BasicAuth(username={self._username!r}, password=***)"


def basic_auth_from_settings(settings: Settings) -> BasicAuth | None:
    """Build :class:`BasicAuth` from settings, or ``None`` when not configured.

    Use as the ``default_headers`` source for the global :class:`RegistryClient`:

        auth = basic_auth_from_settings(settings)
        client = RegistryClient(
            str(settings.registry_url),
            verify=settings.ssl_verify,
            default_headers=auth.as_headers() if auth else None,
        )
    """
    if not settings.registry_username:
        return None
    return BasicAuth(settings.registry_username, settings.registry_password)
