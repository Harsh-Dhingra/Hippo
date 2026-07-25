"""Applying, reverting, and refusing to apply migrations against a real database."""

import threading
from pathlib import Path

import psycopg
import pytest

from core.db import Connection, connect
from core.migrate import (
    IrreversibleMigrationError,
    MigrationDriftError,
    applied,
    current_version,
    discover,
    downgrade,
    pending,
    status,
    upgrade,
)
from tests.conftest import write_migration

pytestmark = pytest.mark.requires_db


def _table_exists(conn: Connection, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s) IS NOT NULL", (name,))
        row = cur.fetchone()
    return bool(row is not None and row[0])


@pytest.fixture
def migrations_dir(tmp_path: Path) -> Path:
    write_migration(
        tmp_path,
        1,
        "widgets",
        "CREATE TABLE widgets (id int PRIMARY KEY)",
        down_sql="DROP TABLE widgets",
    )
    write_migration(
        tmp_path,
        2,
        "gadgets",
        "CREATE TABLE gadgets (id int PRIMARY KEY)",
        down_sql="DROP TABLE gadgets",
    )
    return tmp_path


def test_upgrade_applies_everything_pending(db_conn: Connection, migrations_dir: Path) -> None:
    versions = upgrade(db_conn, discover(migrations_dir))

    assert versions == (1, 2)
    assert _table_exists(db_conn, "widgets")
    assert _table_exists(db_conn, "gadgets")
    assert sorted(applied(db_conn)) == [1, 2]


def test_upgrade_records_name_and_checksum(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    upgrade(db_conn, migrations)

    record = applied(db_conn)[1]
    assert record.name == "widgets"
    assert record.checksum == migrations[0].checksum


def test_upgrade_is_idempotent(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    assert upgrade(db_conn, migrations) == (1, 2)
    assert upgrade(db_conn, migrations) == ()
    assert sorted(applied(db_conn)) == [1, 2]


def test_upgrade_applies_only_the_new_one(db_conn: Connection, tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "widgets", "CREATE TABLE widgets (id int)")
    upgrade(db_conn, discover(tmp_path))

    write_migration(tmp_path, 2, "gadgets", "CREATE TABLE gadgets (id int)")
    assert upgrade(db_conn, discover(tmp_path)) == (2,)


def test_current_version_and_status(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    assert current_version(db_conn) is None

    before = status(db_conn, migrations)
    assert before.up_to_date is False
    assert [m.version for m in before.pending] == [1, 2]
    assert before.applied == ()

    upgrade(db_conn, migrations)

    after = status(db_conn, migrations)
    assert current_version(db_conn) == 2
    assert after.up_to_date is True
    assert after.current_version == 2
    assert [record.version for record in after.applied] == [1, 2]


def test_pending_lists_unapplied(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    upgrade(db_conn, migrations[:1])

    assert [m.version for m in pending(db_conn, migrations)] == [2]


def test_editing_an_applied_migration_is_refused(db_conn: Connection, migrations_dir: Path) -> None:
    upgrade(db_conn, discover(migrations_dir))

    write_migration(migrations_dir, 1, "widgets", "CREATE TABLE widgets (id bigint PRIMARY KEY)")

    with pytest.raises(MigrationDriftError, match="edited after being applied"):
        upgrade(db_conn, discover(migrations_dir))


def test_deleting_an_applied_migration_is_refused(
    db_conn: Connection, migrations_dir: Path
) -> None:
    upgrade(db_conn, discover(migrations_dir))
    (migrations_dir / "002_gadgets.sql").unlink()
    (migrations_dir / "002_gadgets.down.sql").unlink()

    with pytest.raises(MigrationDriftError, match="file is missing"):
        upgrade(db_conn, discover(migrations_dir))


def test_migration_numbered_below_the_high_water_mark_is_refused(
    db_conn: Connection, migrations_dir: Path
) -> None:
    upgrade(db_conn, discover(migrations_dir))
    write_migration(migrations_dir, 0, "sneaked_in", "CREATE TABLE sneaked (id int)")

    with pytest.raises(MigrationDriftError, match="numbered below"):
        upgrade(db_conn, discover(migrations_dir))


def test_drift_blocks_before_applying_anything(db_conn: Connection, migrations_dir: Path) -> None:
    upgrade(db_conn, discover(migrations_dir)[:1])
    write_migration(migrations_dir, 1, "widgets", "CREATE TABLE widgets (id bigint)")

    with pytest.raises(MigrationDriftError):
        upgrade(db_conn, discover(migrations_dir))

    assert _table_exists(db_conn, "gadgets") is False, "no migration may run once drift is found"


def test_failed_migration_rolls_back_entirely(db_conn: Connection, tmp_path: Path) -> None:
    write_migration(
        tmp_path,
        1,
        "half_broken",
        "CREATE TABLE good (id int); CREATE TABLE bad (id nosuchtype);",
    )

    with pytest.raises(psycopg.Error):
        upgrade(db_conn, discover(tmp_path))

    assert _table_exists(db_conn, "good") is False
    assert applied(db_conn) == {}


def test_downgrade_reverts_the_newest(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    upgrade(db_conn, migrations)

    assert downgrade(db_conn, migrations, steps=1) == (2,)
    assert _table_exists(db_conn, "gadgets") is False
    assert _table_exists(db_conn, "widgets") is True
    assert sorted(applied(db_conn)) == [1]


def test_downgrade_multiple_steps_newest_first(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    upgrade(db_conn, migrations)

    assert downgrade(db_conn, migrations, steps=2) == (2, 1)
    assert applied(db_conn) == {}
    assert _table_exists(db_conn, "widgets") is False


def test_downgrade_then_upgrade_round_trips(db_conn: Connection, migrations_dir: Path) -> None:
    migrations = discover(migrations_dir)
    upgrade(db_conn, migrations)
    downgrade(db_conn, migrations, steps=2)

    assert upgrade(db_conn, migrations) == (1, 2)
    assert _table_exists(db_conn, "gadgets")


def test_downgrade_refuses_irreversible_before_reverting_anything(
    db_conn: Connection, tmp_path: Path
) -> None:
    write_migration(tmp_path, 1, "widgets", "CREATE TABLE widgets (id int)")
    write_migration(
        tmp_path, 2, "gadgets", "CREATE TABLE gadgets (id int)", down_sql="DROP TABLE gadgets"
    )
    migrations = discover(tmp_path)
    upgrade(db_conn, migrations)

    with pytest.raises(IrreversibleMigrationError, match="001_widgets"):
        downgrade(db_conn, migrations, steps=2)

    assert _table_exists(db_conn, "gadgets") is True, "a partial downgrade is worse than none"
    assert sorted(applied(db_conn)) == [1, 2]


def test_downgrade_rejects_non_positive_steps(db_conn: Connection, migrations_dir: Path) -> None:
    with pytest.raises(ValueError, match="steps must be >= 1"):
        downgrade(db_conn, discover(migrations_dir), steps=0)


def test_concurrent_upgrades_apply_each_migration_exactly_once(
    db_dsn: str, migrations_dir: Path
) -> None:
    """Two processes booting at once must not both apply the same file."""
    write_migration(migrations_dir, 3, "slow", "SELECT pg_sleep(0.3); CREATE TABLE slow (id int)")
    migrations = discover(migrations_dir)
    results: list[tuple[int, ...]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        try:
            with connect(db_dsn, autocommit=True) as conn:
                barrier.wait(timeout=10)
                results.append(upgrade(conn, migrations))
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    applied_versions = sorted(v for result in results for v in result)
    assert applied_versions == [1, 2, 3], "each migration applied exactly once across both runs"

    with connect(db_dsn) as conn:
        assert sorted(applied(conn)) == [1, 2, 3]


def test_repo_schema_applies_cleanly(db_conn: Connection) -> None:
    """001_schema.sql must apply to an empty Postgres 16 with pgvector available."""
    versions = upgrade(db_conn)

    assert 1 in versions
    for table in (
        "connectors",
        "raw_records",
        "entities",
        "edges",
        "acl_grants",
        "chunks",
        "actions",
    ):
        assert _table_exists(db_conn, table), f"{table} missing after 001_schema.sql"

    with db_conn.cursor() as cur:
        cur.execute("SELECT extname FROM pg_extension WHERE extname = 'vector'")
        assert cur.fetchone() is not None, "pgvector extension not created"
