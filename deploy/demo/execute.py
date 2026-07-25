"""Run the write-back executor over whatever has been approved.

The sync worker's job, invoked directly so the demo does not have to wait for a
scheduler tick. It is the only component holding a Jira credential — here that
is the fixture transport, so the "comment" lands in memory rather than in a real
Jira, and the rollback deletes it from the same place.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import UUID

from core.db import Connection, connect
from sync.connectors.jira import FixtureTransport, JiraConnector
from sync.writeback import due_actions, execute_action

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "jira"


def main(dsn: str) -> None:
    transport = FixtureTransport(FIXTURES)

    def factory(_conn: Connection, _connector_id: UUID) -> JiraConnector:
        return JiraConnector(transport)

    with connect(dsn) as conn:
        pending = due_actions(conn)
        for action_id in pending:
            outcome = execute_action(conn, action_id, factory)
            print(f"  {action_id}: {outcome}")
        conn.commit()
    print(f"executed {len(pending)} action(s)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "postgresql://localhost:5432/hippo_demo")
