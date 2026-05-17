# `protected/` - Login required, read-only

Same UI as `public/`, but every request first bounces the visitor to
`/login`. After signing in they browse repositories / tags / manifests
exactly as in public mode - **no delete capability**. Use this when
you need to publish LayerLoupe but don't want unauthenticated eyes on
the contents.

## When to pick this

- Internal mirror that's accessible from a less-trusted network.
- Public registry explorer that you want crawlers and casual visitors
  to bounce off of.
- "Auditable read access" - every browse session is tied to a logged-in
  identity (visible in logs via the audit pipeline when destructive
  actions, if any, occur upstream).

## Quickstart

```bash
cp .env.example .env

# Generate a strong session secret (don't ship the placeholder)
python3 -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(32))" >> .env

# Generate a bcrypt hash for your admin password
cd ../..   # back to the layerloupe project root
uv run scripts/hash-password.py
#   Password: ********
#   $2b$12$....
# Paste that into ADMIN_PASSWORD_HASH= in your .env

cd examples/protected
docker compose up
```

Open <http://localhost:8080>. You'll be redirected to `/login`; sign in
with the username from `ADMIN_USERNAME` and the plaintext password
whose hash you put in `ADMIN_PASSWORD_HASH`.

## Configuration knobs

| Variable | Purpose |
|---|---|
| `AUTH_MODE=protected` | Activates the login requirement. |
| `ADMIN_USERNAME` | The admin login name (plaintext, not a secret). |
| `ADMIN_PASSWORD_HASH` | Bcrypt hash of the admin password (`$2b$...`). Generate with `scripts/hash-password.py`. |
| `SESSION_SECRET` | Signs session cookies. Set explicitly so sessions survive container restarts. |

## Using Docker / Kubernetes secrets instead

If you'd rather keep the password (or its hash) out of `.env`, use the
`*_FILE` variants - see [`../admin-docker-secrets/`](../admin-docker-secrets/)
for that pattern. The same `ADMIN_PASSWORD_FILE` knob works in
`protected` mode; only the role of the resulting identity differs
between `protected` and `admin`.

## Note: this scenario doesn't allow delete

In `protected` mode the auth provider grants the logged-in user an
empty role set - they're authenticated, but the `admin` role required
by `DELETE /api/.../manifests/...` isn't on their identity. The route
guards return 403 even for the admin account. If you also need
destructive operations, use the `admin/` example.
