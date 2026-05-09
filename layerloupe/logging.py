"""Structured logging with structlog.

Two modes controlled by ``LOG_JSON``:

* ``false`` (default): pretty, colored, human-readable output for dev.
* ``true``: one JSON object per log line for production / log aggregators.

Request middleware binds a ``request_id`` into structlog's contextvars so
every log line emitted during a request is automatically tagged with it.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Literal

import structlog
from fastapi import Request, Response
from structlog.contextvars import bind_contextvars, clear_contextvars

LogLevel = Literal["debug", "info", "warning", "error"]

logger = structlog.get_logger()


def configure_logging(level: LogLevel = "info", json: bool = False) -> None:
    """Configure both stdlib logging and structlog.

    Idempotent: safe to call multiple times (e.g. from app lifespan + tests).
    """
    log_level = getattr(logging, level.upper())

    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    renderer: structlog.types.Processor
    if json:
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    # Stdlib root logger → matches level so warnings from libraries surface.
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level, force=True)

    # Silence uvicorn's default access logger; we emit our own structured one.
    logging.getLogger("uvicorn.access").disabled = True


# Probes are hit every few seconds by k8s / load balancers. Logging each
# would drown the access log; an outage will surface through the registry
# probe's structured logs already, no need to double up.
_LOG_FILTERED_PATHS: frozenset[str] = frozenset({"/api/healthz", "/api/readyz"})


async def request_logging_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Tag the request with a ``request_id`` and log when it completes.

    The ``request_id`` is taken from the inbound ``X-Request-ID`` header if
    present (useful for tracing through reverse proxies); otherwise we mint a
    fresh UUID. The header is echoed back on the response.

    Health / readiness probes are tagged with the request id (so any inner
    log line emitted during the probe is still tied to the call) but their
    completion line is suppressed to keep the noisy access log out of
    production stdout.
    """
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    bind_contextvars(request_id=request_id)

    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        logger.exception(
            "request_failed",
            method=request.method,
            path=request.url.path,
            duration_ms=duration_ms,
        )
        clear_contextvars()
        raise

    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    if request.url.path not in _LOG_FILTERED_PATHS:
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
    response.headers["x-request-id"] = request_id
    clear_contextvars()
    return response
