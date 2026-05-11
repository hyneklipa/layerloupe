# LayerLoupe

Modern, OCI-aware web GUI for Docker / OCI image registries.
Built for self-hosted registries — `registry:2`, Harbor, GitLab, Nexus,
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
- Drill into multi-arch indexes — pick a child manifest, walk into the
  config blob, see history and labels.
- Delete tags via the safe HEAD → DELETE flow with a digest fallback.
- Sign in to private registries (Basic + Bearer token).

## Configuration

| Variable            | Required | Notes                                         |
| ------------------- | -------- | --------------------------------------------- |
| `REGISTRY_URL`      | yes      | Registry base URL, e.g. `https://reg.acme.io` |
| `SESSION_SECRET`    | yes      | 32+ random bytes, base64                      |
| `REGISTRY_USERNAME` | no       | Static auth (skip for browser-side login)     |
| `REGISTRY_PASSWORD` | no       | Static auth                                   |
| `LOG_JSON`          | no       | `true` for structured JSON logs               |

## Image

- Multi-arch: `linux/amd64`, `linux/arm64`.
- Build provenance and SBOM attestations attached (per Docker Build).
- Source: https://github.com/hyneklipa/layerloupe
