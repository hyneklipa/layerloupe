"""HTML routes for the LayerLoupe UI.

Two route flavors:

* **Page routes** at ``/``, ``/repositories``, ``/repositories/{repo}``,
  ``/repositories/{repo}/manifests/{ref}``. Render the full ``index.html``
  shell with as many columns server-rendered as the URL provides info for —
  so a deep link goes straight to the right state on hard reload.

* **Fragment routes** at ``/partials/...``. Return just the inner ``<ul>``
  or info panel. htmx hits these for in-page swaps; they update the
  browser URL via ``hx-push-url``.

Both paths share the registry-side fetching logic via tiny ``_collect_*``
helpers so a click and a reload land on the same data.
"""

from __future__ import annotations

import platform as _platform_mod
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from layerloupe import __version__
from layerloupe.api.auth import _verify_credentials
from layerloupe.audit import log_manifest_deleted
from layerloupe.config import SettingsDep
from layerloupe.deps import (
    AdminDep,
    AuthProviderDep,
    BrowseAccessDep,
    RegistryClientDep,
    get_identity,
)
from layerloupe.registry import (
    AnnotationRow,
    ImageConfig,
    LayerRow,
    ManifestKind,
    Referrer,
    RegistryClient,
    RegistryConnectionError,
    RegistryError,
    RegistryHTTPError,
    UnifiedManifest,
    build_layer_rows,
    merge_annotations,
    to_unified,
)
from layerloupe.sessions import encrypt_password
from layerloupe.utils import human_size, human_time, sort_tags

_HERE = Path(__file__).parent
TEMPLATES_DIR = _HERE / "templates"
STATIC_DIR = _HERE / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.filters["human_size"] = human_size
templates.env.filters["human_time"] = human_time


router = APIRouter(tags=["web"], include_in_schema=False)


# -- Helpers: fetch + render context --------------------------------------


def _is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def _build_pull_command(public_url: str, repository: str, reference: str) -> str:
    host = public_url.rstrip("/")
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    sep = "@" if reference.startswith("sha256:") else ":"
    return f"docker pull {host}/{repository}{sep}{reference}"


async def _fetch_repos(client: RegistryClient, q: str | None) -> tuple[list[str], str | None]:
    """Return ``(repos, error)``.

    HTML routes prefer rendering the shell with an inline error banner over
    a hard 5xx — operators can still interact with the UI (theme toggle,
    sign in / out) when the registry is briefly unavailable.
    """
    items: list[str] = []
    try:
        async for repo in client.iter_repositories(query=q):
            items.append(repo)
            if len(items) >= 1000:
                break
    except (RegistryHTTPError, RegistryConnectionError, RegistryError) as e:
        return [], f"Could not load repository list: {e}"
    return items, None


async def _fetch_tags(client: RegistryClient, repository: str, q: str | None) -> list[str]:
    items: list[str] = []
    async for tag in client.iter_tags(repository, query=q):
        items.append(tag)
        if len(items) >= 5000:
            break
    return sort_tags(items)


async def _fetch_manifest(
    client: RegistryClient, repository: str, reference: str, public_url: str
) -> UnifiedManifest:
    manifest = await client.get_manifest(repository, reference)
    image_config: ImageConfig | None = None
    if manifest.kind in (ManifestKind.OCI_IMAGE, ManifestKind.DOCKER_V2):
        try:
            image_config = await client.get_image_config(repository, manifest)
        except (RegistryError, RegistryHTTPError):
            image_config = None
    pull: str | None = _build_pull_command(public_url, repository, reference)
    pull_digest: str | None = None
    if manifest.digest is not None:
        pull_digest = _build_pull_command(public_url, repository, manifest.digest)
        # When the user navigated to a digest URL, ``pull`` already equals
        # the digest variant — only show it once.
        if pull_digest == pull:
            pull = None
    return to_unified(
        manifest,
        image_config,
        pull_command=pull,
        pull_command_digest=pull_digest,
    )


_SCHEMA_1_MEDIA_TYPE_PREFIX = "application/vnd.docker.distribution.manifest.v1"


async def _fetch_referrers(
    client: RegistryClient,
    repository: str,
    manifest: UnifiedManifest | None,
) -> list[Referrer]:
    """Best-effort referrers fetch — empty on any failure.

    The web layer never wants to fail a manifest render because the
    referrers API hiccupped, so we swallow connection errors and HTTP
    errors here. The dedicated JSON endpoint at
    ``/api/.../referrers`` does propagate them.
    """
    if manifest is None or manifest.digest is None:
        return []
    if manifest.type != "image":
        # Index manifests don't carry referrers themselves; the per-platform
        # child manifest does.
        return []
    if manifest.media_type.startswith(_SCHEMA_1_MEDIA_TYPE_PREFIX):
        # Schema 1 predates the OCI 1.1 referrers spec.
        return []
    try:
        return await client.get_referrers(repository, manifest.digest)
    except (RegistryHTTPError, RegistryConnectionError, RegistryError):
        return []


def _layer_rows(manifest: UnifiedManifest | None) -> list[LayerRow]:
    """Build the Layers section pairing blob layers with history entries."""
    if manifest is None:
        return []
    history = (
        manifest.config.data.history
        if manifest.config is not None and manifest.config.data is not None
        else None
    )
    return build_layer_rows(manifest.layers, history)


def _display_platforms(manifest: UnifiedManifest | None) -> list[Any]:
    """Filter out ``unknown/unknown`` entries from a multi-arch index.

    Build attestation manifests (Cosign signatures, SLSA provenance, …)
    are listed inside the index alongside real platform manifests but
    advertise ``architecture: unknown`` / ``os: unknown``. They belong in
    the referrers panel, not the platform picker.
    """
    if manifest is None:
        return []
    return [
        p for p in manifest.platforms if not (p.architecture == "unknown" and p.os == "unknown")
    ]


def _annotation_rows(manifest: UnifiedManifest | None) -> list[AnnotationRow]:
    """Build the merged annotations table for the info panel.

    Pulls manifest-level ``annotations`` and the image-config ``Labels``
    into a single curated list so the UI doesn't have to render two
    near-duplicate sections.
    """
    if manifest is None:
        return []
    labels: dict[str, str] | None = None
    if manifest.config is not None and manifest.config.data is not None:
        labels = manifest.config.data.config.labels
    return merge_annotations(manifest.annotations, labels)


# OCI-standard architecture / OS names. Python's ``platform.machine()``
# uses uname-style names (``x86_64`` / ``aarch64``); registries speak the
# OCI vocabulary (``amd64`` / ``arm64``).
_OCI_ARCH_MAP: dict[str, str] = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm",
    "armv6l": "arm",
    "i686": "386",
    "i386": "386",
    "ppc64le": "ppc64le",
    "s390x": "s390x",
}


def _local_platform() -> tuple[str, str]:
    """Return ``(architecture, os)`` of the host LayerLoupe runs on, in OCI naming."""
    arch = _OCI_ARCH_MAP.get(_platform_mod.machine().lower(), _platform_mod.machine().lower())
    os_name = _platform_mod.system().lower()
    return arch, os_name


async def _annotation_rows_with_fallback(
    client: RegistryClient,
    repository: str,
    manifest: UnifiedManifest | None,
) -> tuple[list[AnnotationRow], str | None]:
    """Return ``(rows, fallback_label)``.

    Multi-arch indexes often carry no annotations of their own — useful
    metadata sits on each per-platform child. When the index is empty we
    try the child matching the host's architecture so the panel doesn't
    look unhelpfully blank. ``fallback_label`` is the human-readable
    platform name when the fallback was used (``amd64/linux``), else
    ``None``.
    """
    primary = _annotation_rows(manifest)
    if primary or manifest is None or manifest.type != "index":
        return primary, None

    local_arch, local_os = _local_platform()
    pick = next(
        (p for p in manifest.platforms if p.architecture == local_arch and p.os == local_os),
        None,
    )
    if pick is None:
        # The host's arch isn't in the index — pick any non-attestation
        # child so the user still sees something.
        pick = next(
            (
                p
                for p in manifest.platforms
                if not (p.architecture == "unknown" and p.os == "unknown")
            ),
            None,
        )
    if pick is None or pick.digest is None:
        return [], None

    try:
        child = await _fetch_manifest(client, repository, pick.digest, "")
    except (RegistryError, RegistryHTTPError, RegistryConnectionError):
        return [], None

    rows = _annotation_rows(child)
    if not rows:
        return [], None
    variant = f"/{pick.variant}" if pick.variant else ""
    return rows, f"{pick.architecture}{variant}/{pick.os}"


def _shell_context(request: Request, settings: object) -> dict[str, Any]:
    """Topbar / footer context shared by every page render.

    Drives the topbar (Sign-in vs. user pill, Sign-out button) and the
    trash-icon visibility on manifest panels. ``is_admin`` is derived
    from the current session ``Identity``, not from settings — so a
    logged-in admin sees the trash icon in ``admin`` mode but not in
    ``protected`` mode (where the provider hands back an empty role
    set even for the admin credential).
    """
    s = settings  # narrow typing — avoid importing Settings just for this
    identity = get_identity(request)
    return {
        "title": getattr(s, "title", "LayerLoupe"),
        "version": __version__,
        "registry_public_url": str(
            getattr(s, "registry_public_url", None) or getattr(s, "registry_url", "")
        ),
        "allow_registry_login": getattr(s, "allow_registry_login", False),
        "auth_mode": getattr(s, "auth_mode", "public"),
        "identity": identity,
        "is_admin": identity.is_admin,
        "session_username": request.session.get("registry_username")
        if hasattr(request, "session")
        else None,
        "ui_username": identity.username if not identity.is_anonymous else None,
    }


# -- Page routes (full HTML; serve deep links) ----------------------------


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
    q: str | None = Query(default=None),
) -> HTMLResponse:
    repos, error = await _fetch_repos(client, q)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **_shell_context(request, settings),
            "repos": repos,
            "repo_filter": q or "",
            "selected_repo": None,
            "tags": [],
            "tag_filter": "",
            "selected_tag": None,
            "manifest": None,
            "error": error,
        },
    )


@router.get("/repositories", response_class=HTMLResponse)
async def repositories_page(
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    identity: BrowseAccessDep,
    q: str | None = Query(default=None),
) -> HTMLResponse:
    """Same as ``/`` — explicit URL exists so links from the topbar work."""
    return await home(request, settings, client, identity, q)


@router.get("/repositories/{repository:path}/tags", response_class=HTMLResponse)
async def repository_page(
    repository: str,
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
    q: str | None = Query(default=None, description="Tag filter."),
    repo_q: str | None = Query(default=None, description="Repo column filter."),
) -> HTMLResponse:
    repos, repo_err = await _fetch_repos(client, repo_q)
    try:
        tags = await _fetch_tags(client, repository, q)
        tag_err = None
    except (RegistryHTTPError, RegistryConnectionError, RegistryError) as e:
        tags = []
        tag_err = f"Failed to load tags for {repository}: {e}"
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **_shell_context(request, settings),
            "repos": repos,
            "repo_filter": repo_q or "",
            "selected_repo": repository,
            "tags": tags,
            "tag_filter": q or "",
            "selected_tag": None,
            "manifest": None,
            "error": tag_err or repo_err,
        },
    )


@router.get(
    "/repositories/{repository:path}/manifests/{reference}",
    response_class=HTMLResponse,
)
async def manifest_page(
    repository: str,
    reference: str,
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
    repo_q: str | None = Query(default=None),
    tag_q: str | None = Query(default=None),
    platform: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the full shell with the manifest panel populated.

    ``?platform=<digest>`` selects a child manifest of a multi-arch index
    while keeping ``reference`` (the tag) in the URL. The tag list stays
    highlighted on the parent and the panel shows a back-to-index link.
    """
    repos, repo_err = await _fetch_repos(client, repo_q)
    error = repo_err
    tags: list[str] = []
    manifest: UnifiedManifest | None = None
    referrers: list[Referrer] = []
    parent_reference: str | None = None
    annotations_rows: list[AnnotationRow] = []
    annotations_fallback: str | None = None
    try:
        tags = await _fetch_tags(client, repository, tag_q)
        effective_ref = platform if platform else reference
        manifest = await _fetch_manifest(
            client,
            repository,
            effective_ref,
            str(settings.registry_public_url or settings.registry_url),
        )
        referrers = await _fetch_referrers(client, repository, manifest)
        if platform:
            parent_reference = reference
        annotations_rows, annotations_fallback = await _annotation_rows_with_fallback(
            client, repository, manifest
        )
    except (RegistryHTTPError, RegistryConnectionError, RegistryError) as e:
        error = f"Failed to load manifest {repository}:{reference}: {e}"
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            **_shell_context(request, settings),
            "repos": repos,
            "repo_filter": repo_q or "",
            "selected_repo": repository,
            "tags": tags,
            "tag_filter": tag_q or "",
            "selected_tag": reference,
            "manifest": manifest,
            "annotations_rows": annotations_rows,
            "annotations_fallback_source": annotations_fallback,
            "layer_rows": _layer_rows(manifest),
            "display_platforms": _display_platforms(manifest),
            "referrers": referrers,
            "parent_reference": parent_reference,
            "error": error,
        },
    )


# -- Fragment routes (htmx swaps) -----------------------------------------


@router.get("/partials/repositories", response_class=HTMLResponse)
async def repos_fragment(
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
    q: str | None = Query(default=None),
    selected_repo: str | None = Query(default=None),
) -> HTMLResponse:
    repos, _err = await _fetch_repos(client, q)
    return templates.TemplateResponse(
        request=request,
        name="partials/repo_list.html",
        context={
            "repos": repos,
            "selected_repo": selected_repo,
            "repo_filter": q or "",
            "registry_public_url": str(settings.registry_public_url or settings.registry_url),
        },
    )


@router.get("/partials/repositories/{repository:path}/tags", response_class=HTMLResponse)
async def tags_fragment(
    repository: str,
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
    q: str | None = Query(default=None),
    selected_tag: str | None = Query(default=None),
) -> Response:
    """Tag list fragment for a repo.

    Comes from two trigger sources:

    * The repo list (a fresh repo selection). We highlight the first tag
      and pre-fetch its manifest into ``#info-column-body`` via an OOB
      swap so the user lands on usable detail without a second click. We
      also push the URL forward to ``/repositories/<repo>/manifests/<tag>``
      for shareability.
    * The tag-filter input. The user is still inside the same repo —
      leave the manifest panel alone and just refresh the list.
    """
    try:
        tags = await _fetch_tags(client, repository, q)
        error = None
    except (RegistryHTTPError, RegistryConnectionError, RegistryError) as e:
        tags = []
        error = f"Failed to load tags: {e}"

    is_tag_filter = request.headers.get("HX-Trigger") == "tag-filter-input"
    auto_select = (not is_tag_filter) and bool(tags)

    auto_manifest: UnifiedManifest | None = None
    auto_referrers: list[Referrer] = []
    auto_tag: str | None = None
    auto_annotations_rows: list[AnnotationRow] = []
    auto_annotations_fallback: str | None = None
    if auto_select:
        auto_tag = tags[0]
        try:
            auto_manifest = await _fetch_manifest(
                client,
                repository,
                auto_tag,
                str(settings.registry_public_url or settings.registry_url),
            )
            auto_referrers = await _fetch_referrers(client, repository, auto_manifest)
            (
                auto_annotations_rows,
                auto_annotations_fallback,
            ) = await _annotation_rows_with_fallback(client, repository, auto_manifest)
        except (RegistryHTTPError, RegistryConnectionError, RegistryError):
            # Best-effort: fall back to an empty placeholder if the fetch
            # fails. The user can still click a tag manually.
            auto_manifest = None

    response = templates.TemplateResponse(
        request=request,
        name="partials/tag_list.html",
        context={
            "repository": repository,
            "tags": tags,
            "tag_filter": q or "",
            "selected_tag": auto_tag if auto_select else selected_tag,
            "error": error,
            # OOB swap controls — when ``not is_tag_filter`` we always emit
            # something into ``#info-column-body`` (a manifest, or an empty
            # placeholder if the fetch failed / the repo is empty).
            "clear_info_column": not is_tag_filter,
            "auto_manifest": auto_manifest,
            "auto_tag": auto_tag,
            "auto_annotations_rows": auto_annotations_rows,
            "auto_annotations_fallback_source": auto_annotations_fallback,
            "auto_layer_rows": _layer_rows(auto_manifest),
            "auto_display_platforms": _display_platforms(auto_manifest),
            "auto_referrers": auto_referrers,
            # Trash-icon visibility for the auto-selected manifest.
            "is_admin": get_identity(request).is_admin,
        },
    )
    if auto_select and auto_tag is not None:
        response.headers["HX-Push-Url"] = f"/repositories/{repository}/manifests/{auto_tag}"
    return response


@router.get(
    "/partials/repositories/{repository:path}/manifests/{reference}",
    response_class=HTMLResponse,
)
async def manifest_fragment(
    repository: str,
    reference: str,
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
    platform: str | None = Query(default=None),
) -> HTMLResponse:
    referrers: list[Referrer] = []
    parent_reference: str | None = None
    annotations_rows: list[AnnotationRow] = []
    annotations_fallback: str | None = None
    try:
        effective_ref = platform if platform else reference
        manifest: UnifiedManifest | None = await _fetch_manifest(
            client,
            repository,
            effective_ref,
            str(settings.registry_public_url or settings.registry_url),
        )
        referrers = await _fetch_referrers(client, repository, manifest)
        if platform:
            parent_reference = reference
        annotations_rows, annotations_fallback = await _annotation_rows_with_fallback(
            client, repository, manifest
        )
        error: str | None = None
    except (RegistryHTTPError, RegistryConnectionError, RegistryError) as e:
        manifest = None
        error = f"Failed to load manifest: {e}"
    return templates.TemplateResponse(
        request=request,
        name="partials/manifest_info.html",
        context={
            "repository": repository,
            "reference": reference,
            "manifest": manifest,
            "annotations_rows": annotations_rows,
            "annotations_fallback_source": annotations_fallback,
            "layer_rows": _layer_rows(manifest),
            "display_platforms": _display_platforms(manifest),
            "referrers": referrers,
            "parent_reference": parent_reference,
            "is_admin": get_identity(request).is_admin,
            "error": error,
            # Tells the partial to OOB-swap the trash-icon into
            # ``#manifest-actions`` (in the Manifest column header). Only
            # set on fragment renders — full-page renders include the icon
            # inline via index.html, so an OOB swap there would duplicate.
            "swap_actions": True,
        },
    )


# -- Mutating routes (delete) ---------------------------------------------


@router.delete("/web/repositories/{repository:path}/manifests/{reference}")
async def delete_manifest_via_web(
    repository: str,
    reference: str,
    request: Request,
    settings: SettingsDep,
    client: RegistryClientDep,
    _identity: AdminDep,
) -> Response:
    """Delete a manifest from the htmx UI.

    On success returns ``204`` with ``HX-Redirect: /repositories/<repo>/tags``
    so htmx hard-navigates to the tag list — the deleted tag is already gone
    from the freshly-rendered page, no manual fragment juggling needed. An
    audit event ``manifest_deleted`` is emitted alongside (see :mod:`layerloupe.audit`).

    Gated by ``AdminDep`` — anonymous → 401, non-admin → 403. The UI
    doesn't surface the trash-icon for non-admin sessions either; this
    guard catches direct-htmx-DELETE attempts.
    """
    digest = await client.delete_manifest(repository, reference)
    log_manifest_deleted(
        request,
        repository=repository,
        reference=reference,
        digest=digest,
        audit_log_path=settings.audit_log_path,
    )
    return Response(
        status_code=204,
        headers={"HX-Redirect": f"/repositories/{repository}/tags"},
    )


# -- Login / logout (HTML form flow) --------------------------------------


def _safe_redirect(target: str | None) -> str:
    """Whitelist same-origin paths only — refuse external / protocol-relative URLs."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return "/"
    return target


def _login_options(settings: SettingsDep) -> tuple[bool, bool]:
    """Return ``(ui_login_enabled, registry_login_enabled)``.

    The login page picks its template branch from these two booleans:
    show the UI-identity form, the registry-creds form, both as tabs,
    or refuse with 403 when neither is on.
    """
    return settings.auth_mode != "public", settings.allow_registry_login


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    settings: SettingsDep,
    next: str | None = Query(default=None),
) -> HTMLResponse:
    """Render the login page, with whichever forms apply.

    With ``AUTH_MODE != public`` the UI identity form is available.
    With ``ALLOW_REGISTRY_LOGIN=true`` the registry credentials form is
    available. Either or both can be on at once; when both are off
    there's nothing to log in to, so the route 403s.
    """
    ui_enabled, registry_enabled = _login_options(settings)
    if not (ui_enabled or registry_enabled):
        raise HTTPException(
            status_code=403,
            detail=(
                "Login is not enabled (set AUTH_MODE=protected/admin or "
                "ALLOW_REGISTRY_LOGIN=true to enable a login form)."
            ),
        )
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            **_shell_context(request, settings),
            "next": _safe_redirect(next),
            "username": "",
            "ui_username": "",
            "error": None,
            "ui_error": None,
            "ui_login_enabled": ui_enabled,
            "registry_login_enabled": registry_enabled,
        },
    )


@router.post("/login")
async def login_submit(
    request: Request,
    settings: SettingsDep,
    username: str = Form(min_length=1),
    password: str = Form(min_length=1),
    next: str = Form(default="/"),
) -> Response:
    """Submit the **registry credentials** form (per-user upstream login).

    This is the legacy ``/login`` POST kept for the orthogonal per-user
    registry-credential feature. UI identity login lives at
    ``/web/auth/login`` and writes a different session key.
    """
    if not settings.allow_registry_login:
        raise HTTPException(
            status_code=403,
            detail="Per-user registry login is disabled.",
        )

    ok = await _verify_credentials(settings, username, password)
    ui_enabled, registry_enabled = _login_options(settings)
    if not ok:
        # Re-render the form so the user can fix their credentials without
        # losing the ``next`` target. Username preserved (password not, by
        # convention — make the user re-type to avoid stale auto-fill).
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=401,
            context={
                **_shell_context(request, settings),
                "next": _safe_redirect(next),
                "username": username,
                "ui_username": "",
                "error": "Invalid registry credentials.",
                "ui_error": None,
                "ui_login_enabled": ui_enabled,
                "registry_login_enabled": registry_enabled,
            },
        )

    request.session["registry_username"] = username
    request.session["registry_password_enc"] = encrypt_password(
        settings.session_secret.get_secret_value(),
        password,
    )
    # 303 See Other so a refresh on the destination doesn't re-POST.
    return RedirectResponse(url=_safe_redirect(next), status_code=303)


@router.post("/web/auth/login")
async def ui_login_submit(
    request: Request,
    settings: SettingsDep,
    provider: AuthProviderDep,
    username: str = Form(min_length=1),
    password: str = Form(min_length=1),
    next: str = Form(default="/"),
) -> Response:
    """Submit the **UI identity** form (logs the operator in to LayerLoupe).

    Writes the resulting ``Identity`` to ``session["identity"]``; the
    orthogonal ``session["registry_username"]`` / ``..._password_enc``
    keys are untouched so users with per-user registry creds keep
    them across UI login / logout.
    """
    if settings.auth_mode == "public" or provider is None:
        raise HTTPException(
            status_code=403,
            detail="UI login is not enabled (set AUTH_MODE=protected or admin to enable).",
        )

    identity = await provider.authenticate(username, password)
    ui_enabled, registry_enabled = _login_options(settings)
    if identity is None:
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            status_code=401,
            context={
                **_shell_context(request, settings),
                "next": _safe_redirect(next),
                "username": "",
                "ui_username": username,
                "error": None,
                "ui_error": "Invalid credentials.",
                "ui_login_enabled": ui_enabled,
                "registry_login_enabled": registry_enabled,
            },
        )

    request.session["identity"] = identity.to_session()
    return RedirectResponse(url=_safe_redirect(next), status_code=303)


@router.post("/web/auth/logout")
async def ui_logout(request: Request) -> Response:
    """Drop the UI identity, keep any registry creds in place."""
    request.session.pop("identity", None)
    return RedirectResponse(url="/", status_code=303)


@router.post("/web/logout")
async def web_logout(request: Request) -> Response:
    """Clear *all* session state — both UI identity and registry creds.

    Kept as the topbar's single "Sign out" target so users don't have
    to think about which of the two logins they're in.
    """
    request.session.clear()
    return RedirectResponse(url="/", status_code=303)
