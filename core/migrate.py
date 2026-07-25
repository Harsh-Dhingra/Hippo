"""Numbered SQL migrations.

Migrations are append-only, numbered, and reversible where possible
(CLAUDE.md, Conventions). A file that has already been applied may never be
edited: the runner checksums every applied migration on each run and refuses to
do anything at all if one has changed. Silent drift between what the database
contains and what the repo claims is exactly the failure this guards.

Each migration runs inside its own transaction, and the whole run is guarded by
a session advisory lock so two processes booting at once cannot both apply the
same file.
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from core.config import get_settings
from core.db import Connection, connect
from core.logging import configure_logging

LOG = logging.getLogger("hippo.migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"

# Every migrating process contends for this one key.
ADVISORY_LOCK_KEY = 4_012_025

_FILENAME_RE = re.compile(r"^(?P<version>\d{3})_(?P<name>[a-z0-9_]+?)(?P<down>\.down)?\.sql$")

_TRACKING_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     integer PRIMARY KEY,
    name        text NOT NULL,
    checksum    text NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """Base class for every failure this module raises."""


class MigrationDiscoveryError(MigrationError):
    """The migrations directory is malformed."""


class MigrationDriftError(MigrationError):
    """The database and the migration files disagree."""


class IrreversibleMigrationError(MigrationError):
    """A downgrade was requested for a migration with no .down.sql."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One numbered migration, with its optional inverse."""

    version: int
    name: str
    path: Path
    down_path: Path | None

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def reversible(self) -> bool:
        return self.down_path is not None

    def down_sql(self) -> str:
        if self.down_path is None:
            msg = f"migration {self.version:03d}_{self.name} has no down migration"
            raise IrreversibleMigrationError(msg)
        return self.down_path.read_text(encoding="utf-8")


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    """A row of schema_migrations."""

    version: int
    name: str
    checksum: str


@dataclass(frozen=True, slots=True)
class Status:
    """What the database has, and what the repo still owes it."""

    current_version: int | None
    applied: tuple[AppliedMigration, ...]
    pending: tuple[Migration, ...]

    @property
    def up_to_date(self) -> bool:
        return not self.pending


def discover(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    """Read the migrations directory, newest last.

    Raises MigrationDiscoveryError on anything ambiguous: an unparseable
    filename, a duplicate version number, or a .down.sql with no matching up.
    """
    if not directory.is_dir():
        msg = f"migrations directory not found: {directory}"
        raise MigrationDiscoveryError(msg)

    ups: dict[int, tuple[str, Path]] = {}
    downs: dict[int, Path] = {}

    for path in sorted(directory.iterdir()):
        # Case-insensitive so that `001_schema.SQL` reaches the regex and is
        # rejected. On a case-insensitive filesystem, silently skipping it would
        # mean a migration that exists in the repo and never runs.
        if path.suffix.lower() != ".sql":
            continue
        match = _FILENAME_RE.match(path.name)
        if match is None:
            msg = f"migration filename does not match NNN_lower_snake_case[.down].sql: {path.name}"
            raise MigrationDiscoveryError(msg)

        version = int(match.group("version"))
        name = match.group("name")
        if match.group("down"):
            downs[version] = path
            continue
        if version in ups:
            msg = (
                f"duplicate migration version {version:03d}: {ups[version][1].name} and {path.name}"
            )
            raise MigrationDiscoveryError(msg)
        ups[version] = (name, path)

    orphans = sorted(set(downs) - set(ups))
    if orphans:
        listed = ", ".join(downs[v].name for v in orphans)
        msg = f"down migration with no matching up migration: {listed}"
        raise MigrationDiscoveryError(msg)

    return tuple(
        Migration(version=version, name=name, path=path, down_path=downs.get(version))
        for version, (name, path) in sorted(ups.items())
    )


def ensure_tracking_table(conn: Connection) -> None:
    """Create schema_migrations if it is not there yet."""
    with conn.cursor() as cur:
        cur.execute(_TRACKING_DDL)


def applied(conn: Connection) -> dict[int, AppliedMigration]:
    """Every migration the database believes it has, keyed by version."""
    with conn.cursor() as cur:
        cur.execute("SELECT version, name, checksum FROM schema_migrations ORDER BY version")
        rows = cur.fetchall()
    return {
        int(row[0]): AppliedMigration(version=int(row[0]), name=str(row[1]), checksum=str(row[2]))
        for row in rows
    }


def verify(
    migrations: Sequence[Migration], applied_migrations: dict[int, AppliedMigration]
) -> None:
    """Fail loudly if the files and the database disagree.

    Three ways to disagree, all fatal:
      * an applied migration's file was edited (checksum mismatch),
      * an applied migration's file was deleted,
      * a new migration was numbered below one already applied, which would
        otherwise apply out of order and produce a schema nobody can reproduce.
    """
    by_version = {m.version: m for m in migrations}

    for version, record in sorted(applied_migrations.items()):
        migration = by_version.get(version)
        if migration is None:
            msg = (
                f"migration {version:03d}_{record.name} is applied in the database but its "
                f"file is missing from the repo"
            )
            raise MigrationDriftError(msg)
        if migration.checksum != record.checksum:
            msg = (
                f"migration {version:03d}_{record.name} was edited after being applied "
                f"(expected checksum {record.checksum[:12]}, file is {migration.checksum[:12]}). "
                f"Migrations are append-only: add a new migration instead."
            )
            raise MigrationDriftError(msg)

    if applied_migrations:
        highest_applied = max(applied_migrations)
        late = [
            m
            for m in migrations
            if m.version < highest_applied and m.version not in applied_migrations
        ]
        if late:
            listed = ", ".join(f"{m.version:03d}_{m.name}" for m in late)
            msg = (
                f"migration numbered below the highest applied version ({highest_applied:03d}): "
                f"{listed}. Renumber it above {highest_applied:03d}."
            )
            raise MigrationDriftError(msg)


def pending(
    conn: Connection, migrations: Sequence[Migration] | None = None
) -> tuple[Migration, ...]:
    """Migrations present in the repo and absent from the database."""
    resolved = discover() if migrations is None else migrations
    ensure_tracking_table(conn)
    known = applied(conn)
    verify(resolved, known)
    return tuple(m for m in resolved if m.version not in known)


def current_version(conn: Connection) -> int | None:
    """Highest applied version, or None on a virgin database."""
    ensure_tracking_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT max(version) FROM schema_migrations")
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def status(conn: Connection, migrations: Sequence[Migration] | None = None) -> Status:
    """Everything the CLI and the healthcheck need, in one round of queries."""
    resolved = discover() if migrations is None else migrations
    ensure_tracking_table(conn)
    known = applied(conn)
    verify(resolved, known)
    return Status(
        current_version=max(known) if known else None,
        applied=tuple(known[v] for v in sorted(known)),
        pending=tuple(m for m in resolved if m.version not in known),
    )


@contextmanager
def _advisory_lock(conn: Connection) -> Iterator[None]:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
    try:
        yield
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))


def upgrade(conn: Connection, migrations: Sequence[Migration] | None = None) -> tuple[int, ...]:
    """Apply every pending migration in order. Returns the versions applied.

    Safe to run concurrently: the advisory lock serialises runs, and the
    pending set is recomputed once the lock is held.
    """
    resolved = discover() if migrations is None else migrations
    applied_versions: list[int] = []

    with _advisory_lock(conn):
        ensure_tracking_table(conn)
        known = applied(conn)
        verify(resolved, known)

        for migration in resolved:
            if migration.version in known:
                continue
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(migration.sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            applied_versions.append(migration.version)
            LOG.info(
                "migration applied",
                extra={"version": migration.version, "migration": migration.name},
            )

    return tuple(applied_versions)


def downgrade(
    conn: Connection,
    migrations: Sequence[Migration] | None = None,
    steps: int = 1,
) -> tuple[int, ...]:
    """Revert the newest `steps` applied migrations. Returns the versions reverted."""
    if steps < 1:
        msg = f"steps must be >= 1, got {steps}"
        raise ValueError(msg)

    resolved = discover() if migrations is None else migrations
    by_version = {m.version: m for m in resolved}
    reverted: list[int] = []

    with _advisory_lock(conn):
        ensure_tracking_table(conn)
        known = applied(conn)
        verify(resolved, known)

        targets = sorted(known, reverse=True)[:steps]
        # Check every target is reversible before touching the database, so a
        # multi-step downgrade cannot stop half way.
        for version in targets:
            migration = by_version[version]
            if not migration.reversible:
                msg = (
                    f"migration {version:03d}_{migration.name} is not reversible; "
                    f"add {version:03d}_{migration.name}.down.sql first"
                )
                raise IrreversibleMigrationError(msg)

        for version in targets:
            migration = by_version[version]
            with conn.transaction(), conn.cursor() as cur:
                cur.execute(migration.down_sql())
                cur.execute("DELETE FROM schema_migrations WHERE version = %s", (version,))
            reverted.append(version)
            LOG.info(
                "migration reverted",
                extra={"version": version, "migration": migration.name},
            )

    return tuple(reverted)


def _print_status(current: Status) -> None:
    version = "none" if current.current_version is None else f"{current.current_version:03d}"
    print(f"current version: {version}")
    print(f"applied: {len(current.applied)}")
    for record in current.applied:
        print(f"  {record.version:03d}_{record.name}")
    print(f"pending: {len(current.pending)}")
    for migration in current.pending:
        marker = "" if migration.reversible else "  (irreversible)"
        print(f"  {migration.version:03d}_{migration.name}{marker}")


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: hippo-migrate {up,down,status}."""
    parser = argparse.ArgumentParser(prog="hippo-migrate", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("up", help="apply all pending migrations")
    down = subparsers.add_parser("down", help="revert the newest applied migrations")
    down.add_argument("--steps", type=int, default=1)
    subparsers.add_parser("status", help="show applied and pending migrations")

    args = parser.parse_args(argv)
    settings = get_settings()
    configure_logging(level=settings.log_level, service="hippo-migrate")

    try:
        with connect(settings.database_url, autocommit=True) as conn:
            if args.command == "up":
                versions = upgrade(conn)
                print(f"applied {len(versions)} migration(s): {list(versions)}")
            elif args.command == "down":
                versions = downgrade(conn, steps=args.steps)
                print(f"reverted {len(versions)} migration(s): {list(versions)}")
            else:
                _print_status(status(conn))
    except MigrationError as exc:
        LOG.error("migration failed", extra={"error": str(exc)})
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
