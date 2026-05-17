# `admin/` - Login required, admin can delete

The full-power deployment: anonymous visitors get bounced to `/login`,
and after signing in the admin gets the read-only browse plus a
trash-icon on every manifest detail. Every delete writes one line to
the audit JSONL file.

## When to pick this

- Private LayerLoupe instance for the registry maintainer.
- "I want to clean up old tags from the UI" workflow.
- Any deployment where deletes are part of the operational flow.

## Quickstart

```bash
cp .env.example .env

# Generate a strong session secret
python3 -c "import secrets; print('SESSION_SECRET=' + secrets.token_urlsafe(32))" >> .env

# Generate a bcrypt hash for your admin password
cd ../..   # back to the layerloupe project root
uv run scripts/hash-password.py
#   Password: ********
#   $2b$12$....
# Paste the hash into ADMIN_PASSWORD_HASH= in your .env

cd examples/admin
docker compose up
```

Open <http://localhost:8080>. You'll be redirected to `/login`. After
signing in with `ADMIN_USERNAME` and the plaintext password whose hash
went into `ADMIN_PASSWORD_HASH`, the trash icon appears in the manifest
column header for every selected tag.

## Audit log

`AUDIT_LOG_PATH` is set so each successful delete appends one JSONL
record:

```json
{"event":"manifest_deleted","timestamp":"2026-05-12T10:30:00+00:00",
 "actor":"admin","ip":"192.0.2.1","repository":"library/alpine",
 "reference":"3.20","digest":"sha256:..."}
```

The `audit-log` named volume keeps the file across container
restarts. Tail it with:

```bash
docker compose exec layerloupe tail -f /var/log/layerloupe/audit.log
```

## Configuration knobs

| Variable | Purpose |
|---|---|
| `AUTH_MODE=admin` | Activates the UI surface for delete - admin login, trash icon, auth guard. |
| `REGISTRY_STORAGE_DELETE_ENABLED=true` | Tells the registry itself to actually perform the unlink. The two flags are a pair: with only one of them on, the delete either never reaches the registry (no UI to trigger it) or the registry refuses it (`MANIFEST_DELETE_DISABLED`). |
| `ADMIN_USERNAME` | Admin login name. |
| `ADMIN_PASSWORD_HASH` | Bcrypt hash of the admin password. |
| `SESSION_SECRET` | Signs session cookies. |
| `AUDIT_LOG_PATH` | Where to append delete events as JSONL. |

## Want the password out of `.env`?

See [`../admin-docker-secrets/`](../admin-docker-secrets/) - same
scenario, but `ADMIN_PASSWORD_FILE` reads the password from a sealed
secret mount instead of a hash in env.

## Registry garbage collection

Deleting a manifest only unlinks it - the layer blobs persist on the
registry's disk until `registry garbage-collect` runs. The audit log
captures the resolved digest so the operator running GC can reconcile.
