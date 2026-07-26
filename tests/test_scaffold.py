"""P3-SDK-2: the scaffold has to produce something that already works.

"Write a connector in a weekend" is only true if the weekend starts from
something that runs. A scaffold full of TODOs where the hard parts go — the
cursor, the ACL grain — leaves exactly the difficult work undone and gives a
contributor no way to tell whether they have done it right.

So the test that matters is not that files were written. It is that the
generated connector is imported and run through the conformance suite, at
several page sizes, and passes. If that ever stops being true the scaffold is
worse than nothing, because it teaches the wrong shape.
"""

from __future__ import annotations

import importlib
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from sync.connectors.harness import check_connector
from sync.connectors.scaffold import ScaffoldError, class_name, files, generate, main
from sync.connectors.sdk import SDK_VERSION


@pytest.fixture
def generated(tmp_path: Path) -> Iterator[Path]:
    """A generated package, importable for the duration of one test."""
    root = generate("acme", tmp_path)
    sys.path.insert(0, str(root))
    try:
        yield root
    finally:
        sys.path.remove(str(root))
        for name in [n for n in sys.modules if n.startswith("hippo_acme")]:
            del sys.modules[name]


# ---------------------------------------------------------------------------
# The one that matters.
# ---------------------------------------------------------------------------


def test_the_generated_connector_conforms(generated: Path) -> None:
    """Imported and run, not inspected. This is the fragment's done-condition."""
    module = importlib.import_module("hippo_acme")
    connector = module.AcmeConnector(module.FixtureTransport(generated / "tests" / "fixtures"))

    assert check_connector(connector, writeback=connector) == ()


def test_it_conforms_at_every_page_size(generated: Path) -> None:
    """Page boundaries are where cursor bugs live: a connector can be correct
    at one page size and drop a record at another."""
    module = importlib.import_module("hippo_acme")
    fixtures = generated / "tests" / "fixtures"

    for page_size in (1, 2, 3, 7, 50):
        connector = module.AcmeConnector(module.FixtureTransport(fixtures), page_size=page_size)
        assert check_connector(connector, writeback=connector) == (), f"page_size={page_size}"


def test_the_generated_streams_actually_yield_records(generated: Path) -> None:
    """Conformance passes for a connector that yields nothing, so the fixtures
    have to be checked separately or the scaffold could teach an empty shape."""
    module = importlib.import_module("hippo_acme")
    connector = module.AcmeConnector(module.FixtureTransport(generated / "tests" / "fixtures"))

    counts = {
        name: sum(len(page.records) for page in getattr(connector, name)({}))
        for name in ("identities", "content", "acls")
    }

    assert counts == {"identities": 3, "content": 3, "acls": 3}


def test_the_generated_acls_grant_on_containers(generated: Path) -> None:
    """The scaffold has to teach the right grain. One grant per object works
    and is unusably slow on a real workspace."""
    module = importlib.import_module("hippo_acme")
    connector = module.AcmeConnector(module.FixtureTransport(generated / "tests" / "fixtures"))

    targets = {record.target.source_type for page in connector.acls({}) for record in page.records}

    assert targets == {"acme.space"}


def test_the_generated_plugin_declares_its_actions(generated: Path) -> None:
    module = importlib.import_module("hippo_acme")

    assert module.PLUGIN.kind == "acme"
    assert module.PLUGIN.capabilities.action_types == {"acme.comment"}
    assert module.PLUGIN.capabilities.sdk_version == SDK_VERSION


def test_the_generated_payload_model_forbids_extra_fields(generated: Path) -> None:
    """Checked by the conformance suite too, but worth pinning here: it is what
    stops a proposal carrying a field the connector passes straight through."""
    actions = importlib.import_module("hippo_acme.actions")

    with pytest.raises(Exception, match="Extra inputs are not permitted"):
        actions.CommentPayload(body="hello", extra="smuggled")


# ---------------------------------------------------------------------------
# What it writes.
# ---------------------------------------------------------------------------


def test_it_writes_a_package_that_installs_alongside_hippo(generated: Path) -> None:
    """Its own package with an entry point, so nothing in this repository has
    to change for it to be discovered."""
    pyproject = (generated / "pyproject.toml").read_text()

    assert '[project.entry-points."hippo.connectors"]' in pyproject
    assert 'acme = "hippo_acme:PLUGIN"' in pyproject
    assert '"hippo"' in pyproject


def test_it_ships_offline_fixtures(generated: Path) -> None:
    """Fixtures before live. A contributor should never need a token to find
    out whether their cursor resumes."""
    for name in ("users", "items", "spaces"):
        payload = json.loads((generated / "tests" / "fixtures" / f"{name}.json").read_text())
        assert payload, name


def test_it_ships_its_own_conformance_test(generated: Path) -> None:
    test = (generated / "tests" / "test_conformance.py").read_text()

    assert "assert_conforms" in test
    assert "page_size" in test


def test_the_readme_names_the_two_easy_mistakes(generated: Path) -> None:
    """The cursor and the ACL grain. Both are silent when wrong, which is why
    they are what the guide leads with."""
    readme = (generated / "README.md").read_text()

    assert "cursor" in readme.lower()
    assert "container" in readme.lower()


# ---------------------------------------------------------------------------
# Names and destinations.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "expected"),
    [("github", "Github"), ("google_drive", "GoogleDrive"), ("zendesk", "Zendesk")],
)
def test_a_kind_becomes_a_class_name(kind: str, expected: str) -> None:
    assert class_name(kind) == expected


@pytest.mark.parametrize(
    "kind", ["Github", "my-connector", "1password", "", "x" * 40, "with space", "UPPER"]
)
def test_an_unusable_kind_is_refused(kind: str, tmp_path: Path) -> None:
    """It becomes a package name, a class name and a source-type prefix, so it
    has to survive all three."""
    with pytest.raises(ScaffoldError):
        generate(kind, tmp_path)


def test_a_non_empty_destination_is_refused(tmp_path: Path) -> None:
    """The one thing worse than no scaffold is one that overwrote somebody's
    work."""
    generate("acme", tmp_path)

    with pytest.raises(ScaffoldError, match="already exists"):
        generate("acme", tmp_path)


def test_overwriting_can_be_asked_for(tmp_path: Path) -> None:
    generate("acme", tmp_path)

    assert generate("acme", tmp_path, force=True).exists()


def test_every_generated_file_has_content() -> None:
    """Except the package markers, which are empty on purpose."""
    written = files("acme")

    assert all(content for path, content in written.items() if not path.endswith("__init__.py"))
    assert "hippo_acme/connector.py" in written


# ---------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------


def test_the_cli_writes_and_says_what_to_do_next(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["acme", "--out", str(tmp_path)]) == 0

    printed = capsys.readouterr().out
    assert "pytest" in printed
    assert (tmp_path / "hippo-acme" / "pyproject.toml").exists()


def test_the_cli_refuses_a_bad_name(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["Not-Valid", "--out", str(tmp_path)]) == 2
    assert "not a usable connector kind" in capsys.readouterr().err


def test_the_cli_can_be_told_to_overwrite(tmp_path: Path) -> None:
    assert main(["acme", "--out", str(tmp_path)]) == 0
    assert main(["acme", "--out", str(tmp_path)]) == 2
    assert main(["acme", "--out", str(tmp_path), "--force"]) == 0
