"""Discovery: reading the migrations directory, and refusing ambiguity."""

from pathlib import Path

import pytest

from core.migrate import (
    MIGRATIONS_DIR,
    IrreversibleMigrationError,
    MigrationDiscoveryError,
    discover,
)
from tests.conftest import write_migration


def test_orders_by_version(tmp_path: Path) -> None:
    write_migration(tmp_path, 2, "roles", "SELECT 2")
    write_migration(tmp_path, 10, "jobs", "SELECT 10")
    write_migration(tmp_path, 1, "schema", "SELECT 1")

    assert [m.version for m in discover(tmp_path)] == [1, 2, 10]
    assert [m.name for m in discover(tmp_path)] == ["schema", "roles", "jobs"]


def test_pairs_down_migrations(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "schema", "SELECT 1")
    write_migration(tmp_path, 2, "roles", "SELECT 2", down_sql="SELECT -2")

    schema, roles = discover(tmp_path)
    assert schema.reversible is False
    assert roles.reversible is True
    assert roles.down_sql() == "SELECT -2"


def test_down_sql_on_irreversible_migration_raises(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "schema", "SELECT 1")
    (migration,) = discover(tmp_path)
    with pytest.raises(IrreversibleMigrationError, match="001_schema"):
        migration.down_sql()


def test_checksum_tracks_content(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "schema", "SELECT 1")
    before = discover(tmp_path)[0].checksum

    write_migration(tmp_path, 1, "schema", "SELECT 2")
    after = discover(tmp_path)[0].checksum

    assert before != after
    assert len(before) == 64


def test_ignores_non_sql_files(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "schema", "SELECT 1")
    (tmp_path / "README.md").write_text("notes", encoding="utf-8")
    (tmp_path / "001_schema.sql.bak").write_text("SELECT 1", encoding="utf-8")

    assert len(discover(tmp_path)) == 1


def test_rejects_unparseable_filename(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "add-roles.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(MigrationDiscoveryError, match="does not match"):
        discover(tmp_path)


@pytest.mark.parametrize(
    "filename",
    ["1_schema.sql", "0001_schema.sql", "001_Schema.sql", "001-schema.sql", "001_schema.SQL"],
)
def test_rejects_filenames_that_look_close_but_are_not(tmp_path: Path, filename: str) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / filename).write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(MigrationDiscoveryError):
        discover(tmp_path)


def test_rejects_duplicate_version(tmp_path: Path) -> None:
    write_migration(tmp_path, 1, "schema", "SELECT 1")
    write_migration(tmp_path, 1, "roles", "SELECT 2")

    with pytest.raises(MigrationDiscoveryError, match="duplicate migration version"):
        discover(tmp_path)


def test_rejects_orphan_down_migration(tmp_path: Path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "003_ghost.down.sql").write_text("SELECT 1", encoding="utf-8")

    with pytest.raises(MigrationDiscoveryError, match="no matching up migration"):
        discover(tmp_path)


def test_rejects_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(MigrationDiscoveryError, match="not found"):
        discover(tmp_path / "nope")


def test_repo_migrations_directory_is_well_formed() -> None:
    """The real directory must always parse; this catches a bad filename at PR time."""
    migrations = discover(MIGRATIONS_DIR)
    assert migrations, "expected at least 001_schema.sql"
    assert [m.version for m in migrations] == sorted(m.version for m in migrations)
    assert migrations[0].version == 1
    assert migrations[0].name == "schema"
