import json
import logging
import re
from collections.abc import Iterator

import pytest
import structlog
from fastapi.testclient import TestClient

from layerloupe.logging import configure_logging
from layerloupe.main import app


@pytest.fixture(autouse=True)
def _reset_logging() -> Iterator[None]:
    """Restore default structlog config after each test so we don't leak state."""
    yield
    structlog.reset_defaults()


def _parse_json_log_lines(stdout: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]


def test_request_middleware_emits_request_completed_with_context(
    capfd: pytest.CaptureFixture[str],
) -> None:
    # Force JSON mode so we can parse the actual stdout. capture_logs() won't
    # work here because it bypasses the merge_contextvars processor.
    configure_logging(level="info", json=True)

    with TestClient(app) as client:
        # Re-apply config: lifespan reset structlog to default settings.
        configure_logging(level="info", json=True)
        capfd.readouterr()  # discard anything emitted during startup
        # /api/info is logged; /api/healthz and /api/readyz are filtered out
        # to keep the access log out of k8s-probe noise.
        response = client.get("/api/info")
    assert response.status_code == 200

    out, _ = capfd.readouterr()
    entries = _parse_json_log_lines(out)
    completed = [e for e in entries if e.get("event") == "request_completed"]
    assert len(completed) == 1
    entry = completed[0]
    assert entry["method"] == "GET"
    assert entry["path"] == "/api/info"
    assert entry["status_code"] == 200
    assert isinstance(entry["duration_ms"], float)
    assert entry["duration_ms"] >= 0
    assert "request_id" in entry
    assert len(str(entry["request_id"])) > 0


def test_request_id_propagates_to_response_header() -> None:
    with TestClient(app) as client:
        response = client.get("/api/healthz")
    assert "x-request-id" in response.headers
    rid = response.headers["x-request-id"]
    # uuid4 hex form is 32 chars, all hexadecimal.
    assert re.fullmatch(r"[0-9a-f]{32}", rid) is not None


def test_inbound_request_id_is_honored() -> None:
    incoming = "trace-abc-123"
    with TestClient(app) as client:
        response = client.get("/api/healthz", headers={"X-Request-ID": incoming})
    assert response.headers["x-request-id"] == incoming


def test_request_id_differs_per_request() -> None:
    with TestClient(app) as client:
        r1 = client.get("/api/healthz")
        r2 = client.get("/api/healthz")
    assert r1.headers["x-request-id"] != r2.headers["x-request-id"]


def test_json_mode_emits_valid_json(capfd: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="info", json=True)
    log = structlog.get_logger()
    log.info("hello", foo="bar", number=42)

    out, _ = capfd.readouterr()
    line = out.strip().splitlines()[-1]
    parsed = json.loads(line)
    assert parsed["event"] == "hello"
    assert parsed["foo"] == "bar"
    assert parsed["number"] == 42
    assert parsed["level"] == "info"
    assert "timestamp" in parsed


def test_console_mode_is_human_readable(capfd: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="info", json=False)
    log = structlog.get_logger()
    log.info("hello", foo="bar")

    out, _ = capfd.readouterr()
    line = out.strip().splitlines()[-1]
    # Console output is not JSON.
    with pytest.raises(json.JSONDecodeError):
        json.loads(line)
    assert "hello" in line
    assert "foo" in line


def test_log_level_filters_below(capfd: pytest.CaptureFixture[str]) -> None:
    configure_logging(level="warning", json=True)
    log = structlog.get_logger()
    log.info("filtered_out")
    log.warning("kept")

    out, _ = capfd.readouterr()
    lines = [line for line in out.splitlines() if line.strip()]
    events = [json.loads(line)["event"] for line in lines if line.startswith("{")]
    assert "filtered_out" not in events
    assert "kept" in events


def test_configure_logging_is_idempotent() -> None:
    configure_logging(level="info", json=False)
    configure_logging(level="debug", json=True)
    # Second call should win without errors.
    assert structlog.is_configured()


def test_uvicorn_access_logger_is_silenced() -> None:
    configure_logging(level="info", json=False)
    assert logging.getLogger("uvicorn.access").disabled is True
