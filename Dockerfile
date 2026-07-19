# syntax=docker/dockerfile:1

# --- builder ------------------------------------------------------------
# Install runtime deps into a venv using uv. Kept in a separate stage so
# uv itself doesn't ship in the runtime image.
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
# --frozen: fail if uv.lock is stale — keeps builds reproducible
# --no-dev: skip test/lint/type-check tooling
# --no-install-project: don't try to install this tree (package = false anyway)
RUN uv sync --frozen --no-dev --no-install-project

# --- runtime ------------------------------------------------------------
FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/Bitwise-Forge/icloud-shared-album-sync"
LABEL org.opencontainers.image.description="The shared album, backed up somewhere you actually control."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.vendor="Bitwise Forge"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUTPUT_DIR=/photos \
    PATH="/app/.venv/bin:$PATH"

# Runtime user — never run as root inside the container. Named `app` because
# Debian ships a stock `sync` user. UID 1000 matches the first regular user
# on most Linux hosts, which reduces bind-mount permission friction.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /photos \
    && chown -R app:app /photos

WORKDIR /app
COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app src/sync.py /app/sync.py

USER app

# Exec form so SIGTERM propagates to the Python process, letting sync.py's
# signal handler drain cleanly. -u would be redundant given PYTHONUNBUFFERED.
ENTRYPOINT ["python3", "/app/sync.py"]
