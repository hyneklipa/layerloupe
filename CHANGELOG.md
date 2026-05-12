# Changelog

## Unreleased

### Breaking — UI access-control redesign

LayerLoupe's access model moved from a pair of orthogonal toggles
(`ALLOW_DELETE`, the never-implemented `UI_USERNAME` / `UI_PASSWORD`) to
a three-level `AUTH_MODE`:

* `public` (default) — anonymous read-only browse, no delete.
* `protected` — login required, still no delete.
* `admin` — login required, logged-in user can delete tags.

**Removed env vars:**

| Removed | Replacement |
|---|---|
| `ALLOW_DELETE=true` | `AUTH_MODE=admin` + `ADMIN_USERNAME` + `ADMIN_PASSWORD_HASH` |
| `UI_USERNAME` / `UI_PASSWORD` | `ADMIN_USERNAME` / `ADMIN_PASSWORD_HASH` (the old pair was unused) |

The removed knobs are now silently ignored (`extra="ignore"`) — old
`.env` files with them in place don't crash startup; they just have no
effect. The new knobs aren't a drop-in: you need to generate a bcrypt
hash, which is what `scripts/hash-password.py` is for.

**New env vars:**

* `AUTH_MODE` — selects the access mode.
* `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH` — admin identity (required
  when `AUTH_MODE != public`).
* `ADMIN_USERNAME_FILE`, `ADMIN_PASSWORD_FILE` — file-mount variants
  for Docker / Kubernetes secrets (file contents are plaintext;
  hashing happens at startup).
* `SESSION_SECRET_FILE` — file-mount variant of `SESSION_SECRET`.

**Migration cheatsheet:**

```diff
- ALLOW_DELETE=true
+ AUTH_MODE=admin
+ ADMIN_USERNAME=admin
+ ADMIN_PASSWORD_HASH=$2b$12$...    # uv run scripts/hash-password.py
```

Per-deploy templates ship under [`examples/`](examples/) — `public/`,
`protected/`, `admin/`, `admin-docker-secrets/`.

See [`_docs/06-ui-access-control-redesign.md`](../_docs/06-ui-access-control-redesign.md)
for the design rationale.
