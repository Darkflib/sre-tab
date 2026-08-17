# Developer News Dashboard — application image.
#
# Deliberately plain OCI Dockerfile syntax: no `# syntax=` frontend
# directive, no `RUN --mount=` cache or bind mounts, no heredocs. Everything
# here is understood identically by Docker/BuildKit and by Podman/Buildah,
# because the image is built with Docker on developer machines and with
# Podman in CI and on the deployment host. Cache mounts would shave time off
# a warm local rebuild and buy nothing in CI, where the cache is cold anyway.
#
# Every base image is pinned by tag *and* digest. The tag documents intent
# and the digest is what is actually resolved; Renovate updates both together
# (see .github/renovate.json5).
#
# Build:
#   docker build --tag sre-tab:dev .
#   podman build --format docker --tag sre-tab:dev .
#
# `--format docker` matters for Podman: HEALTHCHECK is a Docker-schema field
# with no OCI equivalent, so an OCI-format build silently drops it. The
# deployment does not depend on that — deploy/quadlet/sre-tab.container sets
# HealthCmd= explicitly — but a locally built image should behave the same
# either way.

# --- Frontend -------------------------------------------------------------
# Node 24 covers frontend/package.json's ">=20.19" floor (Vite 8's minimum)
# with room to spare.
FROM node:24-trixie-slim@sha256:0711b541c1c33a8a530ac4f0d391baa9a15b3d804695b1b24a47daa5fb60e74d AS frontend

WORKDIR /build

# Manifest first so the dependency layer survives source-only changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund

# .dockerignore drops frontend/node_modules, so this cannot clobber the
# install above with a host tree built for a different platform.
COPY frontend/ ./

# src/api/schema.d.ts is committed generated output, so a plain build needs
# no codegen step and no network access.
RUN npm run build

# --- uv -------------------------------------------------------------------
# Binary donor only; nothing from this stage reaches the runtime image.
FROM ghcr.io/astral-sh/uv:0.12.5@sha256:e85be844203885286c60ffad8a858d48afb6c5a5c237ca0e67f12e74b8f174b1 AS uv

# --- Python build ---------------------------------------------------------
FROM python:3.12-slim-trixie@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a AS builder

COPY --from=uv /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the committed lockfile and are installed before
# any application source is copied, so editing app/ does not re-resolve them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
RUN uv sync --frozen --no-dev --no-editable

# --- Runtime --------------------------------------------------------------
FROM python:3.12-slim-trixie@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# UID/GID 10001 matches the orbit-data house convention: a fixed numeric
# identity, so bind-mount ownership is predictable across hosts and the
# quadlet's User= never has to resolve a name inside the image.
RUN groupadd --system --gid 10001 sre-tab \
    && useradd --system --uid 10001 --gid sre-tab --home-dir /app --shell /usr/sbin/nologin sre-tab \
    && mkdir -p /app /srv/www \
    && chown sre-tab:sre-tab /app /srv/www

# Strip every setuid and setgid bit. python:3.12-slim-trixie ships eleven —
# mount, umount, su, passwd, chsh, chfn, chage, expiry, gpasswd, newgrp,
# unix_chkpwd — and nothing here runs any of them: the application is one
# unprivileged uid on a read-only rootfs with all capabilities dropped.
#
# This is what deploy/quadlet/sre-tab.container relies on in place of
# NoNewPrivileges=true, which that unit cannot set (podman's AppArmor profile
# denies signal delivery under no_new_privs on Debian 13, so uvicorn cannot
# shut down cleanly — the note in the unit has the measurements). Removing the
# bits deletes the escalation outright rather than disarming it at runtime, and
# because it runs at build time it re-applies itself if a base-image bump ever
# introduces a new one.
RUN find / -xdev -perm /6000 -type f -exec chmod a-s '{}' + || true

WORKDIR /app

# The virtualenv carries the application as an installed (non-editable)
# wheel, so no app/ source tree is shipped.
COPY --from=builder --chown=sre-tab:sre-tab /app/.venv /app/.venv

# Alembic is not part of the wheel (pyproject packages only "app") and the
# migration unit runs `alembic upgrade head` from this working directory.
COPY --chown=sre-tab:sre-tab alembic.ini ./alembic.ini
COPY --chown=sre-tab:sre-tab alembic ./alembic

# Built frontend, published from here into a shared volume by
# deploy/quadlet/sre-tab-assets.container and served by Caddy. Keeping the
# assets in this image rather than a second one makes API/asset version skew
# impossible: there is one artefact to build, sign, and pull.
#
# /srv/www is created above, empty and owned by 10001, purely so that the
# first mount of the empty assets volume inherits that ownership: both Podman
# and Docker copy the image directory's uid/gid/mode onto a fresh named
# volume. Without it the volume arrives root-owned and the publish step —
# which runs unprivileged, as everything here does — cannot write to it.
COPY --from=frontend --chown=sre-tab:sre-tab /build/dist /opt/sre-tab/web

USER 10001:10001

EXPOSE 8000

# urllib rather than curl: the check costs no extra package, no extra layer,
# and no extra attack surface. /api/v1/healthz answers 200 only when both
# liveness and readiness probes pass, and raises for the 503 it returns when
# they do not.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/healthz', timeout=4)"]

# Exec form: uvicorn becomes PID 1 and receives SIGTERM directly, so podman
# stop drains connections and exits cleanly instead of waiting out the kill
# timeout.
#
# No init process, and deliberately so. An init would only matter if uvicorn
# left orphans to reap, which it does not — and on the deployment host adding
# one makes shutdown worse rather than better, because podman's catatonit is
# bind-mounted from the host and cannot signal into the container under
# AppArmor. deploy/quadlet/sre-tab.container has the measurements.
#
# No --proxy-headers or --forwarded-allow-ips here on purpose. uvicorn enables
# proxy-header handling by default and reads the trusted-peer list from
# FORWARDED_ALLOW_IPS, which deploy/app.env.example sets — so the trust
# boundary is configuration an operator can see and change, not a flag baked
# into the image. tests/test_proxy_headers.py pins both of those uvicorn
# behaviours, because per-IP rate limiting silently becomes global if either
# changes underneath us.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
