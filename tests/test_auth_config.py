"""Tests for ``AUTH_MODE`` + ``ADMIN_*`` settings validation.

Covers the resolution rules from
``_docs/06-ui-access-control-redesign.md`` § 3 — env vs file sources,
plaintext-in-env rejection, mode invariants, and deprecation warnings
for the old ``UI_USERNAME`` / ``UI_PASSWORD`` / ``ALLOW_DELETE`` knobs.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import ValidationError

from layerloupe.auth.env_provider import EnvAdminProvider, hash_password
from layerloupe.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Clear env so each test starts from a clean baseline."""
    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def admin_hash() -> str:
    """A real bcrypt hash for ``"hunter2"`` at low rounds — fast in tests."""
    return hash_password("hunter2", rounds=4)


# -- Default + public mode ----------------------------------------------


def test_default_mode_is_public() -> None:
    assert Settings().auth_mode == "public"


def test_public_mode_does_not_require_admin_creds() -> None:
    """Public mode is anonymous-only; admin creds are simply ignored."""
    s = Settings()
    assert s.admin_username is None
    assert s.admin_password_hash is None


def test_public_mode_accepts_admin_creds_without_complaint(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    """Operator may have admin creds set while leaving mode at public —
    e.g. flipping mode for testing. Don't raise."""
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    s = Settings()
    assert s.auth_mode == "public"
    assert s.admin_username == "admin"


# -- Protected + admin modes --------------------------------------------


def test_protected_mode_requires_admin_username(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    with pytest.raises(ValidationError, match="ADMIN_USERNAME"):
        Settings()


def test_protected_mode_requires_admin_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTH_MODE", "protected")
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    with pytest.raises(ValidationError, match="ADMIN_PASSWORD"):
        Settings()


def test_admin_mode_requires_both(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "admin")
    with pytest.raises(ValidationError):
        Settings()


def test_admin_mode_with_env_creds_resolves(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    s = Settings()
    assert s.auth_mode == "admin"
    assert s.admin_username == "alice"
    assert s.admin_password_hash is not None
    assert s.admin_password_hash.get_secret_value() == admin_hash


def test_invalid_auth_mode_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTH_MODE", "superuser")
    with pytest.raises(ValidationError):
        Settings()


# -- File-based admin creds ---------------------------------------------


def test_admin_username_from_file(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str, tmp_path: Path
) -> None:
    name_file = tmp_path / "admin-username"
    name_file.write_text("alice-from-file\n")
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME_FILE", str(name_file))
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    s = Settings()
    assert s.admin_username == "alice-from-file"


def test_admin_password_from_file_is_hashed_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Plaintext in file → bcrypt hash in memory.

    Verifies the hash by feeding it to ``EnvAdminProvider`` and
    authenticating with the original plaintext — the round-trip proves
    we hashed (a) bcrypt-shaped and (b) the actual file contents.
    """
    pw_file = tmp_path / "admin-password"
    pw_file.write_text("file-secret-plaintext\n")
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(pw_file))
    s = Settings()
    assert s.admin_password_hash is not None
    stored_hash = s.admin_password_hash.get_secret_value()
    assert stored_hash.startswith("$2b$")
    # The stored hash must verify against the original plaintext.
    import asyncio

    provider = EnvAdminProvider(username="alice", password_hash=stored_hash)
    identity = asyncio.run(provider.authenticate("alice", "file-secret-plaintext"))
    assert identity is not None


def test_admin_password_file_wins_over_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    admin_hash: str,
) -> None:
    """When both are set, the file value (plaintext → hash) takes
    precedence and we log a warning. The resolved hash must verify
    against the file contents, not the env hash."""
    pw_file = tmp_path / "admin-password"
    pw_file.write_text("from-the-file")
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    monkeypatch.setenv("ADMIN_PASSWORD_FILE", str(pw_file))
    with caplog.at_level(logging.WARNING, logger="layerloupe.config"):
        s = Settings()
    assert any(
        "ADMIN_PASSWORD_HASH" in rec.message and "ADMIN_PASSWORD_FILE" in rec.message
        for rec in caplog.records
    )
    assert s.admin_password_hash is not None
    stored = s.admin_password_hash.get_secret_value()
    # The hash should verify the *file* plaintext, not "hunter2" (the
    # plaintext that ``admin_hash`` was built from).
    import asyncio

    provider = EnvAdminProvider(username="alice", password_hash=stored)
    assert asyncio.run(provider.authenticate("alice", "from-the-file")) is not None
    assert asyncio.run(provider.authenticate("alice", "hunter2")) is None


# -- Plaintext env rejection --------------------------------------------


def test_plaintext_admin_password_in_env_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ADMIN_PASSWORD`` (without ``_HASH`` / ``_FILE`` suffix) must
    fail at startup with a clear instruction — never silently accepted."""
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD", "plaintext-please-no")
    with pytest.raises(ValidationError, match="Plaintext ADMIN_PASSWORD"):
        Settings()


def test_admin_password_env_doesnt_block_hash_or_file(
    monkeypatch: pytest.MonkeyPatch, admin_hash: str
) -> None:
    """``ADMIN_PASSWORD`` env should only block when alone — if the
    operator also set ``_HASH`` or ``_FILE`` they're presumably
    transitioning and we honor the right one."""
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD", "should-be-ignored")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    s = Settings()  # no error
    assert s.admin_password_hash is not None


# -- Retired-knob silence (post-T7.7) -----------------------------------


def test_retired_ui_username_is_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``UI_USERNAME`` was retired in T7.7 — ``extra="ignore"`` drops it
    silently so old ``.env`` files don't crash startup. (Operators
    relying on it never had a working feature anyway — it was unused.)"""
    monkeypatch.setenv("UI_USERNAME", "legacy")
    monkeypatch.setenv("UI_PASSWORD", "legacy-pw")
    # No exception; the values aren't on the model.
    s = Settings()
    assert not hasattr(s, "ui_username")


def test_retired_allow_delete_is_silently_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ALLOW_DELETE`` was replaced by ``AUTH_MODE=admin`` in T7.7."""
    monkeypatch.setenv("ALLOW_DELETE", "true")
    s = Settings()
    assert not hasattr(s, "allow_delete")


def test_no_deprecation_warning_when_only_new_knobs_set(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, admin_hash: str
) -> None:
    """Clean config (new knobs only) → no deprecation noise on startup."""
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "alice")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", admin_hash)
    monkeypatch.setenv("SESSION_SECRET", "explicit-secret-no-autogen-warning")
    with caplog.at_level(logging.WARNING, logger="layerloupe.config"):
        get_settings()
    # No log records at all from layerloupe.config — clean startup.
    assert [rec for rec in caplog.records if rec.name == "layerloupe.config"] == []
