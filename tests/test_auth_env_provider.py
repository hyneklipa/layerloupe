"""Tests for ``EnvAdminProvider`` — bcrypt verification path."""

from __future__ import annotations

import pytest

from layerloupe.auth import ADMIN_ROLE
from layerloupe.auth.env_provider import EnvAdminProvider, hash_password


@pytest.fixture
def admin_hash() -> str:
    """Real bcrypt hash of ``"correct-horse-battery-staple"`` — cheap to
    reuse across tests but freshly generated per fixture so changes to
    rounds don't bleed across files."""
    return hash_password("correct-horse-battery-staple", rounds=4)


@pytest.fixture
def provider(admin_hash: str) -> EnvAdminProvider:
    return EnvAdminProvider(username="admin", password_hash=admin_hash)


# -- Construction --------------------------------------------------------


def test_provider_rejects_empty_username(admin_hash: str) -> None:
    with pytest.raises(ValueError, match="username"):
        EnvAdminProvider(username="", password_hash=admin_hash)


def test_provider_rejects_empty_password_hash() -> None:
    with pytest.raises(ValueError, match="password hash"):
        EnvAdminProvider(username="admin", password_hash="")


def test_provider_name_is_env(provider: EnvAdminProvider) -> None:
    """Used in ``Identity.provider`` and in audit logs."""
    assert provider.name == "env"


# -- Authentication ------------------------------------------------------


async def test_authenticate_success_returns_admin_identity(
    provider: EnvAdminProvider,
) -> None:
    identity = await provider.authenticate("admin", "correct-horse-battery-staple")
    assert identity is not None
    assert identity.username == "admin"
    assert ADMIN_ROLE in identity.roles
    assert identity.provider == "env"
    assert identity.is_admin is True


async def test_authenticate_wrong_password_returns_none(
    provider: EnvAdminProvider,
) -> None:
    assert await provider.authenticate("admin", "wrong-password") is None


async def test_authenticate_wrong_username_returns_none(
    provider: EnvAdminProvider,
) -> None:
    """Same password, wrong username → still rejected, even though
    bcrypt would have matched against the dummy hash."""
    assert await provider.authenticate("not-admin", "correct-horse-battery-staple") is None


async def test_authenticate_both_wrong_returns_none(
    provider: EnvAdminProvider,
) -> None:
    assert await provider.authenticate("not-admin", "wrong-password") is None


async def test_authenticate_empty_credentials_returns_none(
    provider: EnvAdminProvider,
) -> None:
    assert await provider.authenticate("", "") is None
    assert await provider.authenticate("admin", "") is None
    assert await provider.authenticate("", "correct-horse-battery-staple") is None


async def test_authenticate_rejects_malformed_hash() -> None:
    """A non-bcrypt hash in the env shouldn't crash — just always fail."""
    provider = EnvAdminProvider(username="admin", password_hash="not-a-bcrypt-hash")
    assert await provider.authenticate("admin", "any-password") is None


async def test_authenticate_is_case_sensitive_on_username(
    provider: EnvAdminProvider,
) -> None:
    """Single admin account — no canonicalization, matches must be exact."""
    assert await provider.authenticate("Admin", "correct-horse-battery-staple") is None
    assert await provider.authenticate("ADMIN", "correct-horse-battery-staple") is None


# -- hash_password helper ------------------------------------------------


def test_hash_password_produces_verifiable_bcrypt_hash() -> None:
    h = hash_password("hunter2", rounds=4)
    assert h.startswith("$2b$")
    # Round-trip via the provider so we know the format is what the
    # provider will accept later.
    import asyncio

    provider = EnvAdminProvider(username="admin", password_hash=h)
    identity = asyncio.run(provider.authenticate("admin", "hunter2"))
    assert identity is not None


def test_hash_password_uses_distinct_salts() -> None:
    """Two hashes of the same plaintext must differ (salt randomness)."""
    a = hash_password("same-password", rounds=4)
    b = hash_password("same-password", rounds=4)
    assert a != b
