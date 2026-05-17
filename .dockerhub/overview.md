# LayerLoupe

Modern, OCI-aware web GUI for Docker / OCI image registries.
Built for self-hosted registries - `registry:2`, Harbor, GitLab, Nexus,
or anything that speaks the OCI Distribution spec.

![License](https://img.shields.io/badge/license-MIT-blue)
![Multi-arch](https://img.shields.io/badge/arch-amd64%20%7C%20arm64-green)

> Status: **early development.** The application is partially
> functional (browsing repositories, checking manifests for all five
> variants).
> It contains known bugs, such as issues with deleting tags,
> logging into private registries, etc.

## Quick start

```bash
docker run --rm \
  -p 8080:8080 \
  -e REGISTRY_URL=https://registry.example.com \
  -e SESSION_SECRET="$(openssl rand -base64 32)" \
  hyneklipa/layerloupe:latest
```

Open http://localhost:8080 and sign in with your registry credentials.

## What it does

- Browse repositories and tags with semver-aware sort.
- Inspect manifests across all five variants (OCI image / index, Docker
  v2 / list, schema 1).
- Drill into multi-arch indexes - pick a child manifest, walk into the
  config blob, see history and labels.
- Delete tags via the safe HEAD → DELETE flow with a digest fallback.
- Sign in to private registries (Basic + Bearer token).
- Three UI access modes via `AUTH_MODE`: anonymous browse (`public`),
  login-gated browse (`protected`), or login + admin-only delete
  (`admin`).

## Access modes

Pick one based on who reaches the UI:

| `AUTH_MODE` | Browse | Delete | Use case                                          |
| ----------- | ------ | ------ | ------------------------------------------------- |
| `public`    | anyone | nobody | Internal mirror, public registry explorer         |
| `protected` | login  | nobody | Publicly exposed, no destructive capability       |
| `admin`     | login  | admin  | Private instance for maintainers                  |

`protected` and `admin` need an admin identity. Generate the bcrypt
hash once:

```bash
docker run --rm -it hyneklipa/layerloupe:latest \
  uv run scripts/hash-password.py
```

Then run with the hash:

```bash
docker run --rm -p 8080:8080 \
  -e REGISTRY_URL=https://registry.example.com \
  -e SESSION_SECRET="$(openssl rand -base64 32)" \
  -e AUTH_MODE=admin \
  -e ADMIN_USERNAME=admin \
  -e ADMIN_PASSWORD_HASH='$2b$12$...' \
  hyneklipa/layerloupe:latest
```

For Docker / Kubernetes secrets use the `*_FILE` variants
(`ADMIN_PASSWORD_FILE`, `SESSION_SECRET_FILE`); contents are read as
plaintext, the file mount is the trust boundary.

## Configuration

| Variable                                   | Required        | Notes                                                                  |
| ------------------------------------------ | --------------- | ---------------------------------------------------------------------- |
| `REGISTRY_URL`                             | yes             | Registry base URL, e.g. `https://reg.acme.io`                          |
| `SESSION_SECRET`                           | yes             | 32+ random bytes, base64                                               |
| `AUTH_MODE`                                | no              | `public` (default) / `protected` / `admin`                             |
| `ADMIN_USERNAME`, `ADMIN_PASSWORD_HASH`    | if not `public` | Bcrypt hash via `scripts/hash-password.py`                             |
| `ADMIN_USERNAME_FILE`, `ADMIN_PASSWORD_FILE`, `SESSION_SECRET_FILE` | no | `*_FILE` variants for Docker / K8s secrets (file mount is the boundary) |
| `REGISTRY_USERNAME`, `REGISTRY_PASSWORD`   | no              | Static upstream auth (skip if users sign in via UI)                    |
| `LOG_JSON`                                 | no              | `true` for structured JSON logs                                        |

Full env reference and deploy templates (`public/`, `protected/`,
`admin/`, `admin-docker-secrets/`) live in the
[GitHub repo](https://github.com/hyneklipa/layerloupe).

## Image

- Multi-arch: `linux/amd64`, `linux/arm64`.
- Build provenance and SBOM attestations attached (per Docker Build).
- Source: https://github.com/hyneklipa/layerloupe
