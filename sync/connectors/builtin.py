"""The connectors that ship with Hippo, registered the same way any other is.

Deliberately no shortcut. If the built-ins used a private path and third-party
connectors used the entry-point registry, the registry would be the untested
one — and the first person to find out would be a contributor whose connector
does not load, with nothing to compare against.

So Slack and Jira go through `register()` exactly as an installed package does,
which means the path a stranger's connector takes is the path this project's
own two take every time it starts.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sync.connectors.jira.actions import ACTIONS as JIRA_ACTIONS
from sync.connectors.jira.connector import JiraConnector
from sync.connectors.jira.transport import HttpTransport as JiraHttp
from sync.connectors.registry import ConnectorPlugin, register
from sync.connectors.sdk import Capabilities, ReadConnector
from sync.connectors.slack.connector import SlackConnector
from sync.connectors.slack.transport import HttpTransport as SlackHttp

SLACK_SCHEMA_VERSION = "2026-07-01"
JIRA_SCHEMA_VERSION = "2026-07-01"


def _build_slack(config: Mapping[str, Any], token: str) -> ReadConnector:
    return SlackConnector(SlackHttp(token))


def _build_jira(config: Mapping[str, Any], token: str) -> ReadConnector:
    # The email is part of Jira's basic-auth pair, not a second secret: the
    # token is the credential, and the address it belongs to is configuration.
    email = str(config.get("email") or "")
    return JiraConnector(JiraHttp(str(config["base_url"]), email, token))


SLACK = ConnectorPlugin(
    kind="slack",
    display_name="Slack",
    capabilities=Capabilities(
        kind="slack",
        schema_version=SLACK_SCHEMA_VERSION,
        actions=(),
    ),
    build=_build_slack,
)

JIRA = ConnectorPlugin(
    kind="jira",
    display_name="Jira",
    capabilities=Capabilities(
        kind="jira",
        schema_version=JIRA_SCHEMA_VERSION,
        actions=JIRA_ACTIONS,
    ),
    build=_build_jira,
    # Named here so a Jira row without a site URL is a clear message rather
    # than a KeyError inside the transport.
    requires_config=("base_url", "email"),
)

PLUGINS = (SLACK, JIRA)


def register_builtins() -> None:
    """Register without going through entry points.

    Only for a source checkout that was never installed, and for tests that
    empty the registry. Normal operation discovers these two exactly the way it
    discovers anyone else's, which is the point of declaring them in
    pyproject.toml rather than importing them here.
    """
    for plugin in PLUGINS:
        register(plugin, replace=True)
