# `admin-docker-secrets/` — Admin mode with file-mounted secrets

Same access model as [`../admin/`](../admin/) (login required, delete
granted to the admin role), but the admin password and session secret
are sourced from **file mounts** instead of env vars.

## When to pick this

- Deployments where the orchestrator delivers secrets as files: Docker
  Swarm secrets, Kubernetes `Secret` volume mounts, Vault Agent
  Injector writing to `/vault/secrets/...`.
- Anywhere you don't want secrets in `docker inspect` output or in a
  `.env` file checked into source control by mistake.

## Quickstart

The `secrets/` directory is bind-mounted into the container at
`/run/secrets/` (read-only). For this example you populate the files
locally; for real deployments your orchestrator does it.

```bash
# Plaintext password — the file IS the secret storage.
printf 'my-strong-password' > secrets/admin_password

# Session secret (any high-entropy string).
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/session_secret

docker compose up
```

Open <http://localhost:8080>. Sign in with username `admin` (the
non-secret default — change it in `docker-compose.yml`) and the
password you wrote to `secrets/admin_password`.

The files in `secrets/` are git-ignored (see `secrets/.gitignore`)
so you can't accidentally commit a real password. Two `.example`
files document the expected format.

## Why plaintext in the file?

The file mount is the trust boundary — secrets-management platforms
(Docker Swarm secrets, K8s Secret volumes, Vault) deliver plaintext
into a sealed channel, the same way `POSTGRES_PASSWORD_FILE` and
friends work. Asking the operator to bcrypt-hash the password
*outside* the secret store would be friction without security benefit;
LayerLoupe hashes it at startup so the in-memory representation is a
hash either way.

For env-sourced passwords (`ADMIN_PASSWORD_HASH`) the rules are
different — env is leaky (`docker inspect`, `ps auxe`, deploy logs),
so plaintext there is rejected at startup.

## Real-world: Docker Swarm

```yaml
# Swarm-native secrets (recommended over bind mounts in prod).
secrets:
  admin_password:
    external: true
  session_secret:
    external: true

services:
  layerloupe:
    secrets:
      - admin_password
      - session_secret
    environment:
      ADMIN_PASSWORD_FILE: /run/secrets/admin_password
      SESSION_SECRET_FILE: /run/secrets/session_secret
```

```bash
echo -n 'my-password' | docker secret create admin_password -
openssl rand -base64 32 | docker secret create session_secret -
docker stack deploy -c docker-compose.yml layerloupe
```

## Real-world: Kubernetes

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: layerloupe-secrets
type: Opaque
stringData:
  admin_password: my-password
  session_secret: <32-byte random>
---
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: layerloupe
          env:
            - name: ADMIN_PASSWORD_FILE
              value: /run/secrets/admin_password
            - name: SESSION_SECRET_FILE
              value: /run/secrets/session_secret
          volumeMounts:
            - name: secrets
              mountPath: /run/secrets
              readOnly: true
      volumes:
        - name: secrets
          secret:
            secretName: layerloupe-secrets
```

## Configuration knobs (per-mode summary)

| Variable | Source | Notes |
|---|---|---|
| `AUTH_MODE=admin` | env | Enables login + delete. |
| `ADMIN_USERNAME` | env | Not a secret — keeping it inline keeps the secret file count down. |
| `ADMIN_PASSWORD_FILE` | env (points at file) | The file content is the plaintext password. |
| `SESSION_SECRET_FILE` | env (points at file) | The file content is the session-signing secret. |
| `AUDIT_LOG_PATH` | env | Where delete events get appended as JSONL. |
