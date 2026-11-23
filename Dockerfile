# ERP v2 candidate image.
#
# Exact-wheel build from the Forgejo index (Gate 1). The Forgejo
# read token is a BuildKit secret, never a build arg or layer:
#
#   DOCKER_BUILDKIT=1 docker build \
#     --secret id=forgejo_token,env=FORGEJO_READ_TOKEN \
#     -t dotmac-erp-v2:<candidate-sha> .
#
# Without the token the sync step fails with 401 by design; the
# Gate 4 rehearsal's released-source bundle is not an image path.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/root/.local/bin:/app/.venv/bin:$PATH"

# git: dotmac-app-sync is a git-sourced dependency (uv needs git to fetch it)
RUN pip install --no-cache-dir uv==0.13.7 \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
COPY src ./src

# --locked: install exactly the committed uv.lock (Gate 1);
# a drifting resolution fails the build loudly instead.
RUN --mount=type=secret,id=forgejo_token \
    UV_INDEX_FORGEJO_USERNAME=ci-reader \
    UV_INDEX_FORGEJO_PASSWORD="$(cat /run/secrets/forgejo_token)" \
    uv sync --locked --no-dev --no-install-project \
    && UV_INDEX_FORGEJO_USERNAME=ci-reader \
    UV_INDEX_FORGEJO_PASSWORD="$(cat /run/secrets/forgejo_token)" \
    uv sync --locked --no-dev

# Operator one-shots (target_api v2 run_job) read /job/in and write /job/out, as the
# SEPARATE uid `dotmac-erp-import` (P-SEC C-29), never the serving ERP uid 10001. The
# image ships both directories so Foundation's per-dispatch volumes inherit them:
# /job/out is owned by that uid at 0700; /job/in exists, world readable and
# root-owned (inputs are mounted read-only over it).
RUN groupadd --gid 10002 dotmac-erp-import \
    && useradd --uid 10002 --gid 10002 --no-create-home \
        --shell /usr/sbin/nologin dotmac-erp-import \
    && mkdir -p /job/in /job/out \
    && chown 10002:10002 /job/out \
    && chmod 0700 /job/out \
    && chmod 0755 /job/in

# Runtime imports the app only; importer/migrate run as one-off
# commands from this same image with their own env (never bake
# MIGRATION_/LEGACY_ DSNs into the serving container).
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["python", "-m", "erp_v2.runtime", "--port", "8000"]
