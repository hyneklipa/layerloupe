# LayerLoupe

Modern, OCI-aware web GUI for Docker / OCI image registries.
Python, OCI manifests, multi-arch awareness, and built for
self-hosted registries.

> Status: **early development.** The application is partially
> functional (browsing repositories, checking manifests for all five
> variants).
> It contains known bugs, such as issues with deleting tags,
> logging into private registries, etc.

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

* **`registry`** on `:5000` - a plain `registry:2` instance with deletes
  enabled.
* **`layerloupe`** on `:8080` - built from the local `Dockerfile`.
* **`seed`** - a one-shot job that mirrors `alpine`, `busybox`, and
  `hello-world` into the registry so the UI has something to show.

Then open <http://localhost:8080>.

To wipe everything (including the registry's data volume):

```bash
docker compose down --volumes
```

---

## Configuration examples

Beyond the root `docker-compose.yml` (anonymous read-only browsing),
the [`examples/`](examples/) directory carries one runnable scenario
per common deployment shape - protected (login required), admin (login
+ delete), admin with Docker secrets, and more as the access-control
redesign lands. Start at [`examples/README.md`](examples/README.md) for
a rundown of which to pick.

---

## Configuration

LayerLoupe reads its config from environment variables. All variables are
optional - sensible defaults are tuned for "local registry on
`https://localhost:5000`".

### Connecting to your registry

| Variable | Default | What it does |
|---|---|---|
| `REGISTRY_URL` | `https://localhost:5000` | Full URL of the registry to browse. |
| `REGISTRY_PUBLIC_URL` | = `REGISTRY_URL` | URL shown in copy-able `docker pull` strings. Set this when LayerLoupe talks to the registry over an internal hostname but users pull from a public one. |
| `SSL_VERIFY` | `true` | Verify TLS certs. Set to `false` for self-signed dev registries. |

### Talking to the registry

| Variable | Default | What it does |
|---|---|---|
| `REGISTRY_USERNAME` | - | Global username (Basic auth + token-flow upstream). |
| `REGISTRY_PASSWORD` | - | Global password. |
| `ALLOW_REGISTRY_LOGIN` | `false` | Show a UI sign-in form so each user supplies their own upstream registry credentials. Their password is encrypted with Fernet before going into the session cookie. |

Bearer token auth (the one Docker Hub, GHCR, and Harbor use) works
out-of-the-box: LayerLoupe detects the `Www-Authenticate: Bearer …`
challenge, fetches a token (using your Basic creds upstream), caches it
by `(service, scope)`, and pre-attaches it on subsequent requests.

### UI access control

LayerLoupe has three access modes, set via `AUTH_MODE`:

| Mode | Anonymous browse | Login required | Delete |
|---|---|---|---|
| `public` (default) | yes | no | no |
| `protected` | no | yes | no |
| `admin` | no | yes | yes (admin role) |

| Variable | Default | What it does |
|---|---|---|
| `AUTH_MODE` | `public` | Selects the access mode (see table above). |
| `ADMIN_USERNAME` | - | Admin login name. Required when `AUTH_MODE != public`. |
| `ADMIN_PASSWORD_HASH` | - | Bcrypt hash of the admin password. Generate with `uv run scripts/hash-password.py`. |
| `ADMIN_USERNAME_FILE` / `ADMIN_PASSWORD_FILE` | - | `*_FILE` variants for Docker / K8s secrets - the file is read as plaintext (the file mount is the trust boundary). When both `_HASH` and `_FILE` are set the file value wins. |
| `AUDIT_LOG_PATH` | - | Optional JSONL audit file path. Each successful delete appends one line with actor, repo, reference, resolved digest, and timestamp. The same event always goes to stdout regardless. |

Plaintext `ADMIN_PASSWORD` in env is rejected at startup - env values
are visible via `docker inspect` / `ps auxe`. Use `ADMIN_PASSWORD_HASH`
(env, hashed) or `ADMIN_PASSWORD_FILE` (file, plaintext, sealed by the
mount).

Per-deploy runnable templates live in [`examples/`](examples/) -
`public/`, `protected/`, `admin/`, `admin-docker-secrets/`.

#### Bootstrapping the admin password

`scripts/hash-password.py` is a tiny helper that emits a bcrypt hash
you can paste into `ADMIN_PASSWORD_HASH=`. It has two modes:

```bash
# Interactive - password is read with getpass, never echoes or lands in shell history.
uv run scripts/hash-password.py
Password: ********
Confirm:  ********
$2b$12$abc...xyz
```

```bash
# Piped - for CI / provisioning scripts pulling from a secret manager.
echo -n "$(vault kv get -field=admin_password secret/layerloupe)" \
  | uv run scripts/hash-password.py
$2b$12$abc...xyz
```

The script reuses `layerloupe.auth.env_provider.hash_password`, so the
output format is exactly what the running app accepts. Empty input
fails with an error rather than producing a hash of `""`.

> **Heads-up for Docker Compose users:** bcrypt hashes contain `$`
> characters. Docker Compose's `.env` parser treats `$NAME` as a
> variable reference unless the value is **single-quoted**. Paste the
> hash like this:
>
> ```env
> ADMIN_PASSWORD_HASH='$2b$12$abc...xyz'
> ```
>
> Without the single quotes you'll see `WARN: The "..." variable is
> not set` lines and the hash arrives in the container truncated.
> Double quotes don't help - they still allow interpolation.

For `ADMIN_PASSWORD_FILE` (Docker / K8s secret mount) you do **not**
need this script - the file contains the plaintext password, and
LayerLoupe hashes it at startup. The helper is purely for the env-hash
path, and the `$`-interpolation gotcha doesn't apply there either.

> **Note on delete:** deleting a manifest only unlinks it. Layer blobs
> on the registry's disk persist until `registry garbage-collect`
> runs. The confirm modal warns about this; the audit log records the
> digest so the operator running GC can reconcile.

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

1. **`layerloupe.registry`** - async HTTP client to a Docker / OCI
   registry. Handles Basic + Bearer auth, paginates `_catalog` /
   `tags/list`, parses every modern manifest variant (Docker schema 1 +
   v2, OCI image, OCI image index, Docker manifest list), and exposes a
   single `UnifiedManifest` view-model the rest of the app consumes.
2. **`layerloupe.api`** - FastAPI router mounted at `/api/*`. Strict OpenAPI
   contract. Maps `RegistryHTTPError` → identical HTTP status,
   `RegistryConnectionError` → `503`, `RegistryError` → `502`.
3. **`layerloupe.web`** - Jinja2 + htmx server-rendered UI mounted at `/`.
   Three-column layout (repos / tags / manifest), multi-arch dropdown,
   tabbed manifest detail, copy-to-clipboard pull commands, confirm
   modal for deletes, keyboard shortcuts (`/`, `↑↓`, `?`).

The HTML routes degrade gracefully - every page renders via plain HTTP
GET on a hard reload, so deep links (`/repositories/foo/manifests/v1.2.3`)
work even if JavaScript fails. htmx is purely an enhancement.

---

## License

MIT - see [`LICENSE.md`](LICENSE.md).
