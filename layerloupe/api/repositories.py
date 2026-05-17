"""Catalog and tag-list endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel

from layerloupe.config import SettingsDep
from layerloupe.deps import BrowseAccessDep, RegistryClientDep
from layerloupe.utils import sort_tags

router = APIRouter(prefix="/api/repositories", tags=["repositories"])


class RepositoryList(BaseModel):
    items: list[str]
    total: int


class TagList(BaseModel):
    repository: str
    items: list[str]
    total: int


@router.get("", response_model=RepositoryList)
async def list_repositories(
    client: RegistryClientDep,
    settings: SettingsDep,
    _identity: BrowseAccessDep,
    q: str | None = Query(default=None, description="Case-insensitive substring filter."),
    limit: int = Query(default=500, ge=1, le=10_000),
) -> RepositoryList:
    """Stream-iterate the registry's catalog and return up to ``limit`` matches.

    For very large registries (50k+ repos) this should eventually become
    server-sent events / NDJSON; for MVP a flat list with a hard limit is
    fine and matches the UX of the htmx three-column layout.
    """
    items: list[str] = []
    async for repo in client.iter_repositories(query=q, page_size=settings.page_size):
        items.append(repo)
        if len(items) >= limit:
            break
    return RepositoryList(items=items, total=len(items))


@router.get("/{repository:path}/tags", response_model=TagList)
async def list_tags(
    repository: str,
    client: RegistryClientDep,
    settings: SettingsDep,
    _identity: BrowseAccessDep,
    q: str | None = Query(default=None, description="Case-insensitive substring filter."),
    limit: int = Query(default=2_000, ge=1, le=10_000),
) -> TagList:
    """Return the repository's tags, smart-sorted (latest first, semver desc)."""
    items: list[str] = []
    async for tag in client.iter_tags(repository, query=q, page_size=settings.page_size):
        items.append(tag)
        if len(items) >= limit:
            break
    return TagList(repository=repository, items=sort_tags(items), total=len(items))
