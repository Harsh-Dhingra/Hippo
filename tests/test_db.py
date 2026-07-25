"""Connection handling: one-off connections and the pool."""

import psycopg
import pytest
from psycopg_pool import PoolClosed

from core.db import Database, connect

pytestmark = pytest.mark.requires_db


def test_connect_yields_a_usable_connection(db_dsn: str) -> None:
    with connect(db_dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone() == (1,)


def test_connect_autocommit_is_off_by_default(db_dsn: str) -> None:
    with connect(db_dsn) as conn:
        assert conn.autocommit is False
    with connect(db_dsn, autocommit=True) as conn:
        assert conn.autocommit is True


def test_pool_serves_connections(db_dsn: str) -> None:
    db = Database(db_dsn, min_size=1, max_size=2)
    db.open()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT 42")
            assert cur.fetchone() == (42,)
    finally:
        db.close()


def test_pool_is_reusable_across_checkouts(db_dsn: str) -> None:
    with Database(db_dsn, min_size=1, max_size=2) as db:
        for expected in (1, 2, 3):
            with db.connection() as conn, conn.cursor() as cur:
                cur.execute("SELECT %s", (expected,))
                assert cur.fetchone() == (expected,)


def test_context_manager_closes_the_pool(db_dsn: str) -> None:
    with Database(db_dsn) as db, db.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1")

    with pytest.raises(PoolClosed), db.connection():
        pass


def test_open_without_waiting_tolerates_an_unreachable_database() -> None:
    """A served process reports unhealthy; it does not refuse to boot."""
    db = Database("postgresql://127.0.0.1:1/nope?connect_timeout=1")
    db.open(timeout=1.0, wait=False)
    try:
        with pytest.raises(psycopg.Error), db.connection(timeout=1.0):
            pass
    finally:
        db.close()
