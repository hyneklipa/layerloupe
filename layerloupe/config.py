"""Application settings (env-driven via pydantic-settings)."""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import AnyHttpUrl, Field, PrivateAttr, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from layerloupe.auth.env_provider import hash_password
from layerloupe.config_secrets import resolve_secret

logger = logging.getLogger(__name__)

AuthMode = Literal["public", "protected", "admin"]
"""LayerLoupe UI access modes.

``public`` — anonymous read-only browse, no delete.
``protected`` — login required, still no delete.
``admin`` — login required, delete granted to admin role.

See ``_docs/06-ui-access-control-redesign.md`` for the full design.
"""


class Settings(BaseSettings):
    """LayerLoupe runtime configuration.

    Values are loaded from environment variables (no prefix — just
    ``REGISTRY_URL``, ``AUTH_MODE``, …) and optionally from a ``.env``
    file in CWD.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- Registry connection ----------------------------------------------
    registry_url: AnyHttpUrl = Field(
        default=AnyHttpUrl("https://localhost:5000"),
        description="Full URL of the Docker / OCI registry to browse.",
    )
    registry_public_url: str | None = Field(
        default=None,
        description=(
            "Public host (optionally with protocol) shown in `docker pull` "
            "commands. ``docker pull`` doesn't take a scheme so a bare "
            "``registry.example.com`` or ``registry.example.com:5000`` is "
            "fine. Defaults to ``registry_url`` when not set."
        ),
    )
    ssl_verify: bool = Field(default=True, description="Verify TLS certificates of the registry.")

    # -- Registry authentication ------------------------------------------
    registry_username: str | None = Field(
        default=None, description="Global registry user (overridden by per-user session login)."
    )
    registry_password: SecretStr | None = Field(
        default=None, description="Global registry password."
    )
    allow_registry_login: bool = Field(
        default=False, description="Expose UI login form for per-user registry credentials."
    )

    # -- UI access control ------------------------------------------------
    auth_mode: AuthMode = Field(
        default="public",
        description=(
            "UI access mode. ``public`` (default): anonymous browse, no delete. "
            "``protected``: login required, no delete. "
            "``admin``: login required, delete granted to admin role."
        ),
    )
    admin_username: str | None = Field(
        default=None,
        description="Admin login name (required when ``AUTH_MODE != public``).",
    )
    admin_username_file: Path | None = Field(
        default=None,
        description=(
            "Path to a plaintext file containing the admin username "
            "(Docker / Kubernetes secrets idiom)."
        ),
    )
    admin_password_hash: SecretStr | None = Field(
        default=None,
        description=(
            "Bcrypt hash of the admin password (``$2b$...``). Generate with "
            "``uv run scripts/hash-password.py``. Required when ``AUTH_MODE != public`` "
            "unless ``ADMIN_PASSWORD_FILE`` is set."
        ),
    )
    admin_password_file: Path | None = Field(
        default=None,
        description=(
            "Path to a *plaintext* file containing the admin password. "
            "Hashed at startup via bcrypt — the in-memory representation "
            "is always a hash. Use this for Docker / K8s secrets where the "
            "file mount is the trust boundary."
        ),
    )

    # -- Branding & sessions ----------------------------------------------
    title: str = Field(default="LayerLoupe", description="Branding title shown in the UI.")
    # Sentinel default (empty SecretStr) → resolved by ``_resolve_session_secret``
    # to either the env value, the file value, or a freshly auto-generated one.
    # Outside callers always see a non-empty SecretStr after ``Settings()``.
    session_secret: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "Secret used to sign session cookies. Auto-generated if not set "
            "(emits a startup warning — set it explicitly in production). "
            "Can also be loaded from a file via ``SESSION_SECRET_FILE``."
        ),
    )
    session_secret_file: Path | None = Field(
        default=None,
        description=(
            "Path to a file containing the session secret. Plaintext (the file "
            "*is* the secret storage — Docker secrets / K8s secrets idiom). "
            "When both ``SESSION_SECRET`` and ``SESSION_SECRET_FILE`` are set, "
            "the file value wins and a warning is logged."
        ),
    )

    # -- Logging ----------------------------------------------------------
    log_level: Literal["debug", "info", "warning", "error"] = Field(
        default="info", description="Logging level."
    )
    log_json: bool = Field(default=False, description="Emit structured JSON logs.")

    # -- Performance ------------------------------------------------------
    cache_ttl: int = Field(
        default=30,
        ge=0,
        description="TTL (seconds) for cached registry responses. 0 disables caching.",
    )
    page_size: int = Field(
        default=100,
        ge=1,
        le=1000,
        description="Page size sent as `?n=` to `_catalog` and `tags/list`.",
    )

    # -- Audit ------------------------------------------------------------
    audit_log_path: Path | None = Field(
        default=None,
        description=(
            "Optional path to a JSONL audit file. When set, every successful "
            "manifest delete appends a line containing actor, repo, digest, "
            "and timestamp. The same event is always emitted to the structured "
            "stdout log regardless."
        ),
    )

    # Internal marker — set by ``_resolve_session_secret`` so
    # ``get_settings`` knows whether to emit the "auto-generated" warning
    # without having to re-inspect the environment.
    _session_secret_autogenerated: bool = PrivateAttr(default=False)

    @model_validator(mode="after")
    def _resolve_session_secret(self) -> Settings:
        """Merge ``SESSION_SECRET`` + ``SESSION_SECRET_FILE`` into one value.

        Auto-generates a random secret only as a last resort — that
        branch is what triggers the production-warning log at startup.
        """
        # Empty SecretStr == "not supplied" (the sentinel default).
        raw_value = self.session_secret.get_secret_value() or None
        raw_file = str(self.session_secret_file) if self.session_secret_file else None
        resolved = resolve_secret(raw_value, raw_file, name="SESSION_SECRET")
        if resolved is None:
            resolved = secrets.token_urlsafe(32)
            self._session_secret_autogenerated = True
        self.session_secret = SecretStr(resolved)
        return self

    @model_validator(mode="after")
    def _default_public_url(self) -> Settings:
        if self.registry_public_url is None:
            # Mirror the registry URL as a plain string — the public field
            # is no longer scheme-validated, so just stringify the AnyHttpUrl.
            self.registry_public_url = str(self.registry_url)
        return self

    @model_validator(mode="after")
    def _validate_auth_config(self) -> Settings:
        """Resolve admin creds across env + file sources, enforce mode invariants.

        After this validator the model holds at most one effective
        ``admin_username`` (resolved from either ``ADMIN_USERNAME`` or
        ``ADMIN_USERNAME_FILE``) and one ``admin_password_hash`` (either
        the supplied bcrypt hash or a freshly-hashed plaintext from
        ``ADMIN_PASSWORD_FILE``). The ``*_FILE`` fields stay populated as
        a trail for diagnostics but the rest of the app reads only the
        non-``_FILE`` versions.
        """
        import os

        # Plaintext ``ADMIN_PASSWORD`` in env is a security antipattern
        # (visible in ``docker inspect`` / ``ps auxe``). We never accept
        # it — and we say so explicitly so the operator picks the right
        # alternative instead of wondering why their password didn't take.
        if os.environ.get("ADMIN_PASSWORD") and not (
            self.admin_password_hash or self.admin_password_file
        ):
            raise ValueError(
                "Plaintext ADMIN_PASSWORD in env is not supported. "
                "Use ADMIN_PASSWORD_HASH (bcrypt hash) or ADMIN_PASSWORD_FILE "
                "(plaintext file via Docker / Kubernetes secrets)."
            )

        # Resolve username (both sources are plaintext, fully symmetric).
        username = resolve_secret(
            self.admin_username,
            str(self.admin_username_file) if self.admin_username_file else None,
            name="ADMIN_USERNAME",
        )

        # Resolve password hash. ``ADMIN_PASSWORD_HASH`` is a hash;
        # ``ADMIN_PASSWORD_FILE`` is plaintext that we hash here so the
        # in-memory shape is uniform. When both are set the file wins
        # (more secure source) and we log a warning.
        if self.admin_password_hash is not None and self.admin_password_file is not None:
            logger.warning(
                "Both ADMIN_PASSWORD_HASH and ADMIN_PASSWORD_FILE are set — "
                "using file value, ignoring inline hash."
            )
        password_hash: str | None = None
        if self.admin_password_file is not None:
            plaintext = resolve_secret(
                None,
                str(self.admin_password_file),
                name="ADMIN_PASSWORD",
            )
            # ``resolve_secret`` returns ``None`` only when *both* sources
            # are absent; we've already established ``file`` is present.
            assert plaintext is not None
            password_hash = hash_password(plaintext)
        elif self.admin_password_hash is not None:
            password_hash = self.admin_password_hash.get_secret_value()

        # Mode invariant: anything non-public needs an admin.
        if self.auth_mode != "public":
            if not username:
                raise ValueError(
                    f"AUTH_MODE={self.auth_mode} requires ADMIN_USERNAME (or ADMIN_USERNAME_FILE)."
                )
            if not password_hash:
                raise ValueError(
                    f"AUTH_MODE={self.auth_mode} requires ADMIN_PASSWORD_HASH "
                    "or ADMIN_PASSWORD_FILE."
                )

        # Write resolved values back so route code reads ``admin_username``
        # / ``admin_password_hash`` and never touches the ``_FILE`` fields.
        self.admin_username = username
        self.admin_password_hash = SecretStr(password_hash) if password_hash else None
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use as a FastAPI dependency.

    Emits one-time startup warnings for: auto-generated session secret,
    and any deprecated config knob that's still set.
    """
    settings = Settings()
    if settings._session_secret_autogenerated:
        logger.warning(
            "SESSION_SECRET not set — generated a random one. "
            "Sessions will not survive a restart. Set SESSION_SECRET or "
            "SESSION_SECRET_FILE explicitly in production."
        )
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI dependency annotation. Inject as a route parameter:

    @app.get("/...")
    def handler(settings: SettingsDep) -> ...:
        ...
"""
