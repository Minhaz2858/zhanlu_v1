# ── Zhanlu Sandbox Worker ─────────────────────────────────────────────
# Long-running service that polls Redis for sandbox jobs and executes
# them in isolated Docker containers.  This is the ONLY service allowed
# to access the Docker socket.
# Build: docker compose build sandbox-worker

FROM python:3.11-alpine

# ── Use faster Alpine mirror ───────────────────────────────────────────
RUN sed -i 's|dl-cdn.alpinelinux.org|mirrors.aliyun.com|g' /etc/apk/repositories

# ── System dependencies ──────────────────────────────────────────────
# Only runtime deps — psycopg2-binary is precompiled, no build tools needed.
# docker-cli is required for the worker to inspect/manage containers.
# unixodbc provides libodbc.so.2 — the runtime shared library that the
# pre-installed pyodbc module links against (MSSQL connector).
RUN apk add --no-cache \
    docker-cli \
    curl \
    bash \
    unixodbc

# ── Create non-root user ─────────────────────────────────────────────
# The worker must be able to talk to the host Docker daemon via the
# bind-mounted unix socket (/var/run/docker.sock) — that socket is owned
# by the host's `docker` group, whose GID is passed in at build time so
# the same image works regardless of the host.  Default 998 matches
# Debian/Ubuntu and Docker Desktop defaults; override via build arg on
# hosts that use a different docker GID (e.g. some RHEL setups use 990).
ARG DOCKER_GID=998
RUN addgroup -S zhanlu \
    && (addgroup -S -g ${DOCKER_GID} docker 2>/dev/null || true) \
    && adduser -S zhanlu -G zhanlu -u 10001 \
    && addgroup zhanlu docker

# ── App directory ────────────────────────────────────────────────────
WORKDIR /app

# ── Dependencies ─────────────────────────────────────────────────────
COPY requirements.txt .
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
RUN pip install --no-cache-dir -i ${PIP_INDEX_URL} -r requirements.txt

# ── Source ───────────────────────────────────────────────────────────
COPY . .

RUN chown -R zhanlu:zhanlu /app
USER zhanlu

# ── Run ──────────────────────────────────────────────────────────────
# The worker polls Redis (or PostgreSQL fallback) for sandbox jobs.
CMD ["python", "-m", "sandbox_worker.main"]
