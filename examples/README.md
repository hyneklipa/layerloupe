# Configuration examples

Each subdirectory under `examples/` is a self-contained deployment
scenario: a `docker-compose.yml`, a documented `.env.example`, and a
`README.md` that explains when to pick this one. Copy the directory,
adjust the env, and you have a runnable stack.

## Picking a scenario

| Scenario | Auth | Delete | Use case |
|---|---|---|---|
| [`public/`](public/) | none | never | Internal browse-only mirror, public registry explorer. Anonymous read-only access. This is the canonical quickstart - equivalent to the root `docker-compose.yml`. |
| [`protected/`](protected/) | required | never | Publicly exposed instance where you don't want open browse but also don't need anyone with delete rights. |
| [`admin/`](admin/) | required | admin role | Private instance for the registry maintainer - admin can delete tags from the UI. |
| [`admin-docker-secrets/`](admin-docker-secrets/) | required | admin role | Same as `admin/` but with the admin password supplied via a Docker / Kubernetes secret file (`ADMIN_PASSWORD_FILE`) instead of an env value. |

## Conventions across all examples

- **Registry connection** is always to a sibling `registry:2` service on
  the same Compose network - keeps each example self-contained.
- **`SESSION_SECRET`** is always set explicitly. The placeholder value
  in every `.env.example` is a clear "regenerate me" string; running
  `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`
  gives you a real one.
- **No bind mounts of source code.** Examples test the *image*, not a
  local source tree.
- Each scenario's `docker-compose.yml` carries `# Scenario: ...`
  comments so it's clear at a glance which knobs matter for that
  scenario vs. shared boilerplate.

## Adding a new example

1. Create `examples/<scenario>/` with the three required files
   (`docker-compose.yml`, `.env.example`, `README.md`).
2. Add a row to the table above.
3. `tests/test_examples.py` automatically picks up every new
   subdirectory and runs the structural checks against it.
