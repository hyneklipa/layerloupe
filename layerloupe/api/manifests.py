"""Manifest detail / config / referrers / delete endpoints."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from layerloupe.audit import log_manifest_deleted
from layerloupe.config import SettingsDep
from layerloupe.deps import AdminDep, BrowseAccessDep, RegistryClientDep
from layerloupe.registry import (
    ImageConfig,
    ManifestKind,
    Referrer,
    RegistryError,
    RegistryHTTPError,
    UnifiedManifest,
    to_unified,
)

router = APIRouter(prefix="/api/repositories", tags=["manifests"])


class DeleteResult(BaseModel):
    digest: str


class ReferrerOut(BaseModel):
    """One referrer row in the JSON API.

    Mirrors :class:`layerloupe.registry.Referrer` but as a Pydantic model so
    it gets a stable OpenAPI schema (the dataclass would too, but we want
    explicit field aliasing in case of future reshaping).
    """

    digest: str
    media_type: str
    size: int
    artifact_type: str | None
    kind: str
    label: str
    annotations: dict[str, str]


class ReferrersResult(BaseModel):
    items: list[ReferrerOut]
    total: int


def _to_referrer_out(r: Referrer) -> ReferrerOut:
    return ReferrerOut(
        digest=r.digest,
        media_type=r.media_type,
        size=r.size,
        artifact_type=r.artifact_type,
        kind=r.kind,
        label=r.label,
        annotations=dict(r.annotations),
    )


def _build_pull_command(public_url: str, repository: str, reference: str) -> str:
    """Compose ``docker pull <host>/<repo>:<tag>`` (or ``@<digest>``).

    ``public_url`` is normalized: scheme + (optional) port stripped of
    trailing slash. The URL the user actually pastes into a terminal
    shouldn't include ``https://``.
    """
    host = public_url.rstrip("/")
    for prefix in ("https://", "http://"):
        if host.startswith(prefix):
            host = host[len(prefix) :]
            break
    sep = "@" if reference.startswith("sha256:") else ":"
    return f"docker pull {host}/{repository}{sep}{reference}"


@router.get(
    "/{repository:path}/manifests/{reference}",
    response_model=UnifiedManifest,
)
async def get_manifest(
    repository: str,
    reference: str,
    client: RegistryClientDep,
    settings: SettingsDep,
    _identity: BrowseAccessDep,
) -> UnifiedManifest:
    """Fetch a manifest, attach the image config (when applicable), and unify.

    For multi-arch indexes we don't follow into a child manifest here - the
    UI presents a platform picker and the next request includes the chosen
    digest as ``reference``.
    """
    manifest = await client.get_manifest(repository, reference)

    image_config: ImageConfig | None = None
    # Only single-arch image manifests carry a separate config blob worth fetching.
    if manifest.kind in (ManifestKind.OCI_IMAGE, ManifestKind.DOCKER_V2):
        try:
            image_config = await client.get_image_config(repository, manifest)
        except (RegistryError, RegistryHTTPError):
            # Config fetch is best-effort - UI can still render manifest-level data.
            image_config = None

    public_url = str(settings.registry_public_url or settings.registry_url)
    pull_command: str | None = _build_pull_command(public_url, repository, reference)
    pull_command_digest: str | None = None
    if manifest.digest is not None:
        pull_command_digest = _build_pull_command(public_url, repository, manifest.digest)
        if pull_command_digest == pull_command:
            pull_command = None
    return to_unified(
        manifest,
        image_config,
        pull_command=pull_command,
        pull_command_digest=pull_command_digest,
    )


@router.get(
    "/{repository:path}/manifests/{reference}/config",
    response_model=ImageConfig,
)
async def get_manifest_config(
    repository: str,
    reference: str,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
) -> ImageConfig:
    """Fetch just the image config blob - useful for power users / debugging."""
    manifest = await client.get_manifest(repository, reference)
    try:
        return await client.get_image_config(repository, manifest)
    except RegistryError as e:
        # Index / schema 1 manifests have no separate config blob - caller error.
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get(
    "/{repository:path}/manifests/{reference}/referrers",
    response_model=ReferrersResult,
)
async def get_manifest_referrers(
    repository: str,
    reference: str,
    client: RegistryClientDep,
    _identity: BrowseAccessDep,
) -> ReferrersResult:
    """OCI 1.1 referrers API - signatures, SBOMs, attestations attached to a manifest.

    Resolves a tag reference to a digest (referrers API requires a digest).
    Returns an empty list when the registry doesn't implement the endpoint
    (HTTP 404 / 405 / 501) so the UI can hide its panel without erroring.
    Each item is classified by ``artifact_type`` / ``mediaType`` into one
    of ``signature`` / ``sbom`` / ``attestation`` / ``other`` for the UI's
    icon and label.
    """
    digest = reference
    if not reference.startswith("sha256:"):
        head_resp = await client.head(f"/v2/{repository}/manifests/{reference}")
        digest = head_resp.headers.get("docker-content-digest", reference)

    referrers = await client.get_referrers(repository, digest)
    items = [_to_referrer_out(r) for r in referrers]
    return ReferrersResult(items=items, total=len(items))


@router.delete(
    "/{repository:path}/manifests/{reference}",
    response_model=DeleteResult,
)
async def delete_manifest(
    repository: str,
    reference: str,
    request: Request,
    client: RegistryClientDep,
    settings: SettingsDep,
    _identity: AdminDep,
) -> DeleteResult:
    """Delete a manifest by digest (resolving from a tag if needed).

    Gated by ``AdminDep`` - the dependency raises ``401`` for anonymous
    callers and ``403`` for authenticated-but-not-admin ones before this
    handler runs. The UI doesn't show the button for non-admin sessions
    either; this guard is defense in depth against direct API hits.

    On success an audit event ``manifest_deleted`` is emitted with
    actor, repository, reference, and resolved digest.
    ``AUDIT_LOG_PATH`` additionally appends the same record to a JSONL
    file.

    Note: the registry's storage GC must run for layer blobs to
    actually free disk. The UI surfaces this caveat in the confirm
    dialog.
    """
    digest = await client.delete_manifest(repository, reference)
    log_manifest_deleted(
        request,
        repository=repository,
        reference=reference,
        digest=digest,
        audit_log_path=settings.audit_log_path,
    )
    return DeleteResult(digest=digest)
