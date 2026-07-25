# syntax=docker/dockerfile:1
#
# The app container. One of two; the other is stock Postgres.
# Build context is the repo root: `docker build -f deploy/Dockerfile .`

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dependencies first, so a source change does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY README.md LICENSE ./
COPY core ./core
COPY api ./api
COPY sync ./sync
COPY resolver ./resolver
COPY agent ./agent

RUN uv sync --frozen --no-dev

# Runs unprivileged. Nothing here needs root.
RUN useradd --create-home --uid 10001 hippo
USER hippo

EXPOSE 8000

# Migrations run at startup by default (HIPPO_MIGRATE_ON_STARTUP=false to disable),
# which is what keeps `docker compose up` a single step.
CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
