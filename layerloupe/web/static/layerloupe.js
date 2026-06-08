/* LayerLoupe UI shell - minimal vanilla JS.
 *
 * Six tiny widgets:
 *   1. Theme toggle (set in <head> pre-paint script, button toggles).
 *   2. Manifest info tabs (Configuration / Layers / Annotations / Referrers).
 *   3. Multi-arch platform picker → navigate to child manifest.
 *   4. Copy-to-clipboard buttons + transient "Copied!" toast.
 *   5. Native <dialog>-based confirm modal (open / close / outside-click).
 *   6. Keyboard shortcuts (/ to focus filter, ↑↓ to walk list, Enter to
 *      activate, Esc to close modals, ? for the cheat sheet overlay).
 *
 * Every binding is idempotent (guarded by ``data-bound``) and re-runs after
 * every htmx swap, so widgets that appear in newly-fetched fragments work
 * without the developer having to remember to wire them.
 */
{
  "use strict";

  const STORAGE_KEY = "layerloupe-theme";

  /* -- Theme toggle ------------------------------------------------- */
  const setTheme = (theme) => {
    document.documentElement.setAttribute("data-theme", theme);
    try {
      localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      /* private mode → in-memory only */
    }
  };
  const currentTheme = () =>
    document.documentElement.getAttribute("data-theme") || "light";

  const bindThemeToggle = () => {
    const btn = document.getElementById("theme-toggle");
    if (!btn || btn.dataset.bound) return;
    btn.dataset.bound = "1";
    btn.addEventListener("click", () => {
      setTheme(currentTheme() === "dark" ? "light" : "dark");
    });
  };

  /* -- Manifest info tabs ------------------------------------------- */
  const bindTabs = (scope) => {
    const root = scope || document;
    for (const tabsRoot of root.querySelectorAll("[data-tabs]")) {
      if (tabsRoot.dataset.bound) continue;
      tabsRoot.dataset.bound = "1";

      const buttons = tabsRoot.querySelectorAll("[data-tab-target]");
      const panels = tabsRoot.querySelectorAll("[data-tab-panel]");
      const activate = (target) => {
        for (const b of buttons) {
          const on = b.dataset.tabTarget === target;
          b.classList.toggle("tab-btn--active", on);
          b.setAttribute("aria-selected", on ? "true" : "false");
        }
        for (const p of panels) {
          const on = p.dataset.tabPanel === target;
          p.classList.toggle("tab-panel--active", on);
          if (on) p.removeAttribute("hidden");
          else p.setAttribute("hidden", "");
        }
      };
      for (const btn of buttons) {
        btn.addEventListener("click", () => activate(btn.dataset.tabTarget));
      }
    }
  };

  /* -- Copy-to-clipboard + toast ------------------------------------ */
  const TOAST_DURATION_MS = 1800;
  const COPIED_FLAG_MS = 1200;

  const ensureToastEl = () => {
    let toast = document.getElementById("layerloupe-toast");
    if (toast) return toast;
    toast = document.createElement("div");
    toast.id = "layerloupe-toast";
    toast.className = "toast";
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
    document.body.appendChild(toast);
    return toast;
  };

  let toastTimer = null;
  const showToast = (message) => {
    const toast = ensureToastEl();
    toast.textContent = message;
    toast.classList.add("toast--visible");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => {
      toast.classList.remove("toast--visible");
    }, TOAST_DURATION_MS);
  };

  const fallbackCopy = (text) => {
    /* Older browsers / non-HTTPS contexts where Clipboard API is gated. */
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "absolute";
    ta.style.left = "-9999px";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch {
      /* swallow */
    }
    document.body.removeChild(ta);
  };

  const copyText = async (text) => {
    if (navigator.clipboard && window.isSecureContext) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch {
        /* fall through */
      }
    }
    fallbackCopy(text);
  };

  const bindCopyButtons = (scope) => {
    const root = scope || document;
    for (const btn of root.querySelectorAll("[data-copy]")) {
      if (btn.dataset.bound) continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", async () => {
        const text = btn.getAttribute("data-copy") || "";
        if (!text) return;
        await copyText(text);
        showToast("Copied!");
        /* CSS swaps the icon based on this attribute - JS just toggles. */
        btn.setAttribute("data-copied", "");
        setTimeout(() => btn.removeAttribute("data-copied"), COPIED_FLAG_MS);
      });
    }
  };

  /* -- Confirm modal (<dialog>) -------------------------------------- */
  const bindModal = (scope) => {
    const root = scope || document;

    for (const btn of root.querySelectorAll("[data-modal-open]")) {
      if (btn.dataset.bound) continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => {
        const dlg = document.getElementById(btn.getAttribute("data-modal-open"));
        if (dlg?.showModal) dlg.showModal();
        else if (dlg) dlg.setAttribute("open", "");
        /* Focus the type-to-confirm input if the modal has one - saves
         * the user a click and signals the intent of the gate. */
        const confirmInput = dlg?.querySelector("[data-delete-confirm-input]");
        if (confirmInput) {
          confirmInput.value = "";
          confirmInput.focus();
          /* Keep the confirm button disabled across reopens. */
          const confirmBtn = dlg.querySelector("[data-delete-confirm-btn]");
          if (confirmBtn) confirmBtn.disabled = true;
        }
      });
    }

    for (const btn of root.querySelectorAll("[data-modal-close]")) {
      if (btn.dataset.bound) continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => {
        const dlg = btn.closest("dialog");
        if (dlg?.close) dlg.close();
        else dlg?.removeAttribute("open");
      });
    }

    /* Click-outside-to-close: <dialog> emits clicks at its bounding box
       even when content sits inside, so we compare event.target === dialog. */
    for (const dlg of root.querySelectorAll("dialog.modal")) {
      if (dlg.dataset.bound) continue;
      dlg.dataset.bound = "1";
      dlg.addEventListener("click", (e) => {
        if (e.target === dlg && dlg.close) dlg.close();
      });
    }

    /* -- Type-to-confirm gate on delete modal ------------------------
     * The confirm button stays disabled until the user types the
     * expected ``<repo>:<tag>`` (or ``<repo>@<digest>``) string. The
     * gate is a deliberate friction step - the icon trigger is small
     * by design and the typed-name barrier eliminates "muscle memory"
     * deletions of the wrong manifest. */
    for (const input of root.querySelectorAll("[data-delete-confirm-input]")) {
      if (input.dataset.bound) continue;
      input.dataset.bound = "1";
      const dlg = input.closest("dialog");
      const btn = dlg?.querySelector("[data-delete-confirm-btn]");
      const expected = input.getAttribute("data-delete-confirm-expected") || "";
      if (!btn) continue;
      const update = () => {
        btn.disabled = input.value !== expected;
      };
      input.addEventListener("input", update);
      input.addEventListener("keydown", (e) => {
        /* Enter on a matched input fires the delete (avoids requiring a
         * mouse jump to the button). When unmatched, swallow Enter so
         * the form-default doesn't submit anything. */
        if (e.key !== "Enter") return;
        e.preventDefault();
        if (!btn.disabled) btn.click();
      });
    }
  };

  /* -- Filter clear (×) button --------------------------------------- */
  /* Clears the sibling filter input and re-fires the htmx fetch so the list
   * resets. htmx listens for ``search`` on the input (see hx-trigger), so a
   * dispatched ``search`` event drives the same code path as the native
   * clear affordance. Visibility of the button is pure CSS (:placeholder-shown). */
  const bindFilterClear = (scope) => {
    const root = scope || document;
    for (const btn of root.querySelectorAll(".filter-clear")) {
      if (btn.dataset.bound) continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => {
        const input = btn.parentElement?.querySelector(".filter-input");
        if (!input) return;
        if (input.value === "") return;
        input.value = "";
        input.focus();
        input.dispatchEvent(new Event("search", { bubbles: true }));
      });
    }
  };

  /* -- Account menu (avatar dropdown) -------------------------------- */
  const closeUserMenu = (menu) => {
    if (!menu) return;
    menu.hidden = true;
    menu
      .closest(".user-menu-wrap")
      ?.querySelector("[data-user-menu-toggle]")
      ?.setAttribute("aria-expanded", "false");
  };

  const bindUserMenu = (scope) => {
    const root = scope || document;
    for (const toggle of root.querySelectorAll("[data-user-menu-toggle]")) {
      if (toggle.dataset.bound) continue;
      toggle.dataset.bound = "1";
      const wrap = toggle.closest(".user-menu-wrap");
      const menu = wrap?.querySelector("[data-user-menu]");
      if (!menu) continue;
      toggle.addEventListener("click", (e) => {
        e.stopPropagation();
        if (menu.hidden) {
          menu.hidden = false;
          toggle.setAttribute("aria-expanded", "true");
        } else {
          closeUserMenu(menu);
        }
      });
      /* Keep the menu open while toggling theme; close it for anything that
         navigates or opens a dialog (shortcuts, sign in / out). */
      menu.addEventListener("click", (e) => {
        if (e.target.closest("#theme-toggle")) return;
        if (e.target.closest("a, button")) closeUserMenu(menu);
      });
    }
    /* One document-level handler closes any open menu on an outside click. */
    if (!document.body.dataset.userMenuBound) {
      document.body.dataset.userMenuBound = "1";
      document.addEventListener("click", (e) => {
        for (const menu of document.querySelectorAll("[data-user-menu]:not([hidden])")) {
          const wrap = menu.closest(".user-menu-wrap");
          if (wrap && !wrap.contains(e.target)) closeUserMenu(menu);
        }
      });
    }
  };

  /* -- Search stub --------------------------------------------------- */
  /* No command palette yet - clicking the top-bar search focuses the current
     column filter (same effect as ⌘K / "/"). */
  const bindSearchStub = (scope) => {
    const root = scope || document;
    for (const btn of root.querySelectorAll("[data-search-stub]")) {
      if (btn.dataset.bound) continue;
      btn.dataset.bound = "1";
      btn.addEventListener("click", () => focusFilter());
    }
  };

  /* -- Lazy "Load more" on column scroll ----------------------------- */
  /* Each column scrolls on its own, so htmx's window-based ``revealed`` is
     unreliable here. Instead, when a column is scrolled near its bottom we
     click its visible "Load more" button (htmx then swaps in the next page).
     The ``.htmx-request`` guard prevents firing again while one is in flight. */
  const bindLazyLoad = (scope) => {
    const root = scope || document;
    for (const body of root.querySelectorAll(".col-body")) {
      if (body.dataset.lazyBound) continue;
      body.dataset.lazyBound = "1";
      body.addEventListener("scroll", () => {
        if (body.scrollHeight - body.scrollTop - body.clientHeight > 200) return;
        const btn = body.querySelector(".ll-more-btn");
        if (btn && !btn.classList.contains("htmx-request")) btn.click();
      });
    }
  };

  /* -- Keyboard shortcuts -------------------------------------------- */
  const isEditableTarget = (el) => {
    if (!el) return false;
    const tag = el.tagName;
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
    return !!el.isContentEditable;
  };

  const focusFilter = () => {
    /* Prefer the filter for the currently selected column, else fall back
       to the repo filter. ``input.filter-input`` matches both. */
    const inputs = document.querySelectorAll("input.filter-input");
    if (!inputs.length) return false;
    const active = document.activeElement;
    const col = active?.closest?.(".col");
    if (col) {
      const localInput = col.querySelector("input.filter-input");
      if (localInput) {
        localInput.focus();
        localInput.select();
        return true;
      }
    }
    inputs[0].focus();
    inputs[0].select();
    return true;
  };

  const visibleListAnchors = (list) => [...list.querySelectorAll("a")];

  const navigateList = (direction) => {
    const current = document.activeElement;
    let list = current?.closest?.(".item-list") ?? null;
    if (!list) {
      /* No list focused - start in the repo column. */
      list = document.getElementById("repo-list");
    }
    if (!list) return false;
    const items = visibleListAnchors(list);
    if (!items.length) return false;
    const idx = items.indexOf(current);
    const next =
      idx === -1
        ? direction > 0
          ? items[0]
          : items[items.length - 1]
        : items[(idx + direction + items.length) % items.length];
    next.focus();
    next.scrollIntoView?.({ block: "nearest" });
    return true;
  };

  const bindKeyboardShortcuts = () => {
    if (document.body.dataset.keyboardBound) return;
    document.body.dataset.keyboardBound = "1";

    document.addEventListener("keydown", (e) => {
      const target = e.target;
      const editable = isEditableTarget(target);

      if (e.key === "Escape") {
        const openDialog = document.querySelector("dialog[open]");
        if (openDialog?.close) {
          openDialog.close();
          e.preventDefault();
          return;
        }
        const openMenu = document.querySelector("[data-user-menu]:not([hidden])");
        if (openMenu) {
          closeUserMenu(openMenu);
          e.preventDefault();
          return;
        }
        if (editable) {
          target.blur();
          e.preventDefault();
        }
        return;
      }

      /* ⌘K / Ctrl-K: search. No command palette yet, so focus the current
         column filter - works even while typing in another field. */
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        if (focusFilter()) e.preventDefault();
        return;
      }

      /* Don't hijack typing - but only AFTER we've handled Esc / ⌘K above. */
      if (editable) return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;

      if (e.key === "/") {
        if (focusFilter()) e.preventDefault();
        return;
      }

      if (e.key === "t" || e.key === "T") {
        setTheme(currentTheme() === "dark" ? "light" : "dark");
        e.preventDefault();
        return;
      }

      /* y / p: copy the current manifest's digest / pull command. We just
         click the matching copy button so the toast + copied state fire too. */
      if (e.key === "y" || e.key === "Y") {
        const btn = document.querySelector(".digest-section [data-copy]");
        if (btn) {
          btn.click();
          e.preventDefault();
        }
        return;
      }
      if (e.key === "p" || e.key === "P") {
        const btn =
          document.querySelector(".ll-pull.is-primary [data-copy]") ||
          document.querySelector(".pull-block [data-copy]");
        if (btn) {
          btn.click();
          e.preventDefault();
        }
        return;
      }

      if (e.key === "?") {
        const dlg = document.getElementById("hotkey-modal");
        if (dlg?.showModal && !dlg.hasAttribute("open")) {
          dlg.showModal();
          e.preventDefault();
        }
        return;
      }

      if (e.key === "ArrowDown" || e.key === "Down" || e.key === "j") {
        if (navigateList(1)) e.preventDefault();
        return;
      }

      if (e.key === "ArrowUp" || e.key === "Up" || e.key === "k") {
        if (navigateList(-1)) e.preventDefault();
      }
      /* Enter / Space: native browser behavior on a focused <a> already
         activates the link, including htmx attributes - no override needed. */
    });
  };

  /* -- Active-row tracking ------------------------------------------- */
  /* htmx only swaps the click target's content (the tag list, the info
     panel) - it doesn't re-render the source list, so without help the
     ``item-row--active`` class stays on whatever item was active when
     the column was last server-rendered. We move it on click instead. */
  const bindActiveRowTracking = () => {
    if (document.body.dataset.activeBound) return;
    document.body.dataset.activeBound = "1";
    document.body.addEventListener("click", (e) => {
      const link = e.target.closest?.(".item-list a");
      if (!link) return;
      const list = link.closest(".item-list");
      if (!list) return;
      const clickedRow = link.closest(".item-row");
      for (const row of list.querySelectorAll(".item-row")) {
        const on = row === clickedRow;
        row.classList.toggle("item-row--active", on);
        row.setAttribute("aria-selected", on ? "true" : "false");
      }
    });
  };

  /* -- Boot + re-bind on htmx swap ----------------------------------- */
  const init = (scope) => {
    bindThemeToggle();
    bindTabs(scope);
    bindCopyButtons(scope);
    bindModal(scope);
    bindFilterClear(scope);
    bindUserMenu(scope);
    bindSearchStub(scope);
    bindLazyLoad(scope);
    bindKeyboardShortcuts();
    bindActiveRowTracking();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => init());
  } else {
    init();
  }

  /* htmx replaces fragments after page load; re-bind widgets in the new
   * DOM. We re-init the *entire document* (not just ``e.target``) because
   * for innerHTML OOB swaps htmx 2.x fires ``htmx:afterSwap`` /
   * ``htmx:oobAfterSwap`` on the **main** swap target - its ``i.elts``
   * stays at ``[mainTarget]`` since the innerHTML swap path (``Ve()``)
   * doesn't repopulate elts with the new children. A scoped
   * ``init(e.target)`` therefore never reaches OOB-swapped elements like
   * the trash icon in the col-head, leaving them unbound. Bindings are
   * idempotent (``data-bound`` flag), so a full-document re-init on
   * every swap is correct and cheap. */
  document.body.addEventListener("htmx:afterSwap", () => init());

  /* Scroll the page back to the top when the user picks a different
   * repository. The page itself scrolls (no nested scrollbars), so a
   * click deep in a long repo list otherwise leaves the manifest column
   * out of view. ``#tag-column-body`` is only the swap target on a fresh
   * repo selection - tag-list filtering targets ``#tag-list``, manifest
   * fetches target ``#info-column-body``, so this won't fire for those. */
  document.body.addEventListener("htmx:afterSwap", (e) => {
    if (e.target && e.target.id === "tag-column-body") {
      window.scrollTo({ top: 0, behavior: "smooth" });
    }
  });
}
