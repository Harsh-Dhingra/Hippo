"""Secrets handling, as a gate rather than a review (P2-SEC-1).

CLAUDE.md says no secrets anywhere in the repo or the database, environment and
mounted secrets only. That was a sentence and a habit. These are the mechanisms
that make it a rule, and the review that found what needed one.

Three places a credential can end up by accident, in order of how easy it is
to do without noticing:

1. **The database**, because config is a jsonb column and putting a token in it
   works. Migration 013 refuses it.
2. **A log line or an error**, because a connection string contains a password
   and something eventually formats one.
3. **The repository**, because a working example gets committed.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import errors
from pydantic import SecretStr

from core.db import Connection

pytestmark = pytest.mark.requires_db

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# The database refuses to hold one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "config",
    [
        '{"token": "xoxb-real-looking-token"}',
        '{"api_key": "sk-abc123"}',
        '{"apiKey": "sk-abc123"}',
        '{"client_secret": "shh"}',
        '{"password": "hunter2"}',
        '{"private_key": "-----BEGIN"}',
        '{"refresh_token": "1//abc"}',
        '{"base_url": "https://acme.atlassian.net", "bearer": "abc"}',
        '{"CREDENTIAL": "abc"}',
    ],
)
def test_a_connector_cannot_store_a_credential(migrated: Connection, config: str) -> None:
    """The rule, enforced where a habit cannot be forgotten. A token in a jsonb
    column is a token in every backup, every replica, and every pg_dump someone
    pastes into an issue."""
    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            "INSERT INTO connectors (kind, display_name, config) VALUES ('slack', 'S', %s)",
            (config,),
        )
    migrated.rollback()


def test_it_cannot_be_added_by_an_update_either(migrated: Connection) -> None:
    """A CHECK that only ran on insert would be a CHECK with an obvious way
    around it."""
    connector_id = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'S')",
        (connector_id,),
    )

    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            'UPDATE connectors SET config = config || \'{"token": "xoxb"}\'::jsonb WHERE id = %s',
            (connector_id,),
        )
    migrated.rollback()


@pytest.mark.parametrize(
    "config",
    [
        '{"workspace_url": "https://acme.slack.com"}',
        '{"base_url": "https://acme.atlassian.net", "email": "bot@acme.com"}',
        '{"project_keys": ["ACME", "PUB"]}',
        "{}",
    ],
)
def test_ordinary_config_is_unaffected(migrated: Connection, config: str) -> None:
    """A rule that blocked real configuration would be routed around within a
    week."""
    migrated.execute(
        "INSERT INTO connectors (kind, display_name, config) VALUES ('slack', 'S', %s)",
        (config,),
    )


def test_the_check_is_about_key_names_not_values(migrated: Connection) -> None:
    """Recognising a secret by its shape is guesswork that fails open. A key
    called 'token' is unambiguous; a value that looks random is not."""
    migrated.execute(
        "INSERT INTO connectors (kind, display_name, config) VALUES "
        "('slack', 'S', '{\"workspace_url\": \"xoxb-this-looks-like-a-token\"}')"
    )


# ---------------------------------------------------------------------------
# Nothing prints one.
# ---------------------------------------------------------------------------


def test_the_settings_object_does_not_print_its_secrets() -> None:
    """Settings gets logged, put in error messages, and shown in a debugger.
    A repr that spelled out the database password would put it in all three."""
    from core.config import Settings

    settings = Settings(
        database_url="postgresql://user:hunter2@localhost:5432/hippo",
        model_api_key=SecretStr("sk-secret"),
        embedding_api_key=SecretStr("sk-also-secret"),
    )

    rendered = f"{settings!r} {settings!s}"

    assert "hunter2" not in rendered
    assert "sk-secret" not in rendered
    assert "sk-also-secret" not in rendered


def test_a_connection_failure_does_not_quote_the_password() -> None:
    """The most common way a credential reaches a log: a DSN in an exception."""
    import psycopg

    from core.db import connect

    with (
        pytest.raises(psycopg.OperationalError) as caught,
        connect("postgresql://user:hunter2@127.0.0.1:1/nope", autocommit=True),
    ):
        pass

    assert "hunter2" not in str(caught.value)


# ---------------------------------------------------------------------------
# Nothing is committed.
# ---------------------------------------------------------------------------

# Deliberately narrow: patterns for credentials that are unambiguous on sight.
# A broad entropy scan produces enough false positives to be switched off, and
# a gate that gets switched off is worse than none.
SECRET_SHAPES = (
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"sk-ant-[A-Za-z0-9-]{10,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"postgres(?:ql)?://[^\s:/]+:[^\s@]{6,}@"),
)

# The corpus contains strings that are meant to look like credentials, because
# an attack that mentions a token is a shape worth testing. Naming the file is
# how an allowance stays visible instead of becoming a hole.
ALLOWED = {
    # Attack text that is meant to look like a credential; a shape worth testing.
    "tests/fixtures/injection/corpus.json",
    # This file, which quotes the patterns it looks for.
    "tests/test_secrets.py",
    # Default local-development credentials, overridable by environment and
    # published in the compose file precisely so nobody has to invent one.
    # Naming them here keeps the allowance visible instead of weakening the
    # pattern for everything.
    "deploy/compose.yaml",
    ".github/workflows/ci.yaml",
}


def tracked_files() -> list[Path]:
    listing = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return [ROOT / line for line in listing.stdout.splitlines() if line]


def test_no_credential_is_committed() -> None:
    """Runs over what git tracks rather than what is on disk, so a local .env
    is not a failure and a committed one is."""
    findings: list[str] = []

    for path in tracked_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in ALLOWED or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_SHAPES:
            match = pattern.search(text)
            if match:
                findings.append(f"{relative}: {pattern.pattern}")

    assert findings == [], f"credential-shaped strings in tracked files: {findings}"


def test_no_env_file_is_tracked() -> None:
    """The single most common way a working example becomes a leak."""
    tracked = {path.relative_to(ROOT).as_posix() for path in tracked_files()}

    leaked = {
        name
        for name in tracked
        if Path(name).name in {".env", ".env.local", ".env.production", "secrets.yaml"}
    }
    assert leaked == set(), leaked


def test_the_sample_env_carries_no_real_values() -> None:
    """A sample file exists to be copied, so anything in it will end up in a
    real deployment if it looks usable."""
    samples = [
        path
        for path in tracked_files()
        if path.is_file() and path.name in {".env.example", ".env.sample", "env.sample"}
    ]

    for path in samples:
        text = path.read_text(encoding="utf-8")
        for pattern in SECRET_SHAPES:
            assert pattern.search(text) is None, path


def test_gitignore_covers_the_obvious_ones() -> None:
    """Belt as well as braces: the scan above catches a committed secret, this
    stops the commit from being easy in the first place."""
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert ".env" in ignored
