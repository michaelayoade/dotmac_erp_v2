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

RUN pip install --no-cache-dir uv==0.13.7

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src

RUN --mount=type=secret,id=forgejo_token \
    UV_INDEX_FORGEJO_USERNAME=ci-reader \
    UV_INDEX_FORGEJO_PASSWORD="$(cat /run/secrets/forgejo_token)" \
    uv sync --no-dev --no-install-project \
    && UV_INDEX_FORGEJO_USERNAME=ci-reader \
    UV_INDEX_FORGEJO_PASSWORD="$(cat /run/secrets/forgejo_token)" \
    uv sync --no-dev

# Runtime imports the app only; importer/migrate run as one-off
# commands from this same image with their own env (never bake
# MIGRATION_/LEGACY_ DSNs into the serving container).
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"

CMD ["uvicorn", "erp_v2.main:app", "--host", "0.0.0.0", "--port", "8000"]
