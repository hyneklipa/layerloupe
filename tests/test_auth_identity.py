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
    payload = user.to_session(auth_mode="admin")
    assert payload == {
        "username": "alice",
        "roles": ["admin", "viewer"],  # sorted
        "provider": "env",
        "auth_mode": "admin",
    }


def test_to_session_sorts_roles_for_stable_bytes() -> None:
    """Two equivalent identities must serialize identically, regardless
    of frozenset iteration order - otherwise cookie bytes diverge."""
    a = Identity(username="x", roles=frozenset({"b", "a", "c"}), provider="env")
    b = Identity(username="x", roles=frozenset({"c", "a", "b"}), provider="env")
    assert a.to_session(auth_mode="admin") == b.to_session(auth_mode="admin")
    assert a.to_session(auth_mode="admin")["roles"] == ["a", "b", "c"]


def test_from_session_round_trip() -> None:
    original = Identity(username="alice", roles=frozenset({"admin"}), provider="env")
    restored = Identity.from_session(
        original.to_session(auth_mode="admin"),
        expected_auth_mode="admin",
    )
    assert restored == original


def test_from_session_rejects_non_dict() -> None:
    assert Identity.from_session("not a dict", expected_auth_mode="admin") is None
    assert Identity.from_session(None, expected_auth_mode="admin") is None
    assert Identity.from_session(["alice", ["admin"], "env"], expected_auth_mode="admin") is None


def test_from_session_rejects_missing_fields() -> None:
    assert (
        Identity.from_session({"username": "alice", "roles": ["admin"]}, expected_auth_mode="admin")
        is None
    )
    assert (
        Identity.from_session({"username": "alice", "provider": "env"}, expected_auth_mode="admin")
        is None
    )
    # ``auth_mode`` missing → invalid (pre-T7.10 cookies hit this path
    # after a deploy and force a re-login; behaves like a one-shot
    # session invalidation, documented in the CHANGELOG).
    assert (
        Identity.from_session(
            {"username": "alice", "roles": ["admin"], "provider": "env"},
            expected_auth_mode="admin",
        )
        is None
    )


def test_from_session_rejects_wrong_types() -> None:
    assert (
        Identity.from_session(
            {"username": 1, "roles": ["admin"], "provider": "env", "auth_mode": "admin"},
            expected_auth_mode="admin",
        )
        is None
    )
    assert (
        Identity.from_session(
            {"username": "alice", "roles": "admin", "provider": "env", "auth_mode": "admin"},
            expected_auth_mode="admin",
        )
        is None
    )
    assert (
        Identity.from_session(
            {"username": "alice", "roles": [1, 2], "provider": "env", "auth_mode": "admin"},
            expected_auth_mode="admin",
        )
        is None
    )
    assert (
        Identity.from_session(
            {"username": "alice", "roles": ["admin"], "provider": None, "auth_mode": "admin"},
            expected_auth_mode="admin",
        )
        is None
    )
    assert (
        Identity.from_session(
            {"username": "alice", "roles": ["admin"], "provider": "env", "auth_mode": 1},
            expected_auth_mode="admin",
        )
        is None
    )


def test_from_session_preserves_roles_as_frozenset() -> None:
    restored = Identity.from_session(
        {
            "username": "alice",
            "roles": ["admin", "viewer"],
            "provider": "env",
            "auth_mode": "admin",
        },
        expected_auth_mode="admin",
    )
    assert restored is not None
    assert isinstance(restored.roles, frozenset)
    assert restored.roles == frozenset({"admin", "viewer"})


# -- T7.10: auth_mode mismatch invalidation -------------------------------


def test_from_session_rejects_auth_mode_mismatch() -> None:
    """Cookie minted under ``protected`` must not survive into ``admin`` mode.

    The role-set carried by the cookie was correct for the old mode but
    no longer reflects what the active provider would grant. Returning
    ``None`` forces the user back through the login flow so they get a
    fresh role-set.
    """
    payload = {
        "username": "alice",
        "roles": [],
        "provider": "env",
        "auth_mode": "protected",
    }
    assert Identity.from_session(payload, expected_auth_mode="admin") is None


def test_from_session_logs_warning_on_auth_mode_mismatch() -> None:
    """Operators want a breadcrumb when sessions get invalidated by a
    config flip - the warning is the only signal that the post-deploy
    re-login wave wasn't a coincidence."""
    import structlog.testing

    payload = {
        "username": "alice",
        "roles": ["admin"],
        "provider": "env",
        "auth_mode": "admin",
    }
    with structlog.testing.capture_logs() as logs:
        Identity.from_session(payload, expected_auth_mode="protected")
    events = [entry for entry in logs if entry.get("event") == "session_auth_mode_mismatch"]
    assert len(events) == 1
    assert events[0]["log_level"] == "warning"
    assert events[0]["username"] == "alice"
    assert events[0]["payload_auth_mode"] == "admin"
    assert events[0]["expected_auth_mode"] == "protected"


def test_from_session_no_warning_on_malformed_payload() -> None:
    """Malformed payloads (tampering, schema drift, post-SESSION_SECRET-
    rotation cruft) stay silent - logging them at WARNING would be noisy
    after every secret rotation. Only the auth-mode mismatch case is
    operationally interesting enough to surface."""
    import structlog.testing

    with structlog.testing.capture_logs() as logs:
        Identity.from_session({"username": "alice"}, expected_auth_mode="admin")
        Identity.from_session("garbage", expected_auth_mode="admin")
    assert not any(entry.get("event") == "session_auth_mode_mismatch" for entry in logs)
