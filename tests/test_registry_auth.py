import base64

import httpx
import pytest
from pydantic import SecretStr

from layerloupe.config import Settings
from layerloupe.registry import BasicAuth, RegistryClient, basic_auth_from_settings

# -- Pure unit: encoding --------------------------------------------------


def test_basic_auth_with_plain_string_password() -> None:
    auth = BasicAuth("alice", "s3cret")
    expected = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert auth.header_value == f"Basic {expected}"
    assert auth.as_headers() == {"Authorization": f"Basic {expected}"}


def test_basic_auth_unwraps_secret_str() -> None:
    auth = BasicAuth("alice", SecretStr("s3cret"))
    expected = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert auth.header_value == f"Basic {expected}"


def test_basic_auth_none_password_encodes_empty_string() -> None:
    """Token-style registries pass token in username slot, password unused."""
    auth = BasicAuth("oauth2accesstoken")
    expected = base64.b64encode(b"oauth2accesstoken:").decode("ascii")
    assert auth.header_value == f"Basic {expected}"


def test_basic_auth_handles_unicode_credentials() -> None:
    """Non-ASCII passwords must encode as UTF-8 before base64."""
    auth = BasicAuth("hynek", "héslo🔒")
    expected = base64.b64encode("hynek:héslo🔒".encode()).decode("ascii")
    assert auth.header_value == f"Basic {expected}"


def test_basic_auth_empty_username_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty username"):
        BasicAuth("", "anything")


def test_basic_auth_repr_masks_password() -> None:
    auth = BasicAuth("alice", "s3cret")
    assert "s3cret" not in repr(auth)
    assert "alice" in repr(auth)


def test_basic_auth_does_not_retain_plaintext_password() -> None:
    """Sanity: only the encoded blob lives on the instance."""
    auth = BasicAuth("alice", "s3cret")
    # __slots__ ensures no surprise attributes.
    for slot in BasicAuth.__slots__:
        value = getattr(auth, slot)
        if isinstance(value, str):
            assert "s3cret" not in value or slot == "_encoded"


# -- Settings integration -------------------------------------------------


def test_basic_auth_from_settings_returns_none_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for k in ("REGISTRY_USERNAME", "REGISTRY_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    settings = Settings()
    assert basic_auth_from_settings(settings) is None


def test_basic_auth_from_settings_builds_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REGISTRY_USERNAME", "alice")
    monkeypatch.setenv("REGISTRY_PASSWORD", "s3cret")
    settings = Settings()
    auth = basic_auth_from_settings(settings)
    assert auth is not None
    expected = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert auth.header_value == f"Basic {expected}"


# -- End-to-end with RegistryClient via MockTransport ---------------------


async def test_basic_auth_header_reaches_registry() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"repositories": []})

    auth = BasicAuth("alice", "s3cret")
    async with RegistryClient(
        "https://registry.example.com",
        default_headers=auth.as_headers(),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_json("/v2/_catalog")

    expected = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert seen["authorization"] == f"Basic {expected}"


async def test_no_auth_header_when_credentials_missing() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"repositories": []})

    async with RegistryClient(
        "https://registry.example.com",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get_json("/v2/_catalog")

    assert "authorization" not in seen
