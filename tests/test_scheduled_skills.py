"""P3-AGT-2: a question nobody is present to ask.

A standing digest is the first read in this system with no person behind it,
and the permission filter answers "what may *you* see" — so the whole fragment
turns on establishing a whose. Migration 022 makes `runs_as` NOT NULL with a
foreign key, and there is no system principal that sees everything.

The tempting alternative is a service account with broad access posting
summaries into a channel. That is how a memory system becomes the thing that
leaks, and the first test here is that it is not expressible: the column will
not take a NULL and there is no row shape for it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from agent.loop import Agent
from agent.providers.base import Completion, CompletionRequest, Usage
from agent.scheduled import (
    Due,
    ScheduleError,
    claim,
    create,
    delete,
    for_principal,
    run_due,
    run_one,
    set_enabled,
)
from agent.skills import Skill, parse
from core.db import Connection
from resolver.embeddings import HashingEmbeddings

pytestmark = pytest.mark.requires_db

DIGEST = parse(
    "name: pipeline-digest\n"
    "question: What changed about {topic} this week?\n"
    "inputs:\n  - name: topic\n"
)
ACTING = parse(
    "name: acting-digest\n"
    "question: Summarise {issue} and comment on it\n"
    "inputs:\n  - name: issue\n"
    "actions:\n  - jira.comment\n"
)
SKILLS: dict[str, Skill] = {DIGEST.name: DIGEST, ACTING.name: ACTING}


class ScriptedModel:
    """Says one thing, and records what it was asked."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, reply: str = "Nothing changed.") -> None:
        self.reply = reply
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text=self.reply,
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@pytest.fixture
def alice(migrated: Connection) -> UUID:
    principal = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, email, source_id) "
        "VALUES (%s, 'user', 'alice@example.com', 'U-ALICE')",
        (principal,),
    )
    return principal


@pytest.fixture
def agent(migrated: Connection) -> Agent:
    return Agent(ScriptedModel(), embedder=HashingEmbeddings())


def plant(conn: Connection, principal: UUID, text: str) -> UUID:
    """Something for a run to retrieve.

    Without it the loop answers "nothing found" and never reaches the model,
    which makes a test about what the model was asked pass for no reason.
    """
    entity = uuid4()
    conn.execute(
        "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'message', 'm')", (entity,)
    )
    conn.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (entity, principal),
    )
    conn.execute(
        "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
        "VALUES (%s, '00000000-0000-0000-0000-000000000001', %s, 0)",
        (entity, text),
    )
    return entity


def due_now(conn: Connection, schedule_id: UUID) -> None:
    conn.execute(
        "UPDATE skill_schedules SET next_run_at = now() - interval '1 minute' WHERE id = %s",
        (schedule_id,),
    )


# ---------------------------------------------------------------------------
# There is no "runs as everybody".
# ---------------------------------------------------------------------------


def test_a_schedule_must_name_whose_permissions_it_uses(migrated: Connection) -> None:
    """The one that matters. A service account with broad access delivering
    summaries is not expressible here, because the column will not take a
    NULL."""
    with pytest.raises(errors.NotNullViolation):
        migrated.execute(
            "INSERT INTO skill_schedules (skill, runs_as, cadence) "
            "VALUES ('pipeline-digest', NULL, 'daily')"
        )
    migrated.rollback()


def test_a_schedule_cannot_name_a_principal_that_does_not_exist(
    migrated: Connection,
) -> None:
    with pytest.raises(errors.ForeignKeyViolation):
        migrated.execute(
            "INSERT INTO skill_schedules (skill, runs_as, cadence) "
            "VALUES ('pipeline-digest', %s, 'daily')",
            (uuid4(),),
        )
    migrated.rollback()


def test_a_run_sees_exactly_what_that_person_sees(
    migrated: Connection, alice: UUID, agent: Agent
) -> None:
    """The answer a schedule produces is what its owner would have got by
    asking. Bob's content is in the corpus and Alice's run does not reach it."""
    bob = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, source_id) VALUES (%s, 'user', 'U-BOB')", (bob,)
    )
    for principal, text in (
        (alice, "the renewal is blocked on the liability cap in legal review"),
        (bob, "bob's private note about the liability cap and the renewal"),
    ):
        entity = uuid4()
        migrated.execute(
            "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'message', 'm')", (entity,)
        )
        migrated.execute(
            "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
            (entity, principal),
        )
        migrated.execute(
            "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
            "VALUES (%s, '00000000-0000-0000-0000-000000000001', %s, 0)",
            (entity, text),
        )

    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "the liability cap"})
    due_now(migrated, schedule.id)
    answer = run_one(migrated, claim(migrated)[0], SKILLS, agent)

    assert answer is not None
    contents = [hit.content for hit in answer.hits]
    assert any("legal review" in text for text in contents)
    assert not any("bob's private note" in text for text in contents)


def test_a_run_asks_the_rendered_question(migrated: Connection, alice: UUID, agent: Agent) -> None:
    """A schedule quietly asking something other than what its author wrote is
    the failure worth being able to rule out."""
    model = ScriptedModel()
    built = Agent(model, embedder=HashingEmbeddings())
    plant(migrated, alice, "pricing changed this week: the floor moved to eighteen percent")
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "pricing"})
    due_now(migrated, schedule.id)

    run_one(migrated, claim(migrated)[0], SKILLS, built)

    assert "What changed about pricing this week?" in model.requests[-1].messages[0].content


# ---------------------------------------------------------------------------
# Creating one.
# ---------------------------------------------------------------------------


def test_creating_a_schedule_records_when_it_next_runs(migrated: Connection, alice: UUID) -> None:
    schedule = create(
        migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"}, cadence="weekly"
    )

    assert schedule.next_run_at is not None
    assert schedule.next_run_at > datetime.now(UTC)
    assert schedule.describes == "every Monday at 09:00 UTC"


def test_inputs_are_checked_when_the_schedule_is_made(migrated: Connection, alice: UUID) -> None:
    """Not at the first run. A schedule whose inputs do not satisfy its skill
    would otherwise sit quietly until the hour it was meant to fire, which is
    the worst possible moment to find out."""
    with pytest.raises(ScheduleError, match="is required"):
        create(migrated, skill=DIGEST, runs_as=alice, inputs={})


def test_an_unknown_cadence_is_refused(migrated: Connection, alice: UUID) -> None:
    with pytest.raises(ScheduleError, match="cadence must be"):
        create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"}, cadence="fortnightly")


def test_one_standing_question_per_person_per_skill(migrated: Connection, alice: UUID) -> None:
    """A second is either a mistake or a sign the skill needs an input, and
    both are better said out loud."""
    create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    with pytest.raises(errors.UniqueViolation):
        create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "y"})
    migrated.rollback()


@pytest.mark.parametrize(
    ("cadence", "hour", "weekday", "expected"),
    [
        ("hourly", 9, 1, "every hour"),
        ("daily", 7, 1, "every day at 07:00 UTC"),
        ("weekly", 9, 5, "every Friday at 09:00 UTC"),
    ],
)
def test_a_schedule_reads_plainly(
    migrated: Connection, alice: UUID, cadence: str, hour: int, weekday: int, expected: str
) -> None:
    schedule = create(
        migrated,
        skill=DIGEST,
        runs_as=alice,
        inputs={"topic": "x"},
        cadence=cadence,
        at_hour=hour,
        at_weekday=weekday,
    )

    assert schedule.describes == expected


@pytest.mark.parametrize(("hour", "weekday"), [(24, 1), (-1, 1), (9, 0), (9, 8)])
def test_an_impossible_time_is_refused_by_the_database(
    migrated: Connection, alice: UUID, hour: int, weekday: int
) -> None:
    with pytest.raises(errors.CheckViolation):
        create(
            migrated,
            skill=DIGEST,
            runs_as=alice,
            inputs={"topic": "x"},
            cadence="weekly",
            at_hour=hour,
            at_weekday=weekday,
        )
    migrated.rollback()


# ---------------------------------------------------------------------------
# Claiming, which is where two workers must not collide.
# ---------------------------------------------------------------------------


def test_claiming_moves_the_schedule_forward(migrated: Connection, alice: UUID) -> None:
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)

    assert len(claim(migrated)) == 1
    assert claim(migrated) == []


def test_a_schedule_not_yet_due_is_not_claimed(migrated: Connection, alice: UUID) -> None:
    create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"}, cadence="weekly")

    assert claim(migrated) == []


def test_a_disabled_schedule_is_not_claimed(migrated: Connection, alice: UUID) -> None:
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)
    set_enabled(migrated, schedule.id, alice, enabled=False)

    assert claim(migrated) == []


def test_next_run_is_always_in_the_future(migrated: Connection, alice: UUID) -> None:
    """Otherwise a claim that lands exactly on the boundary schedules itself
    for the same instant and the loop never advances."""
    for cadence in ("hourly", "daily", "weekly"):
        with migrated.cursor() as cur:
            cur.execute("SELECT next_skill_run(%s, %s, %s, now())", (cadence, 9, 1))
            row = cur.fetchone()
        assert row is not None
        assert row[0] > datetime.now(UTC), cadence


def test_a_weekly_schedule_lands_on_its_weekday(migrated: Connection) -> None:
    """Checked in SQL rather than trusted: getting the modular arithmetic
    wrong here delivers Monday's summary on Sunday."""
    for weekday in range(1, 8):
        with migrated.cursor() as cur:
            cur.execute(
                "SELECT extract(isodow FROM next_skill_run('weekly', 9, %s, now()) "
                "AT TIME ZONE 'UTC')",
                (weekday,),
            )
            row = cur.fetchone()
        assert row is not None
        assert int(row[0]) == weekday


def test_a_daily_schedule_lands_on_its_hour(migrated: Connection) -> None:
    for hour in (0, 7, 23):
        with migrated.cursor() as cur:
            cur.execute(
                "SELECT extract(hour FROM next_skill_run('daily', %s, 1, now()) "
                "AT TIME ZONE 'UTC')",
                (hour,),
            )
            row = cur.fetchone()
        assert row is not None
        assert int(row[0]) == hour


# ---------------------------------------------------------------------------
# Running, and failing.
# ---------------------------------------------------------------------------


def test_a_scheduled_action_still_waits_for_a_person(migrated: Connection, alice: UUID) -> None:
    """Nothing about being scheduled changes rule 2. A pending row attributed
    to the principal it ran as, and no execution."""
    entity = uuid4()
    connector = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name, config) "
        "VALUES (%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
        (connector,),
    )
    migrated.execute(
        "INSERT INTO entities (id, entity_type, title) VALUES (%s, 'issue', 'ACME-1')", (entity,)
    )
    migrated.execute(
        "INSERT INTO raw_records (connector_id, source_type, source_id, payload) "
        "VALUES (%s, 'jira.issue', 'ACME-1', '{}')",
        (connector,),
    )
    with migrated.cursor() as cur:
        cur.execute("SELECT id FROM raw_records LIMIT 1")
        raw = (cur.fetchone() or (None,))[0]
    migrated.execute(
        "INSERT INTO entity_sources (entity_id, raw_record_id) VALUES (%s, %s)", (entity, raw)
    )
    migrated.execute(
        "INSERT INTO acl_grants (entity_id, principal_id, source) VALUES (%s, %s, 'test')",
        (entity, alice),
    )
    migrated.execute(
        "INSERT INTO chunks (entity_id, scope_id, content, chunk_index) "
        "VALUES (%s, '00000000-0000-0000-0000-000000000001', "
        "'ACME-1 the renewal is blocked on the liability cap', 0)",
        (entity,),
    )

    from agent.links import load_directory

    model = ScriptedModel(
        '{"action_type": "jira.comment", "source": 1, "payload": {"body": "summary"}}'
    )
    built = Agent(model, embedder=HashingEmbeddings(), directory=load_directory(migrated))
    schedule = create(migrated, skill=ACTING, runs_as=alice, inputs={"issue": "ACME-1"})
    due_now(migrated, schedule.id)

    run_one(migrated, claim(migrated)[0], SKILLS, built)

    with migrated.cursor() as cur:
        cur.execute("SELECT status, requested_by FROM actions")
        rows = cur.fetchall()
    assert [(str(row[0]), row[1]) for row in rows] == [("pending", alice)]


def test_a_schedule_naming_a_removed_skill_reports_rather_than_disappears(
    migrated: Connection, alice: UUID, agent: Agent
) -> None:
    """The file may be coming back. Deleting somebody's standing question
    because of a deploy order is worse than saying it did not run."""
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)

    assert run_one(migrated, claim(migrated)[0], {}, agent) is None

    mine = for_principal(migrated, alice)
    assert mine[0].last_error is not None
    assert "no skill named" in mine[0].last_error


def test_one_broken_schedule_does_not_stop_the_others(
    migrated: Connection, alice: UUID, agent: Agent
) -> None:
    good = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    bad = create(migrated, skill=ACTING, runs_as=alice, inputs={"issue": "y"})
    for schedule in (good, bad):
        due_now(migrated, schedule.id)

    ran = run_due(migrated, {DIGEST.name: DIGEST}, agent)

    assert ran == 1


def test_a_failure_is_recorded_where_its_owner_will_see_it(
    migrated: Connection, alice: UUID
) -> None:
    """Whoever owns a standing question is the person who needs to know it has
    been failing, and they are not reading the worker's logs."""

    class Broken:
        name = "broken"
        model = "broken-1"

        def complete(self, request: CompletionRequest) -> Completion:
            raise RuntimeError("the provider is down")

        def count_tokens(self, request: CompletionRequest) -> int:
            return 0

    plant(migrated, alice, "x marks the place where the renewal is blocked")
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)

    run_one(migrated, claim(migrated)[0], SKILLS, Agent(Broken(), embedder=HashingEmbeddings()))

    mine = for_principal(migrated, alice)
    assert mine[0].last_error is not None
    assert "provider is down" in mine[0].last_error


def test_a_failing_schedule_does_not_spin(migrated: Connection, alice: UUID, agent: Agent) -> None:
    """next_run_at moves forward on claim rather than on success, so a skill
    that raises every time fails once per cadence instead of continuously."""
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)

    run_due(migrated, {}, agent)

    assert claim(migrated) == []


def test_a_successful_run_clears_the_previous_error(
    migrated: Connection, alice: UUID, agent: Agent
) -> None:
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)
    run_one(migrated, claim(migrated)[0], {}, agent)

    due_now(migrated, schedule.id)
    run_one(migrated, claim(migrated)[0], SKILLS, agent)

    assert for_principal(migrated, alice)[0].last_error is None


def test_nothing_due_is_not_an_error(migrated: Connection, agent: Agent) -> None:
    assert run_due(migrated, SKILLS, agent) == 0


def test_invalid_stored_inputs_are_reported(
    migrated: Connection, alice: UUID, agent: Agent
) -> None:
    """A skill whose inputs changed after a schedule was made for it."""
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)
    changed = parse(
        "name: pipeline-digest\nquestion: What about {other}?\ninputs:\n  - name: other\n"
    )

    assert run_one(migrated, claim(migrated)[0], {changed.name: changed}, agent) is None
    # "required" rather than "unknown": the skill now wants `other`, and the
    # missing one is reported before the leftover one.
    assert "'other' is required" in str(for_principal(migrated, alice)[0].last_error)


# ---------------------------------------------------------------------------
# Yours, and only yours.
# ---------------------------------------------------------------------------


def test_you_see_your_own_schedules(migrated: Connection, alice: UUID) -> None:
    bob = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, source_id) VALUES (%s, 'user', 'U-BOB')", (bob,)
    )
    create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "mine"})
    create(migrated, skill=DIGEST, runs_as=bob, inputs={"topic": "theirs"})

    assert [s.inputs["topic"] for s in for_principal(migrated, alice)] == ["mine"]
    assert [s.inputs["topic"] for s in for_principal(migrated, bob)] == ["theirs"]


def test_a_schedule_follows_a_person_across_their_accounts(
    migrated: Connection, alice: UUID
) -> None:
    """One created from a Jira login should be visible from the Slack one."""
    from resolver.resolution import link_principal_identities

    other = uuid4()
    connector = uuid4()
    migrated.execute(
        "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'Jira')", (connector,)
    )
    migrated.execute(
        "INSERT INTO principals (id, kind, email, source_id, connector_id) "
        "VALUES (%s, 'user', 'alice@example.com', 'U-ALICE-JIRA', %s)",
        (other, connector),
    )
    link_principal_identities(migrated)
    create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    assert len(for_principal(migrated, other)) == 1


def test_you_cannot_pause_somebody_elses(migrated: Connection, alice: UUID) -> None:
    bob = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, source_id) VALUES (%s, 'user', 'U-BOB')", (bob,)
    )
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    assert set_enabled(migrated, schedule.id, bob, enabled=False) is False
    assert for_principal(migrated, alice)[0].enabled is True


def test_you_cannot_delete_somebody_elses(migrated: Connection, alice: UUID) -> None:
    bob = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, source_id) VALUES (%s, 'user', 'U-BOB')", (bob,)
    )
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    assert delete(migrated, schedule.id, bob) is False
    assert len(for_principal(migrated, alice)) == 1


def test_you_can_pause_and_resume_your_own(migrated: Connection, alice: UUID) -> None:
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    assert set_enabled(migrated, schedule.id, alice, enabled=False) is True
    assert for_principal(migrated, alice)[0].enabled is False
    assert set_enabled(migrated, schedule.id, alice, enabled=True) is True
    assert for_principal(migrated, alice)[0].enabled is True


def test_you_can_delete_your_own(migrated: Connection, alice: UUID) -> None:
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    assert delete(migrated, schedule.id, alice) is True
    assert for_principal(migrated, alice) == []


def test_removing_a_principal_removes_their_schedules(migrated: Connection, alice: UUID) -> None:
    """An offboarded person's standing questions stop running, which is most of
    why the foreign key is there."""
    create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})

    migrated.execute("DELETE FROM principals WHERE id = %s", (alice,))

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM skill_schedules")
        assert cur.fetchone() == (0,)


def test_a_due_row_carries_only_what_a_run_needs(migrated: Connection, alice: UUID) -> None:
    schedule = create(migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"})
    due_now(migrated, schedule.id)

    claimed = claim(migrated)[0]

    assert claimed == Due(
        id=schedule.id, skill="pipeline-digest", runs_as=alice, inputs={"topic": "x"}
    )


def test_the_claim_limit_is_honoured(migrated: Connection, alice: UUID) -> None:
    bob = uuid4()
    migrated.execute(
        "INSERT INTO principals (id, kind, source_id) VALUES (%s, 'user', 'U-BOB')", (bob,)
    )
    for principal in (alice, bob):
        schedule = create(migrated, skill=DIGEST, runs_as=principal, inputs={"topic": "x"})
        due_now(migrated, schedule.id)

    assert len(claim(migrated, limit=1)) == 1
    assert len(claim(migrated, limit=1)) == 1


def test_an_hourly_schedule_is_due_within_the_hour(migrated: Connection, alice: UUID) -> None:
    schedule = create(
        migrated, skill=DIGEST, runs_as=alice, inputs={"topic": "x"}, cadence="hourly"
    )

    assert schedule.next_run_at is not None
    assert schedule.next_run_at <= datetime.now(UTC) + timedelta(hours=1)


# ---------------------------------------------------------------------------
# The background loop that fires them.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_the_loop_is_silent_without_skills(tmp_path: Path) -> None:
    """An install with no skills configured runs no scheduler at all, rather
    than a loop that wakes every minute to find nothing."""
    from api.main import _run_scheduled_skills
    from core.config import Settings

    async def unused(*_args: object) -> None:  # pragma: no cover - must not run
        raise AssertionError("no database call expected")

    # Returns immediately rather than looping: no skills_path, nothing to do.
    await _run_scheduled_skills(unused, Settings(), unused)  # type: ignore[arg-type]
    await _run_scheduled_skills(
        unused,  # type: ignore[arg-type]
        Settings(skills_path=tmp_path),
        unused,  # type: ignore[arg-type]
    )
