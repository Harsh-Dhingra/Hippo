"""The hippo-migrate command line."""

import pytest

from core.db import connect
from core.migrate import applied, discover, main

# Derived, not hardcoded, so adding a migration does not break the CLI tests.
REPO_MIGRATIONS = discover()
LATEST_VERSION = max(m.version for m in REPO_MIGRATIONS)

pytestmark = pytest.mark.requires_db


@pytest.fixture(autouse=True)
def _point_cli_at_test_database(db_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HIPPO_DATABASE_URL", db_dsn)
    monkeypatch.setenv("HIPPO_LOG_LEVEL", "WARNING")


def test_status_on_an_empty_database(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 0

    out = capsys.readouterr().out
    assert "current version: none" in out
    assert "applied: 0" in out
    assert "001_schema" in out


def test_up_applies_the_repo_migrations(db_dsn: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["up"]) == 0
    assert f"applied {len(REPO_MIGRATIONS)} migration(s)" in capsys.readouterr().out

    with connect(db_dsn) as conn:
        assert sorted(applied(conn)) == [m.version for m in REPO_MIGRATIONS]


def test_up_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["up"]) == 0
    capsys.readouterr()

    assert main(["up"]) == 0
    assert "applied 0 migration(s)" in capsys.readouterr().out


def test_status_after_up_shows_no_pending(capsys: pytest.CaptureFixture[str]) -> None:
    main(["up"])
    capsys.readouterr()

    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert f"current version: {LATEST_VERSION:03d}" in out
    assert "pending: 0" in out


def test_down_reverts_the_newest_migration(capsys: pytest.CaptureFixture[str]) -> None:
    main(["up"])
    capsys.readouterr()

    assert main(["down"]) == 0
    assert f"reverted 1 migration(s): [{LATEST_VERSION}]" in capsys.readouterr().out


def test_down_past_an_irreversible_migration_reverts_nothing(
    db_dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """002_roles has no down migration on purpose. Asking to go past it must
    fail whole, not leave the schema half way back."""
    main(["up"])
    capsys.readouterr()

    assert main(["down", "--steps", "99"]) == 1
    assert "not reversible" in capsys.readouterr().err

    with connect(db_dsn) as conn:
        assert sorted(applied(conn)) == [m.version for m in REPO_MIGRATIONS]


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["sideways"])


def test_missing_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main([])
