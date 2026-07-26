"""What ships, and whether it is enough to start.

This file exists because of a bug it would have caught. `surfaces/` was added
in P3-SRF-1 and imported at module scope by api.main, and the Dockerfile was
never updated — so the container would have failed on ImportError at start.
Nothing found it, because the compose smoke test is the only gate that builds
an image and it had never run.

The lesson is not "remember to update the Dockerfile". It is that a list of
files maintained by hand, in a language that cannot check it, next to an import
graph that changes, will drift. So the list is checked against the graph.

The other tests here are the same shape: things that are true of a release
rather than of a function, and that no unit test would notice going wrong.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "deploy" / "Dockerfile"

# Directories that are Python packages of ours, as opposed to a frontend, a
# docs tree or a fixture pile.
FIRST_PARTY = frozenset({"core", "api", "sync", "resolver", "agent", "surfaces", "evals"})


def copied_packages() -> set[str]:
    """Top-level packages the app image contains."""
    lines = DOCKERFILE.read_text().splitlines()
    found: set[str] = set()
    for line in lines:
        match = re.match(r"^COPY\s+(\S+)\s+\./?(\S+)?$", line.strip())
        if match and match.group(1) in FIRST_PARTY:
            found.add(match.group(1))
    return found


def imported_packages() -> set[str]:
    """Top-level first-party packages importing the app entry point pulls in.

    Measured in a subprocess. Doing it in-process was the first version and it
    was wrong twice over: `sys.modules` already holds api.main after the first
    test that imports it, so the second call sees an empty delta — and in a
    full-suite run it holds every package any other test file touched, so the
    answer depended on test order. A fresh interpreter is the only way to ask
    "what does starting the app load" and get the same answer every time.
    """
    program = (
        "import sys, json; before = set(sys.modules); import api.main; "
        "print(json.dumps(sorted({n.split('.')[0] for n in set(sys.modules) - before} "
        "| {'api'})))"
    )
    out = subprocess.run(
        [sys.executable, "-c", program],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(json.loads(out)) & FIRST_PARTY


def test_the_image_contains_everything_the_app_imports() -> None:
    """A missing package is an ImportError at container start, not a degraded
    feature — and the only gate that would notice is the one that builds an
    image."""
    missing = sorted(imported_packages() - copied_packages())

    assert missing == [], (
        f"deploy/Dockerfile does not COPY {', '.join(missing)}, which api.main imports. "
        f"The container would fail to start."
    )


def test_the_image_carries_nothing_it_does_not_need() -> None:
    """The other direction. A package copied and never imported is either dead
    weight or a sign something was moved and this file was not."""
    unused = sorted(copied_packages() - imported_packages())

    assert unused == [], f"deploy/Dockerfile copies {', '.join(unused)}, which nothing imports"


def test_every_console_script_resolves() -> None:
    """A broken entry point is discovered by whoever runs the command, which is
    a contributor on their first day."""
    import importlib

    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    scripts = manifest["project"]["scripts"]

    assert scripts, "the package declares console scripts"
    for name, target in scripts.items():
        module_name, _, attribute = target.partition(":")
        module = importlib.import_module(module_name)
        assert callable(getattr(module, attribute)), f"{name} -> {target}"


def test_every_connector_entry_point_resolves() -> None:
    """The built-ins are discovered exactly the way a contributed connector is,
    so a broken declaration here breaks the mechanism everyone depends on."""
    import importlib

    from sync.connectors.registry import ConnectorPlugin

    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    connectors = manifest["project"]["entry-points"]["hippo.connectors"]

    assert set(connectors) == {"slack", "jira", "github"}
    for kind, target in connectors.items():
        module_name, _, attribute = target.partition(":")
        plugin = getattr(importlib.import_module(module_name), attribute)
        assert isinstance(plugin, ConnectorPlugin), target
        assert plugin.kind == kind


def test_the_lockfile_matches_the_manifest() -> None:
    """CI installs with --frozen, so a lockfile behind pyproject.toml fails the
    build rather than resolving something different from what was tested."""
    manifest = tomllib.loads((ROOT / "pyproject.toml").read_text())
    lock = (ROOT / "uv.lock").read_text()

    for requirement in manifest["project"]["dependencies"]:
        name = re.split(r"[<>=\[]", requirement, maxsplit=1)[0].strip()
        assert f'name = "{name}"' in lock, f"{name} is a dependency and is not in uv.lock"


def test_the_migrations_are_numbered_without_gaps() -> None:
    """A gap means a migration was deleted rather than reversed, and the next
    person's numbering collides with a version some database has applied."""
    versions = sorted(
        int(path.name.split("_")[0])
        for path in (ROOT / "core" / "migrations").glob("*.sql")
        if not path.name.endswith(".down.sql")
    )

    assert versions == list(range(1, len(versions) + 1))


# The two that cannot be reversed, and are not expected to be. 001 creates the
# schema and 002 creates the roles: "reversing" either means dropping the
# database, which is `dropdb`, not a migration. CLAUDE.md says reversible
# *where possible*, and this is the whole of where it is not.
IRREVERSIBLE = frozenset({"001_schema.sql", "002_roles.sql"})


def test_every_migration_after_the_foundation_can_be_reversed() -> None:
    """An absent .down.sql on anything newer is an omission, not a decision —
    and the round-trip test in test_migrate_apply.py can only check what
    exists, so nothing else would notice one missing."""
    migrations = ROOT / "core" / "migrations"
    forward = {
        path.name
        for path in migrations.glob("*.sql")
        if not path.name.endswith(".down.sql") and path.name not in IRREVERSIBLE
    }

    missing = sorted(
        name for name in forward if not (migrations / name.replace(".sql", ".down.sql")).exists()
    )

    assert missing == []


def test_the_irreversible_ones_are_still_the_only_two() -> None:
    """A guard on the exemption. Adding a name to IRREVERSIBLE should take a
    conversation, not a passing test suite."""
    assert {"001_schema.sql", "002_roles.sql"} == IRREVERSIBLE
    for name in IRREVERSIBLE:
        assert (ROOT / "core" / "migrations" / name).exists()


def test_no_env_file_is_tracked() -> None:
    """The example is committed; a real one never is. Checked here rather than
    trusted to .gitignore, because a `git add -f` is one keystroke."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.splitlines()

    leaked = [path for path in tracked if Path(path).name in (".env", ".env.local")]

    assert leaked == []


# Next.js route handlers may export HTTP verbs and a short list of config
# fields, and nothing else. Anything extra is a build error that neither eslint
# nor `tsc --noEmit` reports — only `next build` does, which is the slowest gate
# and the one furthest from whoever wrote the line.
ROUTE_EXPORTS = frozenset(
    {
        "GET",
        "HEAD",
        "POST",
        "PUT",
        "DELETE",
        "PATCH",
        "OPTIONS",
        "dynamic",
        "dynamicParams",
        "revalidate",
        "fetchCache",
        "runtime",
        "preferredRegion",
        "maxDuration",
    }
)

EXPORTED = re.compile(r"^export\s+(?:async\s+)?(?:function|const|let|var)\s+(\w+)", re.MULTILINE)


def test_no_route_handler_exports_something_next_will_reject() -> None:
    """Caught here in a second rather than in `next build` in five minutes.

    The bug this is for: `export const SSO_STATE_COOKIE` in a route handler.
    It type-checks, it lints, and it fails the production build — so a shared
    constant belongs in lib/, not beside the handler that happens to set it.
    """
    offenders: list[str] = []
    for path in (ROOT / "ui" / "app").rglob("route.ts"):
        for name in EXPORTED.findall(path.read_text()):
            if name not in ROUTE_EXPORTS:
                offenders.append(f"{path.relative_to(ROOT)} exports {name}")

    assert offenders == [], "; ".join(offenders)


def test_the_ui_has_route_handlers_to_check() -> None:
    """A guard on the guard: a rename of the app directory would make the test
    above pass by finding nothing."""
    assert list((ROOT / "ui" / "app").rglob("route.ts"))
