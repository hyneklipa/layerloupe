"""Tests for the delete button + confirm modal + DELETE endpoint."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator

import httpx
import pytest
from fastapi.testclient import TestClient

from layerloupe.auth.env_provider import hash_password
from layerloupe.config import get_settings
from layerloupe.deps import get_registry_client
from layerloupe.main import app
from layerloupe.registry import MediaType, RegistryClient
from tests.conftest import load_fixture_bytes

_ADMIN_PASSWORD = "admin-pw"
# Module-level hash so we pay the bcrypt cost once for the file.
_ADMIN_PASSWORD_HASH = hash_password(_ADMIN_PASSWORD, rounds=4)


def _login_admin(client: TestClient) -> None:
    """Log into the test client as the admin user from ``allow_delete``.

    Tests that mutate state (DELETE) or check admin-only UI (trash
    icon, modal) need a logged-in admin in the new access-control
    model — the ``ALLOW_DELETE`` flag is gone.
    """
    r = client.post(
        "/api/auth/ui-login",
        json={"username": "test-admin", "password": _ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text


def _digest_of(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _make_handler(
    *,
    on_delete: Callable[[httpx.Request], httpx.Response] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Default handler that serves manifest_oci + accepts deletes."""
    manifest_bytes = load_fixture_bytes("manifest_oci")
    config_bytes = load_fixture_bytes("image_config")
    digest = _digest_of(manifest_bytes)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE" and on_delete is not None:
            return on_delete(request)
        path = request.url.path
        if path == "/v2/_catalog":
            return httpx.Response(200, json={"repositories": ["foo"]})
        if path.endswith("/tags/list"):
            return httpx.Response(200, json={"name": "foo", "tags": ["latest"]})
        if request.method == "HEAD" and "/manifests/" in path:
            return httpx.Response(200, headers={"docker-content-digest": digest})
        if request.method == "DELETE" and "/manifests/" in path:
            return httpx.Response(202)  # default: success
        if "/manifests/" in path:
            return httpx.Response(
                200,
                content=manifest_bytes,
                headers={
                    "content-type": MediaType.OCI_IMAGE_MANIFEST.value,
                    "docker-content-digest": digest,
                },
            )
        if "/blobs/" in path:
            return httpx.Response(200, content=config_bytes)
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
    """Configure ``AUTH_MODE=admin`` so a logged-in admin can delete.

    Tests still need to log in (via ``_login_admin(client)``) after
    creating the ``TestClient`` — the env alone doesn't grant a
    session.
    """
    monkeypatch.setenv("AUTH_MODE", "admin")
    monkeypatch.setenv("ADMIN_USERNAME", "test-admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", _ADMIN_PASSWORD_HASH)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


@pytest.fixture
def disallow_delete(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Public mode + no login — no delete capability anywhere."""
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD_HASH", raising=False)
    get_settings.cache_clear()
    try:
        yield
    finally:
        get_settings.cache_clear()


# -- Button visibility gated by ``allow_delete`` --------------------------


def test_delete_button_hidden_when_allow_delete_false(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    disallow_delete: None,
) -> None:
    """The acceptance criterion: not just disabled — completely absent."""
    with TestClient(app) as client:
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "Delete this manifest" not in body
    assert "data-modal-open" not in body
    assert 'id="delete-modal"' not in body


def test_delete_button_visible_when_allow_delete_true(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "Delete this manifest" in body
    assert 'data-modal-open="delete-modal"' in body
    assert 'id="delete-modal"' in body


# -- Modal markup ---------------------------------------------------------


def test_modal_includes_garbage_collect_warning(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "garbage-collect" in body
    assert "Cancel" in body
    assert "Yes, delete" in body


def test_modal_delete_button_has_hx_delete(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    # The confirm button hits the web-layer DELETE route.
    assert 'hx-delete="/web/repositories/foo/manifests/latest"' in body


def test_modal_shows_resolved_digest(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """Operators want to see the digest before they confirm a delete."""
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "Resolves to digest" in body
    assert "sha256:" in body


# -- Web DELETE endpoint --------------------------------------------------


def test_web_delete_returns_204_with_hx_redirect(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    with TestClient(app) as client:
        _login_admin(client)
        response = client.delete("/web/repositories/foo/manifests/latest")
    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/repositories/foo/tags"


def test_web_delete_returns_403_when_disabled(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    disallow_delete: None,
) -> None:
    """Public mode + no admin configured → DELETE is gated 403 by
    ``require_admin`` before the route body runs."""
    with TestClient(app) as client:
        response = client.delete("/web/repositories/foo/manifests/latest")
    assert response.status_code == 403


def test_web_delete_propagates_404_from_registry(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """Already-deleted tag → registry 404 → API 404 (via global handler)."""

    def deny(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]})

    use_handler["handler"] = _make_handler(on_delete=deny)
    # The delete path goes HEAD → DELETE; both should 404 here.
    use_handler["handler"] = lambda r: httpx.Response(
        404, json={"errors": [{"code": "MANIFEST_UNKNOWN"}]}
    )

    with TestClient(app) as client:
        _login_admin(client)
        response = client.delete("/web/repositories/foo/manifests/missing")
    assert response.status_code == 404


def test_web_delete_calls_registry_with_resolved_digest(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """Verify the HEAD-then-DELETE pattern carries through."""
    seen: list[tuple[str, str]] = []
    digest = "sha256:" + "f" * 64

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"docker-content-digest": digest})
        if request.method == "DELETE":
            return httpx.Response(202)
        return httpx.Response(404)

    use_handler["handler"] = handler

    with TestClient(app) as client:
        _login_admin(client)
        response = client.delete("/web/repositories/foo/manifests/latest")
    assert response.status_code == 204

    methods = [m for m, _ in seen]
    assert methods[0] == "HEAD"  # resolves digest first
    assert seen[-1] == ("DELETE", f"/v2/foo/manifests/{digest}")


# -- layerloupe.js modal wiring ----------------------------------------------


def test_layerloupe_js_implements_modal() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert "data-modal-open" in js
    assert "data-modal-close" in js
    assert "showModal" in js
    # Outside-click-to-close handler.
    assert "e.target === dlg" in js or "event.target === dlg" in js
    assert "bindModal" in js


def test_css_includes_modal_styles() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert "dialog.modal" in css
    assert "::backdrop" in css
    assert "modal-warn" in css
    assert ".btn-danger" in css
    # Type-to-confirm gate styles.
    assert ".modal-confirm-input" in css
    assert ".modal-confirm-prompt" in css


# -- Trash icon trigger in the Manifest column header -------------------


def test_trash_icon_lives_in_manifest_column_header(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """The trigger is an icon button in the col-head, not a big red button
    at the bottom of the panel — so it doesn't visually invite clicks.
    The button must reach the modal (data-modal-open) and carry an
    accessible label since it has no visible text."""
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/repositories/foo/manifests/latest").text
    assert 'id="manifest-actions"' in body
    assert 'class="icon-btn icon-btn--danger"' in body
    assert 'data-modal-open="delete-modal"' in body
    assert 'aria-label="Delete this manifest"' in body
    # The old prominent footer button must be gone.
    assert "manifest-info-foot" not in body


def test_manifest_actions_slot_present_even_when_empty(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    disallow_delete: None,
) -> None:
    """The slot must exist as an OOB target even when delete is disabled
    (or no manifest is selected) — otherwise an htmx-loaded manifest with
    delete enabled has nowhere to inject the icon."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'id="manifest-actions"' in body
    # No icon content yet — the slot is empty.
    assert "icon-btn--danger" not in body


def test_manifest_fragment_oob_swaps_trash_icon(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """When htmx loads a manifest via /partials/.../manifests/<tag>, the
    response must include an OOB swap that fills #manifest-actions in the
    column header. Without this, clicking a tag from a no-manifest state
    would never show the trash icon."""
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert 'id="manifest-actions"' in body
    assert 'hx-swap-oob="innerHTML"' in body
    assert "icon-btn--danger" in body


# -- Type-to-confirm gate -----------------------------------------------


def test_modal_has_type_to_confirm_input(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """The confirm button is disabled until the user types the manifest's
    repo:tag (or repo@digest) string — prevents accidental clicks even
    after the icon-only trigger lowers initial attractiveness."""
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    # Input + expected-value attribute exist.
    assert "data-delete-confirm-input" in body
    assert 'data-delete-confirm-expected="foo:latest"' in body
    # Confirm button starts disabled and is gate-tagged.
    assert "data-delete-confirm-btn" in body
    # The "disabled" attribute (without value) is present — exact form
    # matters: htmx wouldn't fire DELETE on a disabled button.
    assert " disabled" in body


def test_modal_confirm_string_uses_at_for_digest_reference(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """Digest references display as ``repo@sha256:...`` not ``repo:sha256:...``;
    the type-to-confirm prompt must match the displayed format so the
    string the user sees is the string they have to type."""
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/sha256:" + "a" * 64).text
    assert "foo@sha256:" + "a" * 64 in body
    assert 'data-delete-confirm-expected="foo@sha256:' in body


def test_tags_fragment_emits_single_top_level_manifest_actions_oob(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """Regression: htmx 2.x's nested-OOB handling clones the outer wrapper's
    full subtree (with the inner OOB element still attached) into the live
    DOM before processing the inner OOB, leaving a duplicate icon. To avoid
    that, the trash icon must be OOB-swapped at the TOP level of tag_list
    — sibling of #info-column-body, not nested inside it."""
    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/tags").text
    # Exactly one #manifest-actions appears in the response (the top-level
    # OOB swap), and it sits OUTSIDE the #info-column-body OOB wrapper.
    assert body.count('id="manifest-actions"') == 1
    info_open = body.find('id="info-column-body"')
    info_close = body.find("</div>", info_open)
    actions_pos = body.find('id="manifest-actions"')
    # The actions div must come AFTER the info-column-body's closing div,
    # not nested inside it.
    assert actions_pos > info_close


def test_manifest_info_skips_oob_block_when_swap_actions_false(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """When manifest_info.html is included from tag_list.html (which wraps
    it in an OOB div), it must be rendered with swap_actions=False so it
    doesn't emit its own OOB block — that nested OOB caused the duplicate
    icon bug. The manifest_fragment route still uses swap_actions=True,
    where it's the top-level response and the OOB is safe."""
    with TestClient(app) as client:
        _login_admin(client)
        # tags_fragment includes manifest_info.html with swap_actions=False;
        # the top-level OOB is emitted by tag_list.html itself.
        tags_body = client.get("/partials/repositories/foo/tags").text
        # manifest_fragment renders manifest_info.html as the root — top-level
        # OOB block fires here, populating the col-head icon directly.
        manifest_body = client.get("/partials/repositories/foo/manifests/latest").text
    assert tags_body.count("hx-swap-oob") >= 3  # info-column-body + count + filter + actions
    # In the manifest fragment, exactly one #manifest-actions OOB at top level.
    assert manifest_body.count('id="manifest-actions"') == 1


def test_layerloupe_js_reinits_document_after_htmx_swap() -> None:
    """Regression: the trash icon OOB-swaps into the col-head, but for
    innerHTML OOB swaps htmx 2.x fires htmx:afterSwap with e.target =
    main swap target (not the OOB target). A scoped ``init(e.target)``
    therefore never reaches the icon and its modal-open click handler is
    never bound — so the icon appears but does nothing until full reload.
    The fix is to re-init the whole document on every swap; bindings are
    idempotent via the ``data-bound`` flag."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    # The afterSwap re-init must call init() with no scope (whole doc),
    # NOT init(e.target) which scopes only to the main swap target.
    assert 'addEventListener("htmx:afterSwap", () => init())' in js


def test_layerloupe_js_implements_type_to_confirm() -> None:
    """The JS gate compares input value to the expected string and
    toggles the confirm button's disabled state on every keystroke."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert "data-delete-confirm-input" in js
    assert "data-delete-confirm-btn" in js
    assert "data-delete-confirm-expected" in js
    # Auto-focus the input when the modal opens.
    assert "confirmInput.focus()" in js


# -- Index manifest (no separate config blob): delete still works --------


def test_delete_button_present_for_index_manifests(
    use_handler: dict[str, Callable[[httpx.Request], httpx.Response]],
    allow_delete: None,
) -> None:
    """Multi-arch indexes can be deleted too — tags column hits the head index."""
    manifest_bytes = load_fixture_bytes("manifest_index")
    digest = _digest_of(manifest_bytes)

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

    use_handler["handler"] = handler

    with TestClient(app) as client:
        _login_admin(client)
        body = client.get("/partials/repositories/foo/manifests/latest").text
    assert "Delete this manifest" in body
    assert 'id="delete-modal"' in body
