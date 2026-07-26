"""Generating a connector that already works.

The guide in docs/CONNECTORS.md says a connector is a weekend of work. That is
only true if the weekend starts from something that runs, so this writes a
package that passes conformance before a single line of it has been changed:

    hippo-new-connector github --out ~/src

What it emits is not a stub with TODOs where the hard parts go. It is a
working fixture-backed connector with the cursor contract implemented, an ACL
stream that grants on containers rather than objects, a write-back with inverse
capture, and a test that runs the conformance suite. The contributor's job is
to replace the fixture transport with a real one, which is the part only they
can do.

**The generated package depends on hippo, not the other way round.** It gets
its own pyproject with a `hippo.connectors` entry point, so it installs
alongside Hippo and is discovered rather than merged. Nothing in this
repository has to change for it to work, which is the whole claim of the SDK
and is worth making literally true from the first generated file.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from sync.connectors.sdk import SDK_VERSION

# A connector kind becomes a Python package name, a class name and a source-type
# prefix, so it has to survive all three.
VALID_KIND = re.compile(r"^[a-z][a-z0-9_]{1,30}$")


class ScaffoldError(Exception):
    """The name or the destination will not work, said before anything is written."""


def class_name(kind: str) -> str:
    return "".join(part.capitalize() for part in kind.split("_"))


PYPROJECT = """\
[project]
name = "hippo-{kind}"
version = "0.1.0"
description = "A {kind} connector for Hippo."
requires-python = ">=3.12"
dependencies = ["hippo", "pydantic>=2.10", "httpx>=0.28"]

# How Hippo finds this connector. Nothing in Hippo has to change: install this
# package alongside it and `hippo-conformance --list` will show {kind}.
[project.entry-points."hippo.connectors"]
{kind} = "hippo_{kind}:PLUGIN"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["hippo_{kind}"]
"""

INIT = '''\
"""A {kind} connector for Hippo, built against SDK {sdk}."""

from hippo_{kind}.connector import {cls}Connector, {cls}Transport, FixtureTransport
from hippo_{kind}.plugin import PLUGIN

__all__ = ["PLUGIN", "{cls}Connector", "{cls}Transport", "FixtureTransport"]
'''

CONNECTOR = '''\
"""Reading {kind} through a transport.

Two rules from the SDK shape everything here, and both are worth keeping in
mind while replacing the fixture transport with a real one.

**A connector never touches the database.** It yields records; the sync runtime
persists them. That is what lets this file be tested with no Postgres and no
network, and it keeps every permission-relevant write in one place.

**Payloads are verbatim.** Each record carries the source object unmodified, so
a field this connector has never heard of is stored rather than dropped. Schema
drift costs a log line, never a record.

The cursor contract is the part that is easy to get wrong and expensive to get
wrong. `stream({{}})` must yield everything; `stream(page.cursor)` must yield
exactly what follows that page. Yield too little and the records in between are
lost forever, and nothing will tell you.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from sync.connectors.sdk import (
    DONE,
    AclRecord,
    Capabilities,
    ContentRecord,
    Cursor,
    IdentityRecord,
    InverseCaptureError,
    Page,
    SourceRef,
    WritebackReceipt,
    WritebackRequest,
    is_terminal,
)

from hippo_{kind}.actions import ACTIONS, COMMENT_ACTION

# Source types. Prefixed with the connector kind so two connectors can never
# collide, and used by ActionDefinition.targets to say what an action may aim at.
CONTAINER = "{kind}.space"
OBJECT = "{kind}.item"


class {cls}Transport(Protocol):
    """Everything that touches the network, behind one method.

    Separated so the connector can be developed and tested against fixtures,
    which is what the SDK means by "fixtures before live". Replace this with
    an httpx client and the connector above it does not change.
    """

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any: ...


class FixtureTransport:
    """Reads JSON files instead of a network. Ships so the tests run offline."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        return json.loads((self._root / f"{{path}}.json").read_text(encoding="utf-8"))


class {cls}Connector:
    """The four streams."""

    kind = "{kind}"
    # Bump when the *source system's* payload shape changes, not when this file
    # does. It is the baseline drift detection compares against.
    schema_version = "2026-01-01"

    def __init__(self, transport: {cls}Transport, *, page_size: int = 50) -> None:
        self._transport = transport
        self._page_size = max(1, page_size)

    def capabilities(self) -> Capabilities:
        """What this connector supports, declared rather than discovered.

        Return `actions=()` for a read-only connector. Declaring an action you
        cannot perform means an approved action fails at execution, after a
        person has read it and clicked approve.
        """
        return Capabilities(
            kind=self.kind,
            schema_version=self.schema_version,
            actions=ACTIONS,
        )

    # -- the cursor ---------------------------------------------------------

    def _page(self, records: Sequence[Any], cursor: Cursor) -> Iterator[Page[Any]]:
        """Offset paging over an already-fetched list.

        A listing stream: it walks everything and then has nowhere further to
        go, so its final cursor means *finished* rather than *resume here*.
        Resuming from it yields one empty final page, which is what keeps the
        resume contract exact — see `is_terminal` in the SDK.

        A watermark stream (an updated-since API) should store the watermark
        instead and never set DONE.
        """
        if is_terminal(cursor):
            yield Page(records=(), cursor=dict(cursor))
            return

        offset = int(cursor.get("offset", 0))
        while True:
            batch = tuple(records[offset : offset + self._page_size])
            offset += len(batch)
            done = offset >= len(records)
            yield Page(
                records=batch,
                cursor={{DONE: True}} if done else {{"offset": offset}},
                has_more=not done,
            )
            if done:
                return

    # -- identities ---------------------------------------------------------

    def identities(self, cursor: Cursor) -> Iterator[Page[IdentityRecord]]:
        """Who exists. Becomes principals.

        `member_of` carries the *source ids* of groups, because a connector
        never sees an internal id. The runtime turns them into memberships once
        both sides exist.
        """
        people = [
            IdentityRecord(
                kind="user",
                source_id=str(row["id"]),
                email=row.get("email"),
                display_name=row.get("name"),
                member_of=tuple(str(g) for g in row.get("groups", ())),
                payload=dict(row),
            )
            for row in self._transport.get("users")
        ]
        yield from self._page(people, cursor)

    # -- content ------------------------------------------------------------

    def content(self, cursor: Cursor) -> Iterator[Page[ContentRecord]]:
        """What was said. Becomes raw_records.

        `container` matters more than it looks. Source systems grant access to
        containers, not to individual objects — a channel is shared, not each
        of its ten thousand messages. Declaring it lets the ACL stream emit one
        record per container instead of one per object, and gives the resolver
        the containment edge it needs anyway.
        """
        items = [
            ContentRecord(
                source_type=OBJECT,
                source_id=str(row["id"]),
                payload=dict(row),
                container=SourceRef(source_type=CONTAINER, source_id=str(row["space_id"])),
            )
            for row in self._transport.get("items")
        ]
        yield from self._page(items, cursor)

    # -- acls ---------------------------------------------------------------

    def acls(self, cursor: Cursor) -> Iterator[Page[AclRecord]]:
        """Who can see what, as the source system states it.

        There is no deny record. Deny-by-default is the runtime's rule, so a
        grant that stops being emitted stops existing — which is what makes
        revocation a plain diff and lets it propagate in minutes.
        """
        grants = [
            AclRecord(
                target=SourceRef(source_type=CONTAINER, source_id=str(row["space_id"])),
                principal_source_id=str(principal),
            )
            for row in self._transport.get("spaces")
            for principal in row.get("members", ())
        ]
        yield from self._page(grants, cursor)

    # -- writeback ----------------------------------------------------------

    def capture_inverse(self, request: WritebackRequest) -> dict[str, Any]:
        """Read the target's current state, before changing it.

        Raise InverseCaptureError if it cannot be read. Do not return an empty
        inverse to get past this check: an action with no rollback path must
        fail rather than execute unrecoverably.
        """
        if request.target is None:
            raise InverseCaptureError("write-back needs a target")
        return {{"comments": list(self._comments(request.target))}}

    def execute(self, request: WritebackRequest) -> WritebackReceipt:
        if request.action_type != COMMENT_ACTION:
            raise InverseCaptureError(f"{kind} cannot perform {{request.action_type!r}}")
        # Replace with the real call. The receipt is what the approval screen
        # shows afterwards, so return the source system's own identifier.
        return WritebackReceipt(external_id="replace-me", result=dict(request.payload))

    def rollback(self, request: WritebackRequest, inverse: Mapping[str, Any]) -> None:
        """Put the target back the way capture_inverse found it."""

    def _comments(self, target: SourceRef) -> tuple[str, ...]:
        return ()
'''

ACTIONS_MODULE = '''\
"""What this connector can be asked to do.

Its own module, with no transport import, because two processes need it: the
sync worker to execute, and the agent to know what may be proposed at all. The
agent must never import a module that can hold a credential.

Payload models forbid extra fields. That is what stops a proposal carrying
something this connector would pass through to the source system unexamined,
and the conformance suite checks it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from sync.connectors.sdk import ActionDefinition

COMMENT_ACTION = "{kind}.comment"


class CommentPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    body: str = Field(min_length=1, max_length=32_000)


ACTIONS: tuple[ActionDefinition, ...] = (
    ActionDefinition(
        action_type=COMMENT_ACTION,
        description="Add a comment to a {kind} item.",
        # What this may be aimed at, in source terms. Without it a proposal
        # could name any retrieved chunk and the write-back would have to guess.
        targets=frozenset({{"{kind}.item"}}),
        payload_schema='{{"body": "the comment text"}}',
        payload_model=CommentPayload,
    ),
)
'''

PLUGIN = '''\
"""How Hippo builds this connector.

The plugin never holds a credential. It says how to build a connector given the
connector row's config and the token the worker resolved from the environment,
which is what keeps the registry safe to read from a process that must never
hold one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sync.connectors.registry import ConnectorPlugin
from sync.connectors.sdk import Capabilities, ReadConnector

from hippo_{kind}.actions import ACTIONS
from hippo_{kind}.connector import {cls}Connector


def build(config: Mapping[str, Any], token: str) -> ReadConnector:
    # Replace FixtureTransport with an HTTP transport built from config and
    # token. Keep the credential out of `config` — it comes from the
    # environment, and connectors.config holds no tokens.
    from pathlib import Path

    from hippo_{kind}.connector import FixtureTransport

    return {cls}Connector(FixtureTransport(Path(str(config["fixtures"]))))


PLUGIN = ConnectorPlugin(
    kind="{kind}",
    display_name="{cls}",
    capabilities=Capabilities(
        kind="{kind}",
        schema_version={cls}Connector.schema_version,
        actions=ACTIONS,
    ),
    build=build,
    # Config keys this connector cannot run without, named here so a missing
    # one is a clear message rather than a KeyError inside the transport.
    requires_config=("fixtures",),
)
'''

TEST = '''\
"""Conformance, which is the test worth having first.

Every check here corresponds to a way a connector silently loses or duplicates
data. A cursor that does not resume exactly re-syncs the world every time or,
worse, skips the records between the page it crashed on and the page it resumes
at. Neither failure shows up in a demo, and neither is caught by a test that
only asserts the happy path returns something.

Run against fixtures, never a live source: determinism and resume are checked
by syncing the same source repeatedly, so anything changing underneath will
report violations that are not there.
"""

from __future__ import annotations

from pathlib import Path

from sync.connectors.harness import assert_conforms, check_connector

from hippo_{kind} import {cls}Connector, FixtureTransport

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def build() -> {cls}Connector:
    return {cls}Connector(FixtureTransport(FIXTURES))


def test_the_connector_conforms() -> None:
    assert_conforms(build(), writeback=build())


def test_it_conforms_at_every_page_size() -> None:
    """Page boundaries are where cursor bugs live: a connector can be correct
    at one page size and drop a record at another."""
    for page_size in (1, 2, 3, 50):
        connector = {cls}Connector(FixtureTransport(FIXTURES), page_size=page_size)
        assert check_connector(connector, writeback=connector) == ()
'''

FIXTURE_USERS = """\
[
  {"id": "u1", "email": "ana@example.com", "name": "Ana", "groups": ["g1"]},
  {"id": "u2", "email": "ben@example.com", "name": "Ben", "groups": ["g1"]},
  {"id": "u3", "email": "cleo@example.com", "name": "Cleo", "groups": []}
]
"""

FIXTURE_ITEMS = """\
[
  {"id": "i1", "space_id": "s1", "title": "Renewal", "body": "Blocked on legal review."},
  {"id": "i2", "space_id": "s1", "title": "Pricing", "body": "The floor is eighteen percent."},
  {"id": "i3", "space_id": "s2", "title": "Private", "body": "Not everyone can read this."}
]
"""

FIXTURE_SPACES = """\
[
  {"space_id": "s1", "members": ["u1", "u2"]},
  {"space_id": "s2", "members": ["u1"]}
]
"""

README = """\
# hippo-{kind}

A {kind} connector for [Hippo](https://github.com/hippo), built against SDK {sdk}.

Generated by `hippo-new-connector`. It passes the conformance suite as
generated, so the first thing to do is run it and watch it pass:

    pip install -e '.[dev]'
    pytest
    hippo-conformance hippo_{kind}.connector:{cls}Connector --writeback \\
        hippo_{kind}.connector:{cls}Connector

## What to change

1. **`connector.py` — the transport.** `FixtureTransport` reads JSON files.
   Replace it with an httpx client. Nothing above it changes.
2. **The three read streams.** Map the source's shapes onto `IdentityRecord`,
   `ContentRecord` and `AclRecord`. Keep the payload verbatim.
3. **The cursor.** The generated one is offset paging over a list. If the source
   has an updated-since API, store that watermark instead and do not set `DONE`.
4. **`actions.py`.** Declare only what the connector can actually perform, or
   remove it and return `actions=()` for a read-only connector.

## The two things that are easy to get wrong

**The cursor.** `stream(page.cursor)` must yield exactly what follows that page.
Yield too little and the records in between are lost forever, and nothing will
tell you. The conformance suite checks this at several page sizes because a
connector can be correct at one and wrong at another.

**ACLs on containers.** Source systems grant access to containers, not to
individual objects. Emit one ACL record per container and declare each object's
container on its `ContentRecord`. Emitting one per object works and will be
unusably slow on a real workspace.
"""

GITIGNORE = """\
__pycache__/
*.egg-info/
.venv/
.pytest_cache/
"""


def files(kind: str) -> dict[str, str]:
    """Every file the scaffold writes, as path -> content."""
    cls = class_name(kind)
    package = f"hippo_{kind}"
    substitutions = {"kind": kind, "cls": cls, "sdk": SDK_VERSION}
    return {
        "pyproject.toml": PYPROJECT.format(**substitutions),
        "README.md": README.format(**substitutions),
        ".gitignore": GITIGNORE,
        f"{package}/__init__.py": INIT.format(**substitutions),
        f"{package}/connector.py": CONNECTOR.format(**substitutions),
        f"{package}/actions.py": ACTIONS_MODULE.format(**substitutions),
        f"{package}/plugin.py": PLUGIN.format(**substitutions),
        "tests/__init__.py": "",
        "tests/test_conformance.py": TEST.format(**substitutions),
        "tests/fixtures/users.json": FIXTURE_USERS,
        "tests/fixtures/items.json": FIXTURE_ITEMS,
        "tests/fixtures/spaces.json": FIXTURE_SPACES,
    }


def generate(kind: str, out: Path, *, force: bool = False) -> Path:
    """Write the package. Returns its root.

    Refuses a non-empty destination unless told otherwise: the one thing worse
    than no scaffold is a scaffold that overwrote somebody's work.
    """
    if not VALID_KIND.match(kind):
        raise ScaffoldError(
            f"{kind!r} is not a usable connector kind: lowercase letters, digits and "
            "underscores, starting with a letter"
        )

    root = Path(out) / f"hippo-{kind}"
    if root.exists() and any(root.iterdir()) and not force:
        raise ScaffoldError(f"{root} already exists and is not empty; pass --force to overwrite")

    for relative, content in files(kind).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-new-connector",
        description="Generate a Hippo connector package that already conforms.",
    )
    parser.add_argument("kind", help="the source system, lowercase: github, notion, zendesk")
    parser.add_argument("--out", default=".", help="where to write it (default: here)")
    parser.add_argument("--force", action="store_true", help="overwrite a non-empty destination")
    args = parser.parse_args(argv)

    try:
        root = generate(args.kind, Path(args.out), force=args.force)
    except ScaffoldError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"wrote {root}")
    print("\nnext:")
    print(f"  cd {root}")
    print("  pip install -e . && pytest")
    print("\nIt conforms as generated. Replace FixtureTransport with a real one.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
