"""P2-MEM-3's done-condition: pick a ticket, get a coherent cited timeline.

The Memory Timeline is the fragment PROJECT.md flags as a differentiator, and
what makes it one is that it answers a question retrieval cannot express. A
vector index can say "what is relevant to this". Only a graph with time on it
can say "what happened around this, in order".

Two things have to be true for that to be worth anything, and they are the two
this file spends most of its effort on.

**The order has to be the source's order.** Built on `created_at` every entity
in a freshly synced workspace shares one timestamp, and the chain would be a
flat line at import time. The fixtures now carry real timestamps precisely so a
test can tell the difference.

**It has to be filtered.** A timeline is a new way to see what exists. Carol
walking out from a ticket must not reach the private channel that ticket is
discussed in.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent.links import load_directory
from agent.timeline import build
from core.db import Connection
from sync.connectors.jira import FixtureTransport as JiraFixtures
from sync.connectors.jira import JiraConnector
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime, project_acl_grants
from tests.pipeline import principal, resolve_and_enrich

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PRIVATE = "Acme is asking for 30 percent off"


@pytest.fixture
def world(migrated: Connection) -> tuple[UUID, UUID]:
    slack_id, jira_id = uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name, config) VALUES "
            "(%s, 'slack', 'Slack', '{\"workspace_url\": \"https://acme.slack.com\"}'), "
            "(%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
            (slack_id, jira_id),
        )
    SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(migrated)
    SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_all(migrated)
    resolve_and_enrich(migrated)
    project_acl_grants(migrated, slack_id)
    project_acl_grants(migrated, jira_id)
    return slack_id, jira_id


def entity_of(conn: Connection, title: str) -> UUID:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM entities WHERE title = %s LIMIT 1", (title,))
        row = cur.fetchone()
    assert row is not None, f"no entity titled {title!r}"
    return UUID(str(row[0]))


def timeline_for(conn: Connection, who: UUID, subject: UUID, **kwargs: object) -> object:
    return build(conn, who, subject, directory=load_directory(conn), **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Time comes from the source, not from the sync.
# ---------------------------------------------------------------------------


def test_entities_carry_the_time_the_source_gave_them(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """The distinction the whole feature rests on. created_at is when we heard
    about it; occurred_at is when it happened."""
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT occurred_at, created_at FROM entities "
            "WHERE title = 'Acme renewal blocked on legal review'"
        )
        occurred, created = cur.fetchone() or (None, None)

    assert occurred is not None
    assert occurred.year == 2025
    assert occurred < created, "the ticket predates the sync that found it"


def test_a_slack_timestamp_is_read_as_a_time(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT occurred_at FROM entities WHERE title = %s",
            ("legal review is the blocker, not engineering",),
        )
        row = cur.fetchone()

    assert row is not None
    assert row[0] == datetime.fromtimestamp(1750000010, tz=UTC)


def test_an_entity_the_source_never_dated_has_no_time(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """Guessing from the sync clock would be inventing history."""
    with migrated.cursor() as cur:
        cur.execute("SELECT occurred_at FROM entities WHERE entity_type = 'person' LIMIT 1")
        row = cur.fetchone()

    assert row is not None
    assert row[0] is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1750000000.000100", datetime.fromtimestamp(1750000000, tz=UTC)),
        ("1750000000", datetime.fromtimestamp(1750000000, tz=UTC)),
        ("", None),
        (None, None),
        ("not-a-time", None),
    ],
)
def test_slack_timestamps_parse_or_decline(value: object, expected: datetime | None) -> None:
    from resolver.extraction import slack_time

    assert slack_time(value) == expected


@pytest.mark.parametrize(
    "value",
    ["2025-06-15T09:00:00.000+0000", "2025-06-15T09:00:00+00:00", "2025-06-15T09:00:00Z"],
)
def test_jira_timestamps_parse_in_the_shapes_jira_sends(value: str) -> None:
    from resolver.extraction import jira_time

    parsed = jira_time(value)

    assert parsed is not None
    assert parsed.year == 2025


@pytest.mark.parametrize("value", ["", None, "yesterday", "2025-13-45T99:99:99Z"])
def test_an_unparseable_jira_timestamp_is_declined(value: object) -> None:
    from resolver.extraction import jira_time

    assert jira_time(value) is None


# ---------------------------------------------------------------------------
# The done-condition: a coherent, cited chain around a ticket.
# ---------------------------------------------------------------------------


def test_a_ticket_yields_a_timeline(migrated: Connection, world: tuple[UUID, UUID]) -> None:
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket)

    assert chain.moments  # type: ignore[attr-defined]
    assert chain.events  # type: ignore[attr-defined]


def test_the_chain_is_in_the_order_things_happened(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """The whole point. Not sync order, not id order — what the sources say."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket)

    times = [moment.occurred_at for moment in chain.events]  # type: ignore[attr-defined]
    assert times == sorted(times)
    assert len(times) >= 3


def test_the_ticket_comes_before_the_thread_that_discusses_it(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """A causal chain, not just a sorted list: the fixtures place the ticket at
    09:00 and the Slack thread at 14:26, and the timeline has to show that."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket, hops=3)

    titles = [moment.title for moment in chain.events]  # type: ignore[attr-defined]
    assert "Acme renewal blocked on legal review" in titles
    opened = titles.index("Acme renewal blocked on legal review")
    commented = titles.index("legal will not sign until the liability cap is agreed")
    assert opened < commented


def test_every_moment_links_back_to_its_source(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """A timeline entry nobody can click through to is an assertion rather than
    evidence."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket)

    linked = [moment for moment in chain.events if moment.url]  # type: ignore[attr-defined]
    assert linked
    assert all(
        moment.url.startswith(("https://acme.slack.com", "https://acme.atlassian.net"))
        for moment in linked
    )


def test_each_moment_says_how_it_connects(migrated: Connection, world: tuple[UUID, UUID]) -> None:
    """ "A reply in the thread" is a chain. "Related to the thread" is a list."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket)

    relations = {moment.relation for moment in chain.moments}  # type: ignore[attr-defined]
    assert "the subject" in relations
    assert relations & {"written by", "in", "a reply in", "mentions"}


def test_undated_entities_are_context_rather_than_events(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """People and channels explain the chain; they are not steps in it."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket)

    assert chain.context  # type: ignore[attr-defined]
    assert all(moment.occurred_at is None for moment in chain.context)  # type: ignore[attr-defined]
    assert {m.entity_type for m in chain.context} & {"person", "project", "channel"}  # type: ignore[attr-defined]


def test_the_span_says_how_much_time_it_covers(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """Four minutes and four months read very differently."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    span = timeline_for(migrated, alice, ticket).span  # type: ignore[attr-defined]

    assert span is not None
    assert span[0] < span[1]


# ---------------------------------------------------------------------------
# A timeline is a new way to see what exists, so it is a new way to leak.
# ---------------------------------------------------------------------------


def test_the_private_channel_is_not_on_carols_timeline(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """The filtered path, applied to the timeline. Carol can reach the ticket
    and must not reach the deal room it is negotiated in."""
    slack_id, _ = world
    carol = principal(migrated, slack_id, "U-CAROL")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, carol, ticket, hops=3)

    titles = " ".join(moment.title or "" for moment in chain.moments)  # type: ignore[attr-defined]
    assert PRIVATE not in titles


def test_alice_does_see_it(migrated: Connection, world: tuple[UUID, UUID]) -> None:
    """The positive half, so the test above is not passing because the walk
    reaches nothing for anyone."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    deal = entity_of(migrated, "#deals-acme")

    chain = timeline_for(migrated, alice, deal, hops=2)

    titles = " ".join(moment.title or "" for moment in chain.moments)  # type: ignore[attr-defined]
    assert PRIVATE in titles


def test_a_subject_you_cannot_see_yields_nothing(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """Not an error, and not a partial chain: naming an entity you lack access
    to must not confirm that it exists."""
    slack_id, _ = world
    carol = principal(migrated, slack_id, "U-CAROL")
    private_channel = entity_of(migrated, "#deals-acme")

    chain = timeline_for(migrated, carol, private_channel, hops=3)

    assert chain.moments == ()  # type: ignore[attr-defined]


def test_an_unknown_principal_gets_nothing(migrated: Connection, world: tuple[UUID, UUID]) -> None:
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, uuid4(), ticket)

    assert chain.moments == ()  # type: ignore[attr-defined]


def test_hops_are_clamped(migrated: Connection, world: tuple[UUID, UUID]) -> None:
    """Three hops already tends to reach the whole workspace through a shared
    channel, and a timeline that includes everything is about nothing."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    wide = timeline_for(migrated, alice, ticket, hops=99)
    narrow = timeline_for(migrated, alice, ticket, hops=1)

    assert len(narrow.moments) <= len(wide.moments)  # type: ignore[attr-defined]


def test_the_limit_bounds_the_result(migrated: Connection, world: tuple[UUID, UUID]) -> None:
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    chain = timeline_for(migrated, alice, ticket, hops=3, limit=3)

    assert len(chain.moments) == 3  # type: ignore[attr-defined]


def test_the_agent_role_can_build_a_timeline(
    migrated: Connection, world: tuple[UUID, UUID]
) -> None:
    """It is granted, like visible_chunks, so the same reduced role that
    answers questions can also explain them."""
    slack_id, _ = world
    alice = principal(migrated, slack_id, "U-ALICE")
    ticket = entity_of(migrated, "Acme renewal blocked on legal review")

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
    try:
        with migrated.cursor() as cur:
            cur.execute("SELECT count(*) FROM timeline(%s, %s, 2, 100)", (alice, ticket))
            assert (cur.fetchone() or (0,))[0] > 0
    finally:
        with migrated.cursor() as cur:
            cur.execute("RESET ROLE")
