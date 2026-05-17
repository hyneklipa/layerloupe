from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from layerloupe import __version__
from layerloupe.api import auth, manifests, repositories, system
from layerloupe.config import get_settings
from layerloupe.deps import build_registry_client, get_identity
from layerloupe.logging import configure_logging, request_logging_middleware
from layerloupe.registry import (
    RegistryConnectionError,
    RegistryError,
    RegistryHTTPError,
)
from layerloupe.web import routes as web_routes


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    app.state.registry_client = build_registry_client(settings)
    try:
        yield
    finally:
        await app.state.registry_client.aclose()


app = FastAPI(title="LayerLoupe", version=__version__, lifespan=lifespan)

# Session cookie is signed (itsdangerous). Per-user registry credentials
# add Fernet encryption on top - see ``layerloupe.sessions``.
app.add_middleware(
    SessionMiddleware,
    secret_key=get_settings().session_secret.get_secret_value(),
    https_only=False,
    same_site="lax",
)
app.middleware("http")(request_logging_middleware)

app.include_router(system.router)
app.include_router(repositories.router)
app.include_router(manifests.router)
app.include_router(auth.router)
app.include_router(web_routes.router)

# Serve hand-rolled CSS / JS / favicon. The path is also referenced by
# templates via ``url_for('static', path=...)`` - keep the name in sync.
app.mount(
    "/static",
    StaticFiles(directory=str(web_routes.STATIC_DIR)),
    name="static",
)


# -- Exception → HTTP status mapping --------------------------------------
# Centralized so individual endpoints don't all need try/except wrappers.


@app.exception_handler(RegistryHTTPError)
async def _registry_http_error_handler(_request: Request, exc: RegistryHTTPError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": str(exc)},
    )


@app.exception_handler(RegistryConnectionError)
async def _registry_connection_error_handler(
    _request: Request, exc: RegistryConnectionError
) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc)})


@app.exception_handler(RegistryError)
async def _registry_error_handler(_request: Request, exc: RegistryError) -> JSONResponse:
    """Catch-all for parser / validation issues that don't carry a status code."""
    return JSONResponse(status_code=502, content={"detail": str(exc)})


# -- Browser-facing 404 / 500 → HTML; API / web-mutating → JSON -----------
#
# Two audiences: the htmx UI wants a styled error page, the JSON API wants
# a machine-readable detail. The path prefix is the cheapest discriminator
# we have - anything under ``/api/`` or ``/web/`` (the htmx-mutating routes)
# stays JSON, everything else renders an HTML error template.


def _wants_html(request: Request) -> bool:
    path = request.url.path
    return not (path.startswith("/api/") or path.startswith("/web/"))


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException) -> Response:
    # 401 on a browser route → bounce to login with ``next=<path>`` so
    # the user lands back where they came from after signing in. JSON
    # routes get a plain 401 + detail (htmx clients render their own
    # toast or follow ``HX-Redirect`` if a downstream layer sets one).
    if exc.status_code == 401 and _wants_html(request):
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        return RedirectResponse(
            url=f"/login?next={quote(target, safe='/')}",
            status_code=303,
        )
    if not _wants_html(request) or exc.status_code not in (404, 500, 502, 503):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )
    template = "errors/404.html" if exc.status_code == 404 else "errors/500.html"
    settings = get_settings()
    return web_routes.templates.TemplateResponse(
        request=request,
        name=template,
        status_code=exc.status_code,
        context={
            "title": settings.title,
            "version": __version__,
            "registry_public_url": str(settings.registry_public_url or settings.registry_url),
            "allow_registry_login": settings.allow_registry_login,
            "auth_mode": settings.auth_mode,
            "identity": get_identity(request),
            "session_username": (
                request.session.get("registry_username") if hasattr(request, "session") else None
            ),
            "path": request.url.path,
            "detail": exc.detail if exc.status_code >= 500 else None,
            "request_id": request.headers.get("x-request-id"),
        },
    )


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> Response:
    """Final safety net for truly unexpected exceptions."""
    if not _wants_html(request):
        return JSONResponse(status_code=500, content={"detail": str(exc)})
    settings = get_settings()
    return web_routes.templates.TemplateResponse(
        request=request,
        name="errors/500.html",
        status_code=500,
        context={
            "title": settings.title,
            "version": __version__,
            "registry_public_url": str(settings.registry_public_url or settings.registry_url),
            "allow_registry_login": settings.allow_registry_login,
            "auth_mode": settings.auth_mode,
            "identity": get_identity(request),
            "session_username": (
                request.session.get("registry_username") if hasattr(request, "session") else None
            ),
            "path": request.url.path,
            "detail": str(exc),
            "request_id": request.headers.get("x-request-id"),
        },
    )
