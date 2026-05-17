"""Tests for the ``Identity`` model and session serialization."""

from __future__ import annotations

import pytest

from layerloupe.auth import ADMIN_ROLE, ANONYMOUS, Identity


def test_anonymous_singleton_flags() -> None:
    assert ANONYMOUS.is_anonymous is True
    assert ANONYMOUS.is_admin is False
    assert ANONYMOUS.username == ""
    assert ANONYMOUS.provider == "anonymous"


def test_identity_with_admin_role() -> None:
    user = Identity(username="alice", roles=frozenset({ADMIN_ROLE}), provider="env")
    assert user.is_anonymous is False
    assert user.is_admin is True


def test_identity_without_admin_role() -> None:
    user = Identity(username="bob", roles=frozenset({"viewer"}), provider="env")
    assert user.is_anonymous is False
    assert user.is_admin is False


def test_identity_is_immutable() -> None:
    """Frozen dataclass - accidental mutation should raise."""
    import dataclasses

    user = Identity(username="alice", roles=frozenset({"admin"}), provider="env")
    with pytest.raises(dataclasses.FrozenInstanceError):
        user.username = "bob"  # type: ignore[misc]


def test_identity_hashable() -> None:
    """Required so ``Identity`` can live in a set / be used as a dict key."""
    a = Identity(username="x", roles=frozenset({"admin"}), provider="env")
    b = Identity(username="x", roles=frozenset({"admin"}), provider="env")
    assert hash(a) == hash(b)
    assert a == b


# -- Session round-trip --------------------------------------------------


def test_to_session_returns_jsonable_dict() -> None:
    user = Identity(username="alice", roles=frozenset({"admin", "viewer"}), provider="env")
    payload = user.to_session()
    assert payload == {
        "username": "alice",
        "roles": ["admin", "viewer"],  # sorted
        "provider": "env",
    }


def test_to_session_sorts_roles_for_stable_bytes() -> None:
    """Two equivalent identities must serialize identically, regardless
    of frozenset iteration order - otherwise cookie bytes diverge."""
    a = Identity(username="x", roles=frozenset({"b", "a", "c"}), provider="env")
    b = Identity(username="x", roles=frozenset({"c", "a", "b"}), provider="env")
    assert a.to_session() == b.to_session()
    assert a.to_session()["roles"] == ["a", "b", "c"]


def test_from_session_round_trip() -> None:
    original = Identity(username="alice", roles=frozenset({"admin"}), provider="env")
    restored = Identity.from_session(original.to_session())
    assert restored == original


def test_from_session_rejects_non_dict() -> None:
    assert Identity.from_session("not a dict") is None
    assert Identity.from_session(None) is None
    assert Identity.from_session(["alice", ["admin"], "env"]) is None


def test_from_session_rejects_missing_fields() -> None:
    assert Identity.from_session({"username": "alice", "roles": ["admin"]}) is None
    assert Identity.from_session({"username": "alice", "provider": "env"}) is None


def test_from_session_rejects_wrong_types() -> None:
    assert Identity.from_session({"username": 1, "roles": ["admin"], "provider": "env"}) is None
    assert Identity.from_session({"username": "alice", "roles": "admin", "provider": "env"}) is None
    assert Identity.from_session({"username": "alice", "roles": [1, 2], "provider": "env"}) is None
    assert (
        Identity.from_session({"username": "alice", "roles": ["admin"], "provider": None}) is None
    )


def test_from_session_preserves_roles_as_frozenset() -> None:
    restored = Identity.from_session(
        {"username": "alice", "roles": ["admin", "viewer"], "provider": "env"}
    )
    assert restored is not None
    assert isinstance(restored.roles, frozenset)
    assert restored.roles == frozenset({"admin", "viewer"})
