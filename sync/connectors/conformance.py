"""Checking a connector from the command line.

The harness has been usable from pytest since P1. That is not enough for a
connector living in somebody else's repository: they would have to adopt our
test layout, our fixtures and our conftest to find out whether their cursor
resumes correctly. So the same checks are a command.

    hippo-conformance my_package:build_connector
    hippo-conformance my_package:build_connector --writeback my_package:build_writer
    hippo-conformance --list

The argument is `module:attribute`, and the attribute may be a connector or a
callable returning one. Exit code 0 means conforming, 1 means it is not, 2 means
the argument could not be resolved — three states rather than two, because "your
connector is broken" and "I could not find your connector" want different
reactions from whoever is reading CI output.

**Run this against fixtures, not a live source.** Determinism and resume are
checked by syncing the same source repeatedly, so a live system that changes
mid-run will report violations it does not have. Every check here is designed
to run offline, which is also what makes it usable in a pull request.
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any

from sync.connectors.harness import check_connector
from sync.connectors.registry import available
from sync.connectors.sdk import SDK_VERSION, ReadConnector, WritebackConnector


class ResolutionError(Exception):
    """The `module:attribute` argument did not name something usable."""


def resolve(target: str) -> Any:
    """Turn `module:attribute` into a connector instance.

    Three shapes are accepted because all three are things people naturally
    point at: a class, a zero-argument factory, and an already-built instance.
    A class has to be told apart from an instance rather than tested for
    `identities`, since a class carries its methods as attributes too and would
    otherwise be handed to the harness unconstructed.

    Anything callable is called with no arguments, so a connector needing
    credentials should be wrapped in a factory that supplies a fixture
    transport. That is deliberate: this command must never be the thing that
    prompts somebody to put a live token on a command line.
    """
    if ":" not in target:
        raise ResolutionError(f"{target!r} is not module:attribute")
    module_name, _, attribute = target.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ResolutionError(f"could not import {module_name!r}: {exc}") from exc
    try:
        found = getattr(module, attribute)
    except AttributeError as exc:
        raise ResolutionError(f"{module_name!r} has no {attribute!r}") from exc
    if isinstance(found, type) or (callable(found) and not hasattr(found, "identities")):
        try:
            return found()
        except TypeError as exc:
            raise ResolutionError(
                f"{target!r} could not be built with no arguments: {exc}. "
                "Point at a zero-argument factory that supplies a fixture transport."
            ) from exc
    return found


def describe_registry() -> str:
    """What is installed, which is the first thing to check when one is missing."""
    plugins = available()
    if not plugins:
        return "no connectors are registered"
    lines = [f"SDK {SDK_VERSION}", ""]
    for kind, plugin in sorted(plugins.items()):
        actions = ", ".join(sorted(plugin.capabilities.action_types)) or "read-only"
        lines.append(
            f"  {kind:<12} {plugin.display_name:<16} "
            f"sdk={plugin.capabilities.sdk_version} schema="
            f"{plugin.capabilities.schema_version}  {actions}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-conformance",
        description="Check a connector against the Hippo SDK contract.",
    )
    parser.add_argument(
        "connector",
        nargs="?",
        help="module:attribute naming a connector or a zero-argument factory",
    )
    parser.add_argument(
        "--writeback",
        help="module:attribute for the write-back half, when it is a separate object",
    )
    parser.add_argument(
        "--list", action="store_true", help="show the registered connectors and exit"
    )
    args = parser.parse_args(argv)

    if args.list:
        print(describe_registry())
        return 0

    if not args.connector:
        parser.print_help()
        return 2

    try:
        connector: ReadConnector = resolve(args.connector)
        writeback: WritebackConnector | None = resolve(args.writeback) if args.writeback else None
    except ResolutionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    # A connector that implements write-back on the same object should be
    # checked as one, so the declaration and the methods are compared. Passing
    # --writeback is only for the split case.
    if writeback is None and hasattr(connector, "execute"):
        writeback = connector  # type: ignore[assignment]

    violations = check_connector(connector, writeback=writeback)
    kind = getattr(connector, "kind", type(connector).__name__)

    if not violations:
        print(f"{kind}: conforms to SDK {SDK_VERSION}")
        return 0

    print(f"{kind}: {len(violations)} violation(s) against SDK {SDK_VERSION}", file=sys.stderr)
    for violation in violations:
        print(f"  {violation}", file=sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
