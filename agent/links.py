"""Deep links back to the source system.

A citation that does not resolve is a footnote, not evidence. ARCHITECTURE §3
step 5 makes citations entity ids rendered as links, and this is the rendering.

Building a link needs the connector's kind and its base URL, and the agent's
database role cannot read the connectors table. So the directory is loaded once
by a component that can, and handed to the agent. That is not a hole: nothing
in connectors.config is a secret, because CLAUDE.md forbids putting one there,
and the directory carries no content.

A connector this module does not recognise gets no link rather than a guessed
one. A wrong link is worse than a missing one: it looks like evidence and
leads somewhere else.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from core.db import Connection

LOG = logging.getLogger("hippo.agent.links")


class ConnectorInfo(BaseModel):
    """What link rendering needs to know about a connector."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    kind: str
    config: dict[str, Any]


class ConnectorDirectory(BaseModel):
    """Connector metadata, loaded once rather than per query."""

    model_config = ConfigDict(frozen=True)

    connectors: dict[UUID, ConnectorInfo] = {}

    def get(self, connector_id: UUID | None) -> ConnectorInfo | None:
        if connector_id is None:
            return None
        return self.connectors.get(connector_id)


def load_directory(conn: Connection) -> ConnectorDirectory:
    """Read the connector registry. Run at startup, not per query."""
    with conn.cursor() as cur:
        cur.execute("SELECT id, kind, config FROM connectors")
        rows = cur.fetchall()
    return ConnectorDirectory(
        connectors={
            UUID(str(row[0])): ConnectorInfo(
                id=UUID(str(row[0])), kind=str(row[1]), config=dict(row[2] or {})
            )
            for row in rows
        }
    )


def deep_link(
    connector: ConnectorInfo | None, source_type: str | None, source_id: str | None
) -> str | None:
    """A URL for one source object, or None when one cannot be built."""
    if connector is None or source_type is None or source_id is None:
        return None
    if connector.kind == "slack":
        return _slack_link(connector.config, source_type, source_id)
    if connector.kind == "jira":
        return _jira_link(connector.config, source_type, source_id)
    LOG.debug("no link builder for connector kind", extra={"kind": connector.kind})
    return None


def _slack_link(config: dict[str, Any], source_type: str, source_id: str) -> str | None:
    base = str(config.get("workspace_url") or "").rstrip("/")
    if not base:
        return None
    if source_type == "slack.channel":
        return f"{base}/archives/{source_id}"
    if source_type == "slack.message":
        # Message ids are qualified as channel:ts, and Slack permalinks want
        # the timestamp with its dot removed and a p prefix.
        channel, _, timestamp = source_id.partition(":")
        if not timestamp:
            return None
        return f"{base}/archives/{channel}/p{timestamp.replace('.', '')}"
    return None


def _jira_link(config: dict[str, Any], source_type: str, source_id: str) -> str | None:
    base = str(config.get("base_url") or "").rstrip("/")
    if not base:
        return None
    if source_type in ("jira.project", "jira.issue"):
        # Jira browses both by key, and a project key looks like an issue key
        # without the number.
        return f"{base}/browse/{source_id}"
    if source_type == "jira.comment":
        # Comment ids are qualified as ISSUE:comment_id; Jira anchors the
        # comment on the issue page.
        issue, _, comment = source_id.partition(":")
        if not comment:
            return None
        return f"{base}/browse/{issue}?focusedCommentId={comment}#comment-{comment}"
    return None
