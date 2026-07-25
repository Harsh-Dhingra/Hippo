"""Postgres access.

Raw SQL, no ORM: the schema in core/migrations is the source of truth and a
model layer would only be a second, drifting copy of it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Self

import psycopg
from psycopg.rows import TupleRow
from psycopg_pool import ConnectionPool

Connection = psycopg.Connection[TupleRow]


@contextmanager
def connect(dsn: str, *, autocommit: bool = False) -> Iterator[Connection]:
    """A single short-lived connection. For migrations, CLI tools and tests."""
    with psycopg.connect(dsn, autocommit=autocommit) as conn:
        yield conn


class Database:
    """A lazily-opened connection pool.

    Constructed at import time but not connected until `open()`, so that
    building the app object never requires a reachable database.
    """

    def __init__(self, dsn: str, *, min_size: int = 1, max_size: int = 10) -> None:
        self._pool: ConnectionPool[Connection] = ConnectionPool(
            dsn,
            min_size=min_size,
            max_size=max_size,
            open=False,
        )

    def open(self, timeout: float = 10.0, *, wait: bool = True) -> None:
        """Start the pool.

        `wait=False` returns immediately without proving the database is
        reachable, which is what a served process wants: the healthcheck, not a
        crash loop, is how an unreachable database gets reported.
        """
        self._pool.open(wait=wait, timeout=timeout)

    def close(self) -> None:
        self._pool.close()

    @contextmanager
    def connection(self, timeout: float | None = None) -> Iterator[Connection]:
        with self._pool.connection(timeout=timeout) as conn:
            yield conn

    def __enter__(self) -> Self:
        self.open()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
