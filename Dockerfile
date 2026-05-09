# syntax=docker/dockerfile:1.7
#
# LayerLoupe container image.
#
# Two stages:
#   1. ``deps``    — installs runtime deps into ``/app/.venv`` using uv,
#                    leveraging the official uv binary image so we don't
#                    pay for a curl install dance.
#   2. ``runtime`` — slim Python image with the venv copied in and the
#                    application code on top. Runs as a non-root user.
#
# Note: this project uses hand-written CSS instead of Tailwind, so there's
# no Tailwind build stage here — ``layerloupe/web/static/layerloupe.css`` is
# shipped as-is. If we ever switch to Tailwind, drop a ``tailwind-build``
# stage in front of ``runtime`` and ``COPY --from=tailwind-build`` the
# minified output into ``/app/layerloupe/web/static/``.

# -- Stage 1: build the project venv via uv -----------------------------
FROM python:3.14-slim AS deps

# uv is shipped as a single static binary in its official image; copying it
# is cheaper and more reproducible than installing Python uv via pip.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /usr/local/bin/

WORKDIR /app

# Bytecode files inside the venv would just bloat the image.
ENV UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    PYTHONDONTWRITEBYTECODE=1

# Copy project metadata first so the layer cache hits unless deps change.
COPY pyproject.toml uv.lock ./

# Pre-install dependencies without the project itself (faster cold builds).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Now copy the source and install the project on top of the locked deps.
COPY README.md ./
COPY layerloupe/ ./layerloupe/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# -- Stage 2: minimal runtime image -------------------------------------
FROM python:3.14-slim AS runtime

LABEL org.opencontainers.image.title="LayerLoupe" \
      org.opencontainers.image.description="Modern OCI-aware browser for Docker / OCI registries" \
      org.opencontainers.image.source="https://github.com/hyneklipa/layerloupe" \
      org.opencontainers.image.licenses="MIT"

# Run as a known non-root UID so volume permissions and Kubernetes
# ``runAsNonRoot: true`` policies don't bite at deploy time.
RUN groupadd --system --gid 1001 layerloupe \
 && useradd --system --uid 1001 --gid layerloupe --home /app --shell /usr/sbin/nologin layerloupe

WORKDIR /app

# Bring in the prebuilt venv (and only the venv — no source from the
# deps stage). Code is copied separately so an app-only change doesn't
# bust the deps layer cache.
COPY --from=deps --chown=layerloupe:layerloupe /app/.venv /app/.venv
COPY --chown=layerloupe:layerloupe layerloupe/ /app/layerloupe/

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LOG_JSON=true

USER layerloupe

EXPOSE 8080

# Container-level liveness check — runs the same probe the readiness
# endpoint exposes, just over the loopback.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import sys, urllib.request; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/healthz', timeout=2).status == 200 else 1)" \
        || exit 1

CMD ["uvicorn", "layerloupe.main:app", "--host", "0.0.0.0", "--port", "8080"]
