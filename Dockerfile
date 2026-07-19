# syntax=docker/dockerfile:1

# --- builder ------------------------------------------------------------
# Install runtime deps into a venv using uv. Kept in a separate stage so
# uv itself doesn't ship in the runtime image.
FROM ghcr.io/astral-sh/uv:python3.14-alpine AS builder

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: fail if uv.lock is stale — keeps builds reproducible
# --no-dev: skip test/lint/type-check tooling
# --no-install-project: don't try to install this tree (package = false anyway)
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime ------------------------------------------------------------
FROM python:3.14-alpine

LABEL org.opencontainers.image.source="https://github.com/Bitwise-Forge/icloud-shared-album-sync"
LABEL org.opencontainers.image.description="The shared album, backed up somewhere you actually control."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.vendor="Bitwise Forge"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUTPUT_DIR=/photos \
    PATH="/app/.venv/bin:$PATH"

# Runtime user — never run as root inside the container. Named `app` because
# Alpine ship a stock `sync` user. UID 1000 matches the first regular user
# on most Linux hosts, which reduces bind-mount permission friction. -D
# creates a system user with no password; -H skips home dir creation since
# we don't need it.
RUN adduser -D -H -u 1000 -s /sbin/nologin app \
    && mkdir -p /photos \
    && chown -R app:app /photos

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/icloud_sync /app/icloud_sync

USER app

# Exec form so SIGTERM propagates to the Python process, letting the
# signal handler drain cleanly. -u would be redundant given PYTHONUNBUFFERED.
ENTRYPOINT ["python3", "-m", "icloud_sync"]
