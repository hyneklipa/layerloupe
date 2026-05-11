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

from layerloupe.config_secrets import resolve_secret

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """LayerLoupe runtime configuration.

    Values are loaded from environment variables (no prefix — just
    ``REGISTRY_URL``, ``ALLOW_DELETE``, …) and optionally from a ``.env``
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
    allow_delete: bool = Field(
        default=False, description="Show the delete-tag/manifest button in the UI."
    )

    # -- UI auth (basic auth in front of LayerLoupe itself) ------------------
    ui_username: str | None = Field(
        default=None, description="If set, require basic auth to access the UI."
    )
    ui_password: SecretStr | None = Field(
        default=None, description="Basic-auth password (used together with `ui_username`)."
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
    def _validate_ui_auth_pair(self) -> Settings:
        if self.ui_username and not self.ui_password:
            raise ValueError("UI_PASSWORD must be set when UI_USERNAME is set.")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Use as a FastAPI dependency.

    Logs a one-time warning if ``session_secret`` was auto-generated, since
    that means sessions won't survive a restart.
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
