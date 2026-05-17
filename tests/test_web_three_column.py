"""Tests for the three-column layout, htmx fragments, deep links."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import MediaType, RegistryClient
from tests.conftest import load_fixture_bytes


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


@pytest.fixture
def web_handler() -> Iterator[dict[str, Callable[[httpx.Request], httpx.Response]]]:
    """Slot test fills with a registry handler - wired into the dependency.

    Default handler serves a small fixture: 4 repos, the manifest_oci fixture
    for tag listings + manifest fetches. Tests can swap the handler or
    augment via setting ``box["handler"]`` before issuing requests.
    """
    manifest_bytes = load_fixture_bytes("manifest_oci")
    config_bytes = load_fixture_bytes("image_config")
    manifest_digest = _digest_of(manifest_bytes)

    def default_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(
                200, json={"repositories": ["alpine", "library/ubuntu", "node", "redis"]}
            )
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "x", "tags": ["latest", "1.0", "1.1"]})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": manifest_digest,
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=config_bytes)
        return httpx.Response(404)

    box: dict[str, Callable[[httpx.Request], httpx.Response]] = {"handler": default_handler}

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


# -- Page routes (full HTML) ----------------------------------------------


def test_home_renders_three_columns_with_repos(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/")
    assert response.status_code == 200
    body = response.text
    # All three columns present.
    assert ">Repositories<" in body
    assert ">Tags<" in body
    assert ">Manifest<" in body
    # Repo list populated.
    assert "alpine" in body
    assert "library/ubuntu" in body
    # Tags column shows placeholder hint, not tag list.
    assert "Select a repository to see its tags." in body
    # Info column shows placeholder.
    assert "Select a tag to see its manifest details." in body


def test_repository_page_loads_tags_for_selected_repo(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/repositories/library/ubuntu/tags")
    assert response.status_code == 200
    body = response.text
    # Repo list still rendered (column 1).
    assert "library/ubuntu" in body
    # Tags rendered (smart-sorted: latest first).
    assert ">latest<" in body
    assert ">1.1<" in body
    # Info column still placeholder.
    assert "Select a tag to see its manifest details." in body


def test_manifest_page_renders_full_state(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/repositories/library/ubuntu/manifests/latest")
    assert response.status_code == 200
    body = response.text
    # All three columns populated.
    assert "library/ubuntu" in body
    assert ">latest<" in body
    # Manifest info is rendered (pull command appears).
    assert "docker pull" in body
    assert "library/ubuntu:latest" in body
    # Layers section is rendered inline (list view, no tabs).
    assert "layers-section" in body


def test_repo_filter_propagates_via_query_param(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/", params={"q": "ubuntu"})
    body = response.text
    # Filter input echoes the query.
    assert 'value="ubuntu"' in body


# -- Fragment routes ------------------------------------------------------


def test_repos_fragment_returns_just_the_list(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/partials/repositories")
    assert response.status_code == 200
    body = response.text
    # Fragment, not full page - no <!DOCTYPE>.
    assert "<!DOCTYPE" not in body
    assert 'id="repo-list"' in body
    assert "alpine" in body
    # No topbar etc.
    assert 'class="topbar"' not in body


def test_repos_fragment_filters(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/partials/repositories", params={"q": "ubuntu"})
    body = response.text
    assert "library/ubuntu" in body
    assert "alpine" not in body
    assert "redis" not in body


def test_tags_fragment_returns_just_tag_list(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/partials/repositories/foo/tags")
    body = response.text
    assert "<!DOCTYPE" not in body
    assert 'id="tag-list"' in body
    # Smart-sorted: latest comes first.
    latest_pos = body.find(">latest<")
    one_zero_pos = body.find(">1.0<")
    assert 0 <= latest_pos < one_zero_pos


def test_manifest_fragment_returns_just_info_panel(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        response = client.get("/partials/repositories/foo/manifests/latest")
    body = response.text
    assert "<!DOCTYPE" not in body
    assert 'class="manifest-info"' in body or 'id="manifest-info"' in body
    assert "docker pull" in body
    assert "foo:latest" in body


# -- htmx wiring on filter input + clicks ---------------------------------


def test_filter_input_has_htmx_attributes(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """The repo filter must have hx-get + delay-debounced trigger."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'hx-get="/partials/repositories"' in body
    assert "delay:300ms" in body
    assert 'hx-target="#repo-list"' in body


def test_repo_link_has_hx_push_url(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    # Each repo link should push the deep-link URL.
    assert "hx-push-url=" in body
    assert 'href="/repositories/alpine/tags"' in body


def test_repo_selection_scrolls_window_to_top() -> None:
    """Clicking a repo deep in a long list should snap the page back to the
    top so the manifest column is visible without manual scrolling. The
    handler lives in layerloupe.js and gates on the swap target id, so it
    only fires for repo clicks (not tag-filter or tag-click swaps)."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert 'e.target.id === "tag-column-body"' in js
    assert "window.scrollTo" in js


def test_empty_state_has_filter_slot_for_oob_swap(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """The Tags column needs a persistent ``#tag-filter-slot`` (and
    ``#tag-count``) even when no repo is selected, so the tag-list fragment
    can OOB-swap the filter input in on the first repo click. Without this,
    the user has to reload the page before the filter appears."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'id="tag-filter-slot"' in body
    assert 'id="tag-count"' in body
    # No filter input yet - the slot is empty until a repo is picked.
    assert 'id="tag-filter-input"' not in body


def test_tags_fragment_oob_swaps_filter_input_on_fresh_repo(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """When the user picks a repo (htmx hits the tags fragment without the
    HX-Trigger=tag-filter-input header), the response must include OOB
    swaps that fill in ``#tag-filter-slot`` and ``#tag-count``."""
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/tags").text
    assert 'id="tag-filter-slot"' in body
    assert 'hx-swap-oob="innerHTML"' in body
    assert 'id="tag-filter-input"' in body
    assert 'id="tag-count"' in body


def test_tags_fragment_skips_oob_swaps_on_filter_trigger(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """When the trigger is the filter input itself, we must NOT OOB-swap
    the input (would clobber the user's focus and value mid-typing)."""
    with TestClient(app) as client:
        response = client.get(
            "/partials/repositories/foo/tags",
            headers={"HX-Trigger": "tag-filter-input"},
            params={"q": "lat"},
        )
    body = response.text
    # The OOB filter slot wrapper must be absent on the filter trigger.
    assert 'id="tag-filter-slot"' not in body
    # But the tag list is still re-rendered.
    assert 'id="tag-list"' in body


# -- Manifest tabs --------------------------------------------------------


def test_image_manifest_renders_overview_and_layers_tabs(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Layers can dwarf the rest of the panel - image manifests split into
    Overview (default, with config + annotations + signatures) and Layers."""
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    # Tab container + buttons.
    assert "data-tabs" in body
    assert 'data-tab-target="overview"' in body
    assert 'data-tab-target="layers"' in body
    # Overview is the default-active tab.
    assert 'tab-btn tab-btn--active"\n' in body or 'tab-btn tab-btn--active" ' in body
    # The Layers panel exists and starts hidden.
    assert 'data-tab-panel="layers"' in body
    # Configuration and Layers sections both still render in the markup
    # (just gated by tabs in CSS / hidden attribute).
    assert "config-section" in body
    assert "layers-section" in body


def test_index_manifest_renders_without_tabs(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """Multi-arch index manifests have no layers - wrapping just
    Annotations in tabs would be visual noise."""
    import hashlib

    from layerloupe.registry import MediaType
    from tests.conftest import load_fixture_bytes

    manifest_bytes = load_fixture_bytes("manifest_index")
    digest = "sha256:" + hashlib.sha256(manifest_bytes).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": MediaType.OCI_IMAGE_INDEX.value,
                    "docker-content-digest": digest,
                },
            )
        return httpx.Response(404)

    web_handler["handler"] = handler

    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    # No tabs on index manifests.
    assert "data-tabs" not in body
    assert 'data-tab-target="layers"' not in body
    # Annotations section still renders directly.
    assert "annotations-section" in body


def test_css_includes_tab_styles() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert ".tab-btn" in css
    assert ".tab-panel" in css
    assert ".tab-btn--active" in css


def test_tag_link_has_manifest_target(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        body = client.get("/repositories/foo/tags").text
    assert 'hx-target="#info-column-body"' in body
    assert 'href="/repositories/foo/manifests/latest"' in body


# -- Selection state in HTML reflects URL ---------------------------------


def test_selected_repo_has_active_class(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        body = client.get("/repositories/library/ubuntu/tags").text
    # The matching repo row should be marked active.
    assert "item-row--active" in body
    # And specifically on library/ubuntu, not on alpine.
    import re

    active_rows = re.findall(r"<li[^>]*item-row--active[^>]*>.*?</li>", body, re.DOTALL)
    assert len(active_rows) >= 1
    assert any("library/ubuntu" in row for row in active_rows)


def test_selected_tag_has_active_class(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    with TestClient(app) as client:
        body = client.get("/repositories/foo/manifests/latest").text
    assert "item-row--active" in body


# -- Deep linking ---------------------------------------------------------


def test_deep_link_to_manifest_shows_info_panel(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """A bare GET on the manifest URL renders all three columns server-side.

    This is the "permalink works without JS / on hard reload" guarantee.
    """
    with TestClient(app) as client:
        body = client.get("/repositories/library/ubuntu/manifests/latest").text
    # Repo list, tag list, and manifest info all present in one response.
    assert "alpine" in body  # repos column
    assert ">latest<" in body  # tags column
    assert "docker pull" in body  # info column
    # And no JS was needed to get here - htmx hadn't run yet.


def test_deep_link_with_404_repo_renders_error(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    def handler_404_tags(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["alpine"]})
        return httpx.Response(404, json={"errors": [{"code": "NAME_UNKNOWN"}]})

    web_handler["handler"] = handler_404_tags

    with TestClient(app) as client:
        response = client.get("/repositories/missing/tags")
    # Page still renders (200), with an error banner.
    assert response.status_code == 200
    assert "error-banner" in response.text or "Failed to load" in response.text


# -- htmx static asset mount ----------------------------------------------


def test_htmx_script_served() -> None:
    with TestClient(app) as client:
        response = client.get("/static/htmx.min.js")
    assert response.status_code == 200
    assert "htmx" in response.text.lower()


def test_base_html_includes_htmx_script() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    assert "/static/htmx.min.js" in body


# -- Registry unreachable → home page still renders -----------------------


def test_home_renders_when_registry_unreachable(
    web_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
) -> None:
    """A flaky registry shouldn't 503 the whole UI."""

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated outage")

    web_handler["handler"] = boom

    with TestClient(app) as client:
        response = client.get("/")
    # Shell still renders (200).
    assert response.status_code == 200
    body = response.text
    assert ">Repositories<" in body
    assert "error-banner" in body or "Could not load" in body
