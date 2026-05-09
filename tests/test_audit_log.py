"""Tests for the audit log on manifest delete."""

from __future__ import annotations

import hashlib
import io
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient

from layerloupe.audit import log_manifest_deleted
from layerloupe.config import get_settings
from layerloupe.deps import get_registry_client
from layerloupe.logging import configure_logging
from layerloupe.main import app
from layerloupe.registry import MediaType, RegistryClient


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _make_handler(
    *, digest: str = "sha256:" + "f" * 64
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if request.method == "HEAD" and "/manifests/" in path:
            return httpx.Response(200, headers={"docker-content-digest": digest})
        if request.method == "DELETE" and "/manifests/" in path:
            return httpx.Response(202)
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=b'{"schemaVersion":2}',
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": digest,
                },
            )
        return httpx.Response(404)

    return handler


@pytest.fixture
def use_handler() -> Iterator[dict[str, Callable[[httpx.Request], httpx.Response]]]:
    box: dict[str, Callable[[httpx.Request], httpx.Response]] = {"handler": _make_handler()}

    def _override() -> RegistryClient:
        return RegistryClient(
            "https://registry.example.com",
            transport=httpx.MockTransport(lambda r: box["handler"](r)),
        )

    app.dependency_overrides[get_registry_client] = _override
    try:
        yield box
    finally:
        app.dependency_overrides.pop(get_registry_client, None)


@pytest.fixture
def allow_delete(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ALLOW_DELETE", "true")
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


# -- Pure unit -----------------------------------------------------------


def _fake_request(*, session: dict | None = None, ip: str | None = "203.0.113.7") -> MagicMock:
    """Smallest possible thing that quacks like a Starlette Request for our use."""
    req = MagicMock()
    req.session = session if session is not None else {}
    req.client = MagicMock(host=ip) if ip is not None else None
    return req


def test_log_manifest_deleted_emits_event_to_stdout(
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The structured log carries actor / repo / digest / reference."""
    configure_logging(level="info", json=True)
    capfd.readouterr()

    log_manifest_deleted(
        _fake_request(session={"registry_username": "alice"}),
        repository="library/ubuntu",
        reference="latest",
        digest="sha256:abc123",
    )
    structlog.reset_defaults()

    out, _ = capfd.readouterr()
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    deleted = [e for e in lines if e.get("event") == "manifest_deleted"]
    assert len(deleted) == 1
    entry = deleted[0]
    assert entry["actor"] == "alice"
    assert entry["repository"] == "library/ubuntu"
    assert entry["reference"] == "latest"
    assert entry["digest"] == "sha256:abc123"
    assert entry["ip"] == "203.0.113.7"


def test_log_writes_to_audit_file_when_path_set(tmp_path: Path) -> None:
    audit_file = tmp_path / "subdir" / "audit.log"  # subdir doesn't exist yet
    log_manifest_deleted(
        _fake_request(session={"registry_username": "bob"}),
        repository="library/ubuntu",
        reference="22.04",
        digest="sha256:def456",
        audit_log_path=audit_file,
    )

    assert audit_file.exists()
    record = json.loads(audit_file.read_text(encoding="utf-8").strip())
    assert record["event"] == "manifest_deleted"
    assert record["actor"] == "bob"
    assert record["repository"] == "library/ubuntu"
    assert record["reference"] == "22.04"
    assert record["digest"] == "sha256:def456"
    assert "timestamp" in record


def test_audit_file_appends_one_line_per_delete(tmp_path: Path) -> None:
    audit_file = tmp_path / "audit.log"

    for i in range(3):
        log_manifest_deleted(
            _fake_request(),
            repository=f"repo-{i}",
            reference="latest",
            digest=f"sha256:{i}",
            audit_log_path=audit_file,
        )

    lines = audit_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    repos = [json.loads(line)["repository"] for line in lines]
    assert repos == ["repo-0", "repo-1", "repo-2"]


def test_audit_file_failure_does_not_raise(tmp_path: Path) -> None:
    """An unwritable audit path must not break the delete response path."""
    # Create a file at the path of the parent — opening for append will fail
    # because the "parent" can't be made into a directory.
    blocker = tmp_path / "audit.log"
    blocker.write_text("placeholder")
    nested = blocker / "child.log"  # parent is a file, not a dir

    # Should not raise — failure is logged and swallowed.
    log_manifest_deleted(
        _fake_request(),
        repository="foo",
        reference="latest",
        digest="sha256:abc",
        audit_log_path=nested,
    )


def test_audit_actor_is_env_creds_when_no_session() -> None:
    """A request without a session-stored username uses the env actor name."""
    buf = io.StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = buf
        configure_logging(level="info", json=True)
        log_manifest_deleted(
            _fake_request(session={}),
            repository="foo",
            reference="latest",
            digest="sha256:x",
        )
    finally:
        sys.stdout = old_stdout
        structlog.reset_defaults()

    lines = [json.loads(line) for line in buf.getvalue().splitlines() if line.startswith("{")]
    deleted = [e for e in lines if e.get("event") == "manifest_deleted"]
    assert len(deleted) >= 1
    assert deleted[-1]["actor"] == "env-creds"


def test_audit_handles_request_without_client() -> None:
    """``request.client is None`` when running under ASGI lifespan tests."""
    log_manifest_deleted(
        _fake_request(session={"registry_username": "carol"}, ip=None),
        repository="foo",
        reference="latest",
        digest="sha256:y",
    )
    # No raise — only stdout. Smoke test.


# -- End-to-end via API DELETE ------------------------------------------


def test_api_delete_writes_audit_record(
    tmp_path: Path,
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_file = tmp_path / "delete.log"
    monkeypatch.setenv("ALLOW_DELETE", "true")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
    get_settings.cache_clear()

    digest = "sha256:" + "a" * 64
    use_handler["handler"] = _make_handler(digest=digest)

    try:
        with TestClient(app) as client:
            response = client.delete("/api/repositories/library/ubuntu/manifests/latest")
        assert response.status_code == 200
    finally:
        get_settings.cache_clear()

    assert audit_file.exists()
    record = json.loads(audit_file.read_text(encoding="utf-8").strip())
    assert record["event"] == "manifest_deleted"
    assert record["repository"] == "library/ubuntu"
    assert record["reference"] == "latest"
    # The digest in the audit log is the *resolved* digest, not the requested tag.
    assert record["digest"] == digest


def test_web_delete_writes_audit_record(
    tmp_path: Path,
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit_file = tmp_path / "web-delete.log"
    monkeypatch.setenv("ALLOW_DELETE", "true")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
    get_settings.cache_clear()

    digest = "sha256:" + "b" * 64
    use_handler["handler"] = _make_handler(digest=digest)

    try:
        with TestClient(app) as client:
            response = client.delete("/web/repositories/foo/manifests/v1.2.3")
        assert response.status_code == 204
    finally:
        get_settings.cache_clear()

    assert audit_file.exists()
    record = json.loads(audit_file.read_text(encoding="utf-8").strip())
    assert record["repository"] == "foo"
    assert record["reference"] == "v1.2.3"
    assert record["digest"] == digest


def test_failed_delete_does_not_emit_audit_record(
    tmp_path: Path,
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 404 from registry must NOT show up in the audit log."""
    audit_file = tmp_path / "audit.log"
    monkeypatch.setenv("ALLOW_DELETE", "true")
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
    get_settings.cache_clear()

    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})

    use_handler["handler"] = deny

    try:
        with TestClient(app) as client:
            response = client.delete("/api/repositories/foo/manifests/missing")
        assert response.status_code == 404
    finally:
        get_settings.cache_clear()

    # No file was created — audit only fires on successful delete.
    assert not audit_file.exists()


def test_disabled_delete_does_not_emit_audit_record(
    tmp_path: Path,
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """403 from gating doesn't generate an audit entry either."""
    audit_file = tmp_path / "audit.log"
    monkeypatch.delenv("ALLOW_DELETE", raising=False)
    monkeypatch.setenv("AUDIT_LOG_PATH", str(audit_file))
    get_settings.cache_clear()

    try:
        with TestClient(app) as client:
            response = client.delete("/api/repositories/foo/manifests/latest")
        assert response.status_code == 403
    finally:
        get_settings.cache_clear()

    assert not audit_file.exists()


# -- Settings -----------------------------------------------------------


def test_settings_audit_log_path_defaults_to_none() -> None:
    from layerloupe.config import Settings

    s = Settings()
    assert s.audit_log_path is None


def test_settings_audit_log_path_parses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDIT_LOG_PATH", "/var/log/layerloupe/audit.log")
    from layerloupe.config import Settings

    s = Settings()
    assert s.audit_log_path == Path("/var/log/layerloupe/audit.log")
