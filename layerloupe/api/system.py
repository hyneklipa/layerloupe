"""System endpoints - health, readiness, registry metadata.

The probe endpoints (``/healthz``, ``/readyz``) carry ``Cache-Control:
no-store`` so a misconfigured intermediate proxy can't return last-known-
good values for a process that's actually unhealthy. Both are also
filtered out of the access log by :func:`layerloupe.logging.request_logging_middleware`
so frequent k8s probes don't drown the production log.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from layerloupe import __version__
from layerloupe.config import SettingsDep
from layerloupe.deps import RegistryClientDep

router = APIRouter(prefix="/api", tags=["system"])

_PROBE_HEADERS = {"Cache-Control": "no-store"}


@router.get("/healthz")
def healthz() -> JSONResponse:
    """Liveness probe - always 200 once the process is running."""
    return JSONResponse(
        {"status": "ok", "version": __version__},
        headers=_PROBE_HEADERS,
    )


@router.get("/readyz")
async def readyz(client: RegistryClientDep) -> JSONResponse:
    """Readiness probe - 200 if the registry is reachable + authenticated, 503 otherwise."""
    probe = await client.probe()
    payload = {
        "status": "ready" if probe.authenticated else "not_ready",
        "registry": probe.to_dict(),
    }
    return JSONResponse(
        payload,
        status_code=200 if probe.authenticated else 503,
        headers=_PROBE_HEADERS,
    )


@router.get("/info")
def info(settings: SettingsDep) -> dict[str, object]:
    """Public registry metadata for the UI (no secrets).

    ``auth_mode`` and ``allow_delete`` are surfaced here because the
    JS layer toggles a few client-side affordances (e.g. keyboard
    shortcuts to the delete modal) based on whether delete is even
    possible at this site. The auth guard remains the source of
    truth - this field is just a UX hint.
    """
    return {
        "title": settings.title,
        "version": __version__,
        "registry_url": str(settings.registry_url),
        "registry_public_url": str(settings.registry_public_url),
        "ssl_verify": settings.ssl_verify,
        "auth_mode": settings.auth_mode,
        "allow_delete": settings.auth_mode == "admin",
        "allow_registry_login": settings.allow_registry_login,
    }
