# `public/` — Anonymous read-only browsing

The default LayerLoupe deployment: anyone who can reach the URL can
browse repositories, tags, and manifests, but no one can delete
anything. No authentication, no session state worth protecting.

## When to pick this

- Internal mirror or registry explorer that's only reachable from a
  trusted network anyway.
- Public showcase of an OSS image registry.
- "I just want to see what's in this registry from a browser" — the
  quickstart.

## Canonical example

For the `public/` scenario the **root `docker-compose.yml`** of this
repository *is* the canonical example. It runs in `AUTH_MODE=public` —
anonymous read-only browse, no delete. Bringing it up:

```bash
cd ../..             # back to the layerloupe/ project root
docker compose up
```

You'll get a registry on `:5000`, LayerLoupe on `:8080`, and a one-shot
seeder that mirrors a few public images so the UI has something to
show on first load. See the top-level [`README.md`](../../README.md)
for the full walkthrough.

If you need delete capability, use the [`admin/`](../admin/) example
instead.

## Why no compose file here

Two compose files saying the same thing would drift. The rule is:
*every scenario has one canonical home*. For `public/` that home is
the root of the project, so this directory is intentionally just a
pointer.
