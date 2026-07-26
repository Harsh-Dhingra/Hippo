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
# Every first-party package api.main imports, transitively. A missing one is
# an ImportError at container start rather than a degraded feature, and
# tests/test_packaging.py compares this list against the real import graph so
# adding a package cannot silently forget this file.
COPY core ./core
COPY api ./api
COPY sync ./sync
COPY resolver ./resolver
COPY agent ./agent
COPY surfaces ./surfaces

# Worked examples, so HIPPO_SKILLS_PATH=/app/skills works out of the box.
COPY skills ./skills

# The demo world. A few kilobytes of JSON so `docker compose up` can be tried
# before anyone connects a Slack workspace; see deploy/demo/__init__.py.
COPY deploy/demo ./deploy/demo
COPY tests/fixtures ./tests/fixtures

RUN uv sync --frozen --no-dev

# Runs unprivileged. Nothing here needs root.
RUN useradd --create-home --uid 10001 hippo
USER hippo

EXPOSE 8000

# Migrations run at startup by default (HIPPO_MIGRATE_ON_STARTUP=false to disable),
# which is what keeps `docker compose up` a single step.
CMD ["uvicorn", "api.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
