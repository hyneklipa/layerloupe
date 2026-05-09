"""Application settings (env-driven via pydantic-settings)."""

from __future__ import annotations

import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import AnyHttpUrl, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    session_secret: SecretStr = Field(
        default_factory=lambda: SecretStr(secrets.token_urlsafe(32)),
        description=(
            "Secret used to sign session cookies. Auto-generated if not set "
            "(emits a startup warning — set it explicitly in production)."
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

    Logs a one-time warning if `session_secret` was auto-generated, since
    that means sessions won't survive a restart.
    """
    settings = Settings()
    # Detect "secret was generated" by re-reading env: if env wasn't set, the
    # default_factory ran. We check by looking up the env var ourselves.
    import os

    if not os.environ.get("SESSION_SECRET"):
        logger.warning(
            "SESSION_SECRET not set — generated a random one. "
            "Sessions will not survive a restart. Set it explicitly in production."
        )
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings)]
"""FastAPI dependency annotation. Inject as a route parameter:

    @app.get("/...")
    def handler(settings: SettingsDep) -> ...:
        ...
"""
