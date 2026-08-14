"""Making a throwaway database, with whatever credentials this Postgres wants.

Three CLIs needed one of these and all three built the DSN by writing
`postgresql://localhost:5432/postgres` into the source. That works on a
developer machine, where local connections are trusted and no password is
needed, and fails everywhere else:

    psycopg.OperationalError: connection failed: connection to server at
    "127.0.0.1", port 5432 failed: fe_sendauth: no password supplied

CI found it on the first run it ever did. It is worth being precise about why
it survived so long: the tests around these CLIs passed locally, and passed for
the same reason the bug was invisible locally. A hardcoded DSN cannot be caught
by a test that runs where the hardcoded DSN happens to work.

So the address comes from the environment, the way the application's does, and
the database name is swapped into it rather than a new string being built —
which is what keeps the user, password, host, port and sslmode that were
already there.
"""

from __future__ import annotations

import os
import uuid

from psycopg.conninfo import conninfo_to_dict, make_conninfo

from core.db import connect

# The eval CLIs are developer tools, so the test DSN is the natural first
# choice: whoever is running these has already set it to run the suite. The
# application's own URL is the fallback, and the local default is last, for a
# machine where neither is set and local connections are trusted.
DSN_VARS = ("HIPPO_TEST_DATABASE_URL", "HIPPO_DATABASE_URL")
LOCAL_DEFAULT = "postgresql://localhost:5432/postgres"


def admin_dsn() -> str:
    """A DSN with rights to create databases."""
    for var in DSN_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return LOCAL_DEFAULT


def with_dbname(dsn: str, dbname: str) -> str:
    """The same connection, pointed at a different database.

    Parsed and rebuilt rather than string-formatted, so every other parameter
    survives. A password in the DSN is the whole point of this function.
    """
    params: dict[str, str] = {
        key: str(value) for key, value in conninfo_to_dict(dsn).items() if value is not None
    }
    params["dbname"] = dbname
    return make_conninfo(**params)


def scratch_database(prefix: str) -> str:
    """Create an empty database and return a DSN for it.

    Left behind on purpose: a run whose numbers looked wrong is a run somebody
    will want to poke at afterwards.
    """
    admin = admin_dsn()
    name = f"{prefix}_{uuid.uuid4().hex[:8]}"
    with connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    return with_dbname(admin, name)
