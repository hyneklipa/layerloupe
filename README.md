# LayerLoupe

Modern, OCI-aware web GUI for Docker / OCI image registries.
Python, OCI manifests, multi-arch awareness, and built for
self-hosted registries.

> Status: **early development.** The app is functional end-to-end (browse
> repositories, inspect manifests across all five variants, delete tags,
> sign in to private registries) — what's left is polish, deployment
> templates, and an end-to-end test suite.

---

## Quick start (Docker Compose)

Five-minute path: bring up LayerLoupe in front of a fresh local registry,
seeded with a handful of public images.

```bash
git clone <this-repo>
cd layerloupe
docker compose up
```

That's it. Three services start:

* **`registry`** on `:5000` — a plain `registry:2` instance with deletes
  enabled.
* **`layerloupe`** on `:8080` — built from the local `Dockerfile`.
* **`seed`** — a one-shot job that mirrors `alpine`, `busybox`, and
  `hello-world` into the registry so the UI has something to show.

Then open <http://localhost:8080>.

To wipe everything (including the registry's data volume):

```bash
docker compose down --volumes
```

---

## Screenshots

> Screenshots and a demo GIF are coming once the UI polish settles. For
> now: a three-column layout (repositories / tags / manifest detail)
> with a multi-arch picker, OCI annotations, copy-able pull commands,
> and a dark mode that doesn't make your retinas sting.

---

## Configuration

LayerLoupe reads its config from environment variables. All variables are
optional — sensible defaults are tuned for "local registry on
`https://localhost:5000`".

### Connecting to your registry

| Variable | Default | What it does |
|---|---|---|
| `REGISTRY_URL` | `https://localhost:5000` | Full URL of the registry to browse. |
| `REGISTRY_PUBLIC_URL` | = `REGISTRY_URL` | URL shown in copy-able `docker pull` strings. Set this when LayerLoupe talks to the registry over an internal hostname but users pull from a public one. |
| `SSL_VERIFY` | `true` | Verify TLS certs. Set to `false` for self-signed dev registries. |

### Authenticating

| Variable | Default | What it does |
|---|---|---|
| `REGISTRY_USERNAME` | — | Global username (Basic auth + token-flow upstream). |
| `REGISTRY_PASSWORD` | — | Global password. |
| `ALLOW_REGISTRY_LOGIN` | `false` | Show the UI sign-in form so each user supplies their own credentials. Their password is encrypted with Fernet before going into the session cookie. |
| `UI_USERNAME` / `UI_PASSWORD` | — | Optional HTTP Basic auth in front of the LayerLoupe UI itself. |

Bearer token auth (the one Docker Hub, GHCR, and Harbor use) works
out-of-the-box: LayerLoupe detects the `Www-Authenticate: Bearer …`
challenge, fetches a token (using your Basic creds upstream), caches it
by `(service, scope)`, and pre-attaches it on subsequent requests.

### Destructive operations

| Variable | Default | What it does |
|---|---|---|
| `ALLOW_DELETE` | `false` | Show the "Delete this manifest" button. The registry must also have `REGISTRY_STORAGE_DELETE_ENABLED=true`. |
| `AUDIT_LOG_PATH` | — | Optional JSONL audit file path. Each successful delete appends one line with actor, repo, reference, and resolved digest. The same event always goes to stdout regardless. |

> **Note:** deleting a manifest only unlinks it. Layer blobs on the
> registry's disk persist until `registry garbage-collect` runs. The
> confirm modal warns about this; the audit log records the digest so
> the operator running GC can reconcile.

### Branding & sessions

| Variable | Default | What it does |
|---|---|---|
| `TITLE` | `LayerLoupe` | Shown in the topbar / browser title. |
| `SESSION_SECRET` | randomly generated | Signs (and, for credentials, encrypts) the session cookie. **Set this in production** so sessions survive restarts and so the rotation isn't accidental. |

### Logging

| Variable | Default | What it does |
|---|---|---|
| `LOG_LEVEL` | `info` | `debug` / `info` / `warning` / `error`. |
| `LOG_JSON` | `false` | Emit one JSON object per log line (recommended in production for log shippers). |

### Performance

| Variable | Default | What it does |
|---|---|---|
| `CACHE_TTL` | `30` | Seconds to cache catalog / tag / manifest responses. Set to `0` to disable. |
| `PAGE_SIZE` | `100` | `?n=` page size sent to `_catalog` and `tags/list`. |

A complete `.env.example` is checked in.

---

## Production deployment

Build the image:

```bash
docker build -t layerloupe:0.1.0 .
```

Run it:

```bash
docker run --rm \
  -p 8080:8080 \
  -e REGISTRY_URL=https://registry.example.com \
  -e REGISTRY_USERNAME=svc-layerloupe \
  -e REGISTRY_PASSWORD="$(cat /run/secrets/registry_password)" \
  -e SESSION_SECRET="$(openssl rand -base64 32)" \
  -e LOG_JSON=true \
  layerloupe:0.1.0
```

The image:

* Runs as non-root (`uid=1001`).
* Exposes port `8080`.
* Defaults to JSON logs.
* Carries a `HEALTHCHECK` against `/api/healthz` so Docker / Kubernetes /
  Compose all see liveness without extra config.
* Weighs in at ~56 MB single-platform.

`/api/healthz` and `/api/readyz` are intended for Kubernetes liveness /
readiness probes respectively. Both carry `Cache-Control: no-store` and
are filtered out of LayerLoupe's own structured access log so they don't
drown stdout.

---

## Development

Requires [`uv`](https://github.com/astral-sh/uv) (≥ 0.11) and Python ≥ 3.13.

```bash
uv sync
uv run uvicorn layerloupe.main:app --reload
```

The full developer cycle:

```bash
uv run ruff check .          # lint
uv run ruff format .         # format (use --check in CI)
uv run mypy layerloupe       # type-check (strict)
uv run pytest                # ~600 tests, runs in ~10s
uv run pre-commit install    # one-time, then hooks run on git commit
```

### Project layout

```
layerloupe/
├-- pyproject.toml             # uv + ruff + mypy + pytest config
├-- Dockerfile                 # multi-stage: deps → runtime
├-- docker-compose.yml         # registry + layerloupe + seed
├-- .env.example               # documented env knobs
├-- layerloupe/                   # Python package
│   ├-- main.py                # FastAPI app + lifespan + exception handlers
│   ├-- config.py              # pydantic-settings
│   ├-- deps.py                # FastAPI dependencies (registry client, sessions)
│   ├-- audit.py               # delete-action audit trail
│   ├-- sessions.py            # Fernet password encryption
│   ├-- logging.py             # structlog + request middleware
│   ├-- api/                   # JSON REST API
│   ├-- web/                   # Jinja2 + htmx HTML UI
│   ├-- registry/              # async httpx client + parsers
│   │   ├-- client.py          # the httpx wrapper
│   │   ├-- auth.py            # Basic
│   │   ├-- bearer.py          # Bearer token flow + cache
│   │   ├-- cache.py           # TTL cache
│   │   ├-- manifests.py       # media-type classifier
│   │   ├-- models.py          # Pydantic manifest models
│   │   ├-- parser.py          # unified view-model
│   │   ├-- annotations.py     # OCI image-spec annotations
│   │   ├-- layers.py          # Dockerfile-instruction parser
│   │   └-- referrers.py       # OCI 1.1 referrers
│   └-- utils/
│       ├-- humanize.py        # "1.5 MB", "3 weeks ago"
│       └-- version_sort.py    # latest-first, semver-aware tag sort
└-- tests/                     # mirrors layerloupe/, plus fixtures/
```

### Architecture overview

Three layers, end to end:

1. **`layerloupe.registry`** — async HTTP client to a Docker / OCI
   registry. Handles Basic + Bearer auth, paginates `_catalog` /
   `tags/list`, parses every modern manifest variant (Docker schema 1 +
   v2, OCI image, OCI image index, Docker manifest list), and exposes a
   single `UnifiedManifest` view-model the rest of the app consumes.
2. **`layerloupe.api`** — FastAPI router mounted at `/api/*`. Strict OpenAPI
   contract. Maps `RegistryHTTPError` → identical HTTP status,
   `RegistryConnectionError` → `503`, `RegistryError` → `502`.
3. **`layerloupe.web`** — Jinja2 + htmx server-rendered UI mounted at `/`.
   Three-column layout (repos / tags / manifest), multi-arch dropdown,
   tabbed manifest detail, copy-to-clipboard pull commands, confirm
   modal for deletes, keyboard shortcuts (`/`, `↑↓`, `?`).

The HTML routes degrade gracefully — every page renders via plain HTTP
GET on a hard reload, so deep links (`/repositories/foo/manifests/v1.2.3`)
work even if JavaScript fails. htmx is purely an enhancement.

---

## License

MIT — see [`LICENSE.md`](LICENSE.md).
