# Release image for the Coliseum of Knowledge Telegram bot.
# Base is intentionally Debian slim (not Alpine): the accepted runtime is
# CPython 3.12 and the dependency set is pure-Python / manylinux wheels.
#
# The digest is the multi-arch index digest for python:3.12.16-slim-bookworm
# (published 2026-10-28). Update the tag and digest together, then rebuild and
# re-run the smoke checks; never silently move the base under an existing tag.
FROM python:3.12.16-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    HOME=/tmp \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Dedicated runtime identity. The same numeric UID/GID is used by Compose and
# by the host bind-mount bootstrap instructions in docs/DOCKER_DEPLOYMENT.md.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --home-dir /home/app \
       --shell /usr/sbin/nologin app

WORKDIR /app

# requirements.txt in the accepted tree is UTF-16LE with a BOM and CRLF.
# Convert it only for the installer; the owner file is not rewritten.
COPY requirements.txt /tmp/requirements.txt
RUN python -c "from pathlib import Path; raw = Path('/tmp/requirements.txt').read_bytes(); text = raw.decode('utf-16') if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else raw.decode('utf-8-sig'); Path('/tmp/requirements.normalized.txt').write_text(text, encoding='utf-8')" \
    && pip install --no-cache-dir --no-compile --root-user-action=ignore \
       -r /tmp/requirements.normalized.txt \
    && rm -f /tmp/requirements.txt /tmp/requirements.normalized.txt

RUN pip install --no-cache-dir --no-compile --root-user-action=ignore "aiohttp-socks==0.12.0"

# .dockerignore is the build-context boundary: no .env, SQLite, storage,
# uploads, archives, tests, logs, venvs, Git history or recovery data.
COPY --chown=10001:10001 . /app

# Writable mount points are created in the image so empty bind mounts receive
# the correct ownership. Static assets (imgs/, catalog/) stay read-only by
# virtue of Compose's read_only root filesystem.
RUN mkdir -p /app/storage /data \
    && chown -R 10001:10001 /app/storage /data

USER 10001:10001

# No EXPOSE: the bot uses Telegram long polling, not an inbound HTTP port.
STOPSIGNAL SIGTERM

# Exec form: Python is PID 1 only because Compose sets init: true (tini).
CMD ["python", "-B", "main.py"]
