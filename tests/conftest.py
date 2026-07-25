"""Shared fixtures.

Database tests run against a throwaway database created per test, so no test
can see another's schema. Point HIPPO_TEST_DATABASE_URL at any Postgres 16 with
pgvector; it defaults to a local server.
"""

import os
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from core.config import Settings, get_settings

ADMIN_DSN = os.environ.get("HIPPO_TEST_DATABASE_URL", "postgresql://localhost:5432/postgres")


def _with_dbname(dsn: str, dbname: str) -> str:
    params: dict[str, str] = {
        key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None
    }
    params["dbname"] = dbname
    return make_conninfo(**params)


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    """A DSN with rights to create databases, or skip the whole DB suite."""
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=3):
            pass
    except psycopg.Error as exc:
        pytest.skip(f"no Postgres at {ADMIN_DSN}: {exc}")
    return ADMIN_DSN


@pytest.fixture
def db_dsn(admin_dsn: str) -> Iterator[str]:
    """A freshly created, empty database, dropped when the test ends."""
    name = f"hippo_test_{uuid4().hex[:16]}"
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _with_dbname(admin_dsn, name)
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db_conn(db_dsn: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """An autocommit connection to the throwaway database."""
    with psycopg.connect(db_dsn, autocommit=True) as conn:
        yield conn


@pytest.fixture
def settings(db_dsn: str) -> Settings:
    """Settings wired to the throwaway database, migrations applied on startup."""
    return Settings(database_url=db_dsn, log_level="WARNING", service_name="hippo-test")


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """Keep the lru_cache on get_settings from leaking between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def write_migration(
    directory: Path,
    version: int,
    name: str,
    sql: str,
    down_sql: str | None = None,
) -> None:
    """Create a migration file pair inside a temp migrations directory."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{version:03d}_{name}.sql").write_text(sql, encoding="utf-8")
    if down_sql is not None:
        (directory / f"{version:03d}_{name}.down.sql").write_text(down_sql, encoding="utf-8")
