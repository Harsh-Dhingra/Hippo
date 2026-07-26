"""Which connectors exist, and how to build one.

Before v1 this was a chain of `if kind == "slack" ... if kind == "jira"` inside
the sync worker, and `isinstance(connector, JiraConnector)` to decide whether
write-back was possible. Both work fine for connectors that live in this
repository and neither works at all for one that does not — which makes them
the thing standing between the SDK and its own stated goal.

**A connector is registered, not recognised.** Built-ins register on import.
Anything installed alongside Hippo registers through the `hippo.connectors`
entry-point group, so a third-party package is discovered rather than merged:

    [project.entry-points."hippo.connectors"]
    github = "hippo_github:PLUGIN"

**Credentials still come from the environment, never the registry.** A plugin
declares how to build a connector given config and a token; it never holds one,
and the registry is safe to read from any process — including the API, which
needs to know what actions exist and must never hold a source-system token.

**A plugin that will not run is refused at registration.** A connector built
against SDK 2.0 fails here, once, with its name in the message, rather than
part-way through a sync with an AttributeError.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from importlib.metadata import entry_points
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from sync.connectors.sdk import (
    ActionDefinition,
    Capabilities,
    IncompatibleConnectorError,
    ReadConnector,
    WritebackConnector,
)

LOG = logging.getLogger("hippo.connectors.registry")

ENTRY_POINT_GROUP = "hippo.connectors"

# What a plugin is handed to build a connector: the connector row's config, and
# the credential the worker resolved from the environment. Nothing else — a
# builder that wanted a database connection would be reaching past the contract.
Builder = Callable[[Mapping[str, Any], str], ReadConnector]
WritebackBuilder = Callable[[Mapping[str, Any], str], WritebackConnector]


class MissingConfigError(Exception):
    """A connector row lacks something its plugin needs, named so it can be fixed."""


class ConnectorPlugin(BaseModel):
    """One connector implementation, and what it needs to run."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    kind: str = Field(min_length=1)
    display_name: str = Field(min_length=1)
    capabilities: Capabilities
    build: Builder
    # Optional, for a connector that keeps its write path in its own class. The
    # SDK has separate protocols for read and write-back, so a contributor may
    # reasonably split them — and if the only supported shape were one object
    # doing both, that split would be a protocol nobody could actually use.
    # Absent means the read connector performs its own write-backs, which is
    # what Jira does.
    build_writeback: WritebackBuilder | None = None
    # Config keys the plugin cannot run without. Checked before building, so a
    # missing base_url is a clear message at registration time rather than a
    # KeyError inside somebody's transport.
    requires_config: tuple[str, ...] = ()

    def _check_config(self, config: Mapping[str, Any]) -> None:
        missing = [key for key in self.requires_config if not config.get(key)]
        if missing:
            raise MissingConfigError(
                f"{self.kind} connector needs {', '.join(missing)} in its config"
            )

    def connector(self, config: Mapping[str, Any], token: str) -> ReadConnector:
        self._check_config(config)
        return self.build(config, token)

    def writer(self, config: Mapping[str, Any], token: str) -> WritebackConnector:
        """The half that performs actions, however this connector is arranged."""
        if not self.capabilities.supports_writeback:
            raise LookupError(f"the {self.kind} connector is read-only")
        self._check_config(config)
        if self.build_writeback is not None:
            return self.build_writeback(config, token)
        return as_writeback(self.build(config, token), self.kind)


_REGISTRY: dict[str, ConnectorPlugin] = {}
_LOADED_ENTRY_POINTS = False


def register(plugin: ConnectorPlugin, *, replace: bool = False) -> None:
    """Add a connector to the registry.

    Refuses a silent overwrite. Two packages claiming the same kind is a
    deployment mistake whose symptom would otherwise be one of them quietly not
    running, which is exactly the failure nobody notices.
    """
    plugin.capabilities.require_compatible()
    if plugin.capabilities.kind != plugin.kind:
        raise ValueError(
            f"plugin {plugin.kind!r} declares capabilities for {plugin.capabilities.kind!r}"
        )
    existing = _REGISTRY.get(plugin.kind)
    if existing is not None and not replace:
        raise ValueError(f"a connector for {plugin.kind!r} is already registered")
    _REGISTRY[plugin.kind] = plugin
    LOG.info(
        "connector registered",
        extra={
            "kind": plugin.kind,
            "actions": sorted(plugin.capabilities.action_types),
            "sdk": plugin.capabilities.sdk_version,
        },
    )


def _load_entry_points() -> None:
    """Discover connectors installed alongside Hippo.

    One bad plugin does not stop the others. A package that fails to import is
    logged with its name and skipped, because the alternative is that installing
    a broken third-party connector takes the whole sync worker down — and the
    person who installed it is not necessarily the person on call.
    """
    global _LOADED_ENTRY_POINTS
    if _LOADED_ENTRY_POINTS:
        return
    _LOADED_ENTRY_POINTS = True

    for entry in entry_points(group=ENTRY_POINT_GROUP):
        try:
            plugin = entry.load()
        except Exception as exc:
            LOG.error(
                "could not load a connector plugin",
                extra={"entry_point": entry.name, "error": str(exc)[:200]},
            )
            continue
        if not isinstance(plugin, ConnectorPlugin):
            LOG.error(
                "entry point did not provide a ConnectorPlugin",
                extra={"entry_point": entry.name, "got": type(plugin).__name__},
            )
            continue
        try:
            register(plugin)
        except (ValueError, IncompatibleConnectorError) as exc:
            LOG.error(
                "refused a connector plugin",
                extra={"entry_point": entry.name, "reason": str(exc)},
            )


def available() -> dict[str, ConnectorPlugin]:
    """Every registered connector, built-ins and installed packages alike."""
    _load_entry_points()
    return dict(_REGISTRY)


def plugin_for(kind: str) -> ConnectorPlugin:
    _load_entry_points()
    found = _REGISTRY.get(kind)
    if found is None:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise LookupError(f"no connector implementation for {kind!r}; registered: {known}")
    return found


def action_definitions() -> dict[str, ActionDefinition]:
    """The write-back vocabulary, assembled from what connectors declare.

    This is what the agent is allowed to propose. It is closed — content cannot
    add to it — but it is no longer a literal, so a connector shipped in another
    package can contribute an action instead of being read-only forever.
    """
    definitions: dict[str, ActionDefinition] = {}
    for plugin in available().values():
        for action in plugin.capabilities.actions:
            if action.action_type in definitions:
                LOG.warning(
                    "two connectors declare the same action",
                    extra={"action": action.action_type, "kind": plugin.kind},
                )
                continue
            definitions[action.action_type] = action
    return definitions


def supports_writeback(kind: str) -> bool:
    """Declared by the connector rather than inferred from its class."""
    return plugin_for(kind).capabilities.supports_writeback


def as_writeback(connector: ReadConnector, kind: str) -> WritebackConnector:
    """Narrow a connector to its write-back half, or refuse.

    Two checks rather than one, because they fail for different reasons and a
    reader deserves to know which: the plugin may not claim write-back at all,
    or it may claim it and not implement it, which is a bug in the connector
    rather than a configuration mistake.
    """
    if not supports_writeback(kind):
        raise LookupError(f"the {kind} connector is read-only")
    missing = [
        name for name in ("capture_inverse", "execute", "rollback") if not hasattr(connector, name)
    ]
    if missing:
        raise LookupError(
            f"the {kind} connector declares write-back but is missing {', '.join(missing)}"
        )
    return connector  # type: ignore[return-value]


def reset_for_tests() -> None:
    """Empty the registry. Only tests call this."""
    global _LOADED_ENTRY_POINTS
    _REGISTRY.clear()
    _LOADED_ENTRY_POINTS = False
