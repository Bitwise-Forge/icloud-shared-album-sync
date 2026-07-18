# syntax=docker/dockerfile:1

FROM python:3.13-slim

LABEL org.opencontainers.image.source="https://github.com/Bitwise-Forge/icloud-shared-album-sync"
LABEL org.opencontainers.image.description="The shared album, backed up somewhere you actually control."
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.vendor="Bitwise Forge"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OUTPUT_DIR=/photos

# Runtime user — never run as root inside the container. Named `app` because
# Debian's stock users include `sync`. UID 1000 matches the first regular
# user on most Linux hosts, which reduces bind-mount permission friction.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /photos \
    && chown -R app:app /photos

WORKDIR /app
COPY --chown=app:app src/sync.py /app/sync.py

USER app

# Exec form so SIGTERM propagates to the Python process, letting sync.py's
# signal handler drain cleanly. -u would be redundant given PYTHONUNBUFFERED.
ENTRYPOINT ["python3", "/app/sync.py"]
