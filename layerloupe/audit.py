"""Audit log for destructive operations.

The only mutation LayerLoupe performs against a registry is ``DELETE`` on a
manifest, and operators want a clean trail of "who deleted what, when".
This module emits a structured ``manifest_deleted`` event to the regular
log stream, and — when ``AUDIT_LOG_PATH`` is configured — also
appends a JSON line to a dedicated audit file so the trail survives even
if the main log gets rotated or filtered downstream.

We never block the request on a failed audit-file write: a missing audit
file is logged once at WARNING level, not raised. The structured stdout
log always succeeds and is the source of truth for compliance use cases.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import structlog
from fastapi import Request

audit_logger = structlog.get_logger("layerloupe.audit")


_ENV_ACTOR = "env-creds"


def _session_actor(request: Request) -> str:
    """Resolve a request to a human-recognizable actor.

    Returns the session-stored username if the user has logged in via the
    UI, else ``"env-creds"`` to indicate the action used the global,
    env-configured registry credentials.
    """
    if hasattr(request, "session"):
        username = request.session.get("registry_username")
        if isinstance(username, str) and username:
            return username
    return _ENV_ACTOR


def _client_ip(request: Request) -> str | None:
    """Extract the client IP — ``None`` if the request didn't carry one."""
    return request.client.host if request.client is not None else None


def log_manifest_deleted(
    request: Request,
    *,
    repository: str,
    reference: str,
    digest: str,
    audit_log_path: Path | None = None,
) -> None:
    """Emit a structured audit event for a successful manifest delete.

    Args:
        request: FastAPI request, used to pull session creds + client IP.
        repository: Repository the manifest was deleted from.
        reference: What the user originally asked to delete (tag or digest).
        digest: The resolved digest the registry actually deleted.
        audit_log_path: Optional path to a JSONL audit file. When set, the
            same event is also appended here. File errors are swallowed
            (with a warning) so the main response keeps succeeding.
    """
    actor = _session_actor(request)
    ip = _client_ip(request)
    timestamp = datetime.now(UTC).isoformat()

    audit_logger.info(
        "manifest_deleted",
        actor=actor,
        ip=ip,
        repository=repository,
        reference=reference,
        digest=digest,
    )

    if audit_log_path is None:
        return

    record = {
        "event": "manifest_deleted",
        "timestamp": timestamp,
        "actor": actor,
        "ip": ip,
        "repository": repository,
        "reference": reference,
        "digest": digest,
    }
    try:
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        audit_logger.warning(
            "audit_file_write_failed",
            path=str(audit_log_path),
            error=str(exc),
        )
