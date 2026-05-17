"""Tests for keyboard navigation + cheat sheet overlay.

The actual key handling lives in vanilla JS, so these tests assert that
the wiring is in place: the JS file declares the right key bindings, the
cheat sheet markup is rendered server-side, and the topbar exposes a
trigger button. Behavior in a real browser is out of scope (would need
Playwright); the goal here is to catch regressions in the contract
between server-rendered HTML and the JS hotkey handler.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from layerloupe.main import app

# -- layerloupe.js declares the expected hotkey bindings --------------------


def test_keyboard_module_handles_filter_focus_hotkey() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    # Forward-slash focuses the filter.
    assert 'e.key === "/"' in js
    assert "focusFilter" in js


def test_keyboard_module_handles_arrow_navigation() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    # Both arrow keys + vim-style j/k aliases.
    assert "ArrowDown" in js
    assert "ArrowUp" in js
    assert '"j"' in js
    assert '"k"' in js
    assert "navigateList" in js


def test_keyboard_module_handles_escape() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    # Esc closes any open dialog and blurs editable fields.
    assert 'e.key === "Escape"' in js
    assert "dialog[open]" in js


def test_keyboard_module_opens_cheat_sheet_on_question_mark() -> None:
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert 'e.key === "?"' in js
    assert "hotkey-modal" in js


def test_keyboard_module_skips_when_typing_in_input() -> None:
    """Hotkeys must not steal keys while the user is typing in a filter."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert "isEditableTarget" in js
    # Esc has its own pre-editable branch above the early return.
    assert "if (editable) return" in js


def test_keyboard_module_ignores_modifier_combos() -> None:
    """Cmd+/ etc. must not collide with browser shortcuts."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert "altKey" in js or "ctrlKey" in js or "metaKey" in js


def test_keyboard_bindings_are_idempotent() -> None:
    """``data-bound``-style guard prevents double-binding after htmx swaps."""
    with TestClient(app) as client:
        js = client.get("/static/layerloupe.js").text
    assert "keyboardBound" in js


# -- Cheat sheet overlay markup ------------------------------------------


def test_cheat_sheet_dialog_present_on_every_page() -> None:
    """The ``<dialog id="hotkey-modal">`` is rendered by base.html - present
    on home, error pages, the login form, etc."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'id="hotkey-modal"' in body
    assert "Keyboard shortcuts" in body
    # All six hotkeys documented.
    assert ">/<" in body
    assert ">↑<" in body or ">k<" in body
    assert ">↓<" in body or ">j<" in body
    assert ">Enter<" in body
    assert ">Esc<" in body
    assert ">?<" in body


def test_cheat_sheet_uses_kbd_elements() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    # Semantic <kbd> markup, not just styled spans.
    assert "<kbd>" in body
    # Help text mentions Esc to leave the filter.
    assert "Esc" in body
    assert "filter" in body.lower()


def test_topbar_exposes_keyboard_shortcuts_button() -> None:
    """A clickable affordance for users who don't know the ``?`` hotkey yet."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'data-modal-open="hotkey-modal"' in body
    # Tooltip references the ``?`` hotkey for discoverability.
    assert "Keyboard shortcuts" in body


def test_cheat_sheet_dialog_uses_modal_class() -> None:
    """Reuses the existing ``.modal`` style + bindModal() open/close logic."""
    with TestClient(app) as client:
        body = client.get("/").text
    match = re.search(r'<dialog[^>]*id="hotkey-modal"[^>]*>', body)
    assert match is not None
    tag = match.group(0)
    assert "modal" in tag


def test_cheat_sheet_close_button_present() -> None:
    with TestClient(app) as client:
        body = client.get("/").text
    assert "data-modal-close" in body


# -- Filter inputs are addressable by the hotkey selector ----------------


def test_filter_inputs_carry_filter_input_class() -> None:
    """``focusFilter`` looks for ``input.filter-input`` - make sure that's true."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'class="filter-input"' in body


def test_repo_list_has_known_id() -> None:
    """``navigateList`` falls back to ``#repo-list`` when no list is focused."""
    with TestClient(app) as client:
        body = client.get("/").text
    assert 'id="repo-list"' in body


# -- CSS hooks -----------------------------------------------------------


def test_focus_visible_outline_styled() -> None:
    """Keyboard navigation must show a visible focus indicator."""
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert ":focus-visible" in css


def test_kbd_styling_present() -> None:
    with TestClient(app) as client:
        css = client.get("/static/layerloupe.css").text
    assert "kbd {" in css or "\nkbd {" in css
    assert ".hotkey-table" in css
