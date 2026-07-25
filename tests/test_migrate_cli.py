"""The hippo-migrate command line."""

import pytest

from core.db import connect
from core.migrate import applied, main

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
    assert "applied 1 migration(s)" in capsys.readouterr().out

    with connect(db_dsn) as conn:
        assert 1 in applied(conn)


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
    assert "current version: 001" in out
    assert "pending: 0" in out


def test_down_on_an_irreversible_migration_exits_nonzero(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["up"])
    capsys.readouterr()

    assert main(["down"]) == 1
    assert "not reversible" in capsys.readouterr().err


def test_unknown_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main(["sideways"])


def test_missing_command_is_rejected() -> None:
    with pytest.raises(SystemExit):
        main([])
