# Changelog

## Unreleased

### Changed

- UI redesign groundwork (design tokens + typography). The CSS design-token
  layer moved to the blue `--ll-*` system (LayerLoupe logo blue `#0C7FE8`,
  cool-slate ink ramp, blue-grey page surface) from `theme-blue.css`, with
  matching dark overrides. The sans typeface switched from self-hosted IBM
  Plex Sans to self-hosted **Archivo** (weights 400/500/600/700); monospace
  now uses the system stack. No layout or behavior change yet - this is the
  visual base for the rest of the registry-viewer redesign.
- UI redesign: app shell + three-pane layout. The browser is now a
  full-height app - a 60px top bar over three columns (Repositories 256px,
  Tags 264px, Manifest fills the rest), each scrolling on its own instead of
  the whole page. The top bar gained a `registry › repo` breadcrumb and
  absorbed the keyboard-shortcuts and theme-toggle controls; the footer was
  removed (version label moved into the top bar). Filter inputs became search
  pills with a clear (×) button. Repository / tag rows restyled (mono, soft
  selected fill, accent rail). Narrow screens stack the columns and restore
  page scroll. No data-model or endpoint changes.
- UI redesign: account menu + top-bar controls. The two user pills (UI
  identity / registry credentials) collapsed into an **avatar dropdown** that
  shows the username(s), role badge, a **Dark mode** toggle, **Keyboard
  shortcuts**, and **Sign out / Sign in** - and is always present (it hosts
  theme + shortcuts even for anonymous browsing). The standalone theme toggle
  moved into this menu. Added a search-box stub (clicking it or **⌘K** focuses
  the current filter; full command palette is future work) and a **`t`**
  shortcut to toggle the theme. Avatar shows initials derived from the
  username, or a neutral glyph when anonymous. No endpoint changes (sign-in /
  sign-out targets unchanged).
- UI redesign: manifest detail. The detail pane now leads with a large
  `repo:tag` title (tag in brand-blue mono) and an at-a-glance fact row, with
  the admin delete icon moved inline next to the title (the old
  `#manifest-actions` column-header slot and its out-of-band swap are gone -
  the icon travels with the panel). Pull commands became bordered rows (a
  primary tag row with a solid Copy button + a secondary digest row); the full
  digest moved to its own quiet section. The multi-arch platform picker is now
  a responsive **card grid** (arch/os, child digest · size, hover lift) instead
  of pills - `UnifiedPlatform` gained a `size` field to back it. Annotations
  render as a single card of label-over-key / value rows. Section headings use
  a hairline rule. The Overview / Layers tabs and the type-to-confirm delete
  gate are unchanged.
- UI redesign: empty states + modals. The empty manifest pane is now a
  centered **empty-detail** block (rounded tile + layers glyph, adaptive
  heading/copy, a ⌘K hint) instead of a one-line placeholder. The keyboard
  shortcuts dialog became a two-column key grid with a header + close button;
  the delete dialog gained a head / body / foot shell with a danger-tinted
  icon (the type-to-confirm gate and GC warning are unchanged). Modals share a
  rounded 18px shell with the brand overlay/shadow.
- UI: paginated repo / tag lists + filter match highlighting. The lists now
  render 50 rows at a time with a **Load more** footer ("X of Y" / "All N
  shown"); the column scroll auto-loads the next page near the bottom. This is
  a pure in-memory slice of the already-cached registry listing - no extra
  registry round-trips. The active filter substring is **highlighted**
  (`<mark>`) in repo / tag names, rendered server-side. Added **`y`** (copy
  digest) and **`p`** (copy pull command) keyboard shortcuts. Column header
  counts now show the full total rather than the visible page size.
- UI redesign: login. The sign-in card was restyled to the design - decorative
  blurred blobs behind a rounded 20px card, the brand logo in-card, labelled
  fields, an informational read-only **Registry** field, and a full-width
  primary button. The dual sign-in surfaces (LayerLoupe identity + per-user
  registry credentials) and their endpoints are unchanged. "Continue with SSO"
  from the mockup is intentionally omitted until there's an SSO backend.
- UI redesign: polish. Honor `prefers-reduced-motion` (near-instant
  transitions, no smooth scroll). Narrow-screen refinements below the
  column-stacking breakpoint (tighter top bar / manifest padding, wrapping
  title, stacked annotation rows). Removed dead CSS left over from the
  redesign (`.container`, `.kv-list`, stale `.manifest-meta` reference).

## 0.2.1 - 2026-05-18

### Security

- Apply pending Debian security updates in the runtime image so the
  published container ships with current glibc instead of waiting for
  `docker-library/python` to rebake `python:3.14-slim`. Fixes
  CVE-2026-4046 and CVE-2026-4437 (glibc < `2.41-12+deb13u3`) flagged
  by Docker Scout.

## 0.2.0 - 2026-05-17

### Breaking - UI access-control redesign

LayerLoupe's access model moved from a pair of orthogonal toggles
(`ALLOW_DELETE`, the never-implemented `UI_USERNAME` / `UI_PASSWORD`) to
a three-level `AUTH_MODE`:

* `public` (default) - anonymous read-only browse, no delete.
* `protected` - login required, still no delete.
* `admin` - login required, logged-in user can delete tags.

**Removed env vars:**

| Removed | Replacement |
|---|---|
| `ALLOW_DELETE=true` | `AUTH_MODE=admin` + `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` |
| `UI_USERNAME` / `UI_PASSWORD` | `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` (the old pair was unused) |

The removed knobs are now silently ignored (`extra="ignore"`) - old
`.env` files with them in place don't crash startup; they just have no
effect. The new knobs aren't a drop-in: you need to generate a bcrypt
hash, which is what `scripts/hash-password.py` is for.

**New env vars:**

* `AUTH_MODE` - selects the access mode.
* `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` - admin identity (required
  when `AUTH_MODE != public`).
* `ADMIN_USERNAME_FILE`, `ADMIN_PASSWORD_FILE` - file-mount variants
  for Docker / Kubernetes secrets (file contents are plaintext;
  hashing happens at startup).
* `SESSION_SECRET_FILE` - file-mount variant of `SESSION_SECRET`.

**Migration cheatsheet:**

```diff
- ALLOW_DELETE=true
+ AUTH_MODE=admin
+ ADMIN_USERNAME=admin
+ ADMIN_PASSWORD_HASH=$2b$12$...    # uv run scripts/hash-password.py
```

Per-deploy templates ship under [`examples/`](examples/) - `public/`,
`protected/`, `admin/`, `admin-docker-secrets/`.


### Sessions invalidate when `AUTH_MODE` changes

UI sessions now carry the `AUTH_MODE` they were minted under. Flipping
the mode in env (e.g. `protected` → `admin` after onboarding a
maintainer) invalidates all existing UI sessions on the next request -
users land on `/login` and re-authenticate, picking up the role-set
the new mode grants. Without this, a session minted under `protected`
would keep its empty role-set forever and the trash icon would stay
hidden even after the operator enabled `admin` mode.

The invalidation is one-shot per deploy: existing sessions from before
this release are missing the `auth_mode` field and will also be
rejected, forcing a single re-login. Equivalent to (a much lower-cost
than) a `SESSION_SECRET` rotation.

Registry-credential sessions (`session["registry_username"]` /
`..._password_enc`) are unaffected - they live under different keys
and have their own lifecycle.
