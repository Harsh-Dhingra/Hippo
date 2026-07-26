"""Skills that run without anybody asking.

A standing digest is the first thing in this system that reads content with
nobody present. Every other read is a person asking a question, and the
permission filter answers "what may *you* see". A scheduled run has to answer
the same question, which means it needs a whose — and the whole design of this
module is downstream of insisting on one.

**A schedule runs as a named principal.** Migration 022 makes `runs_as` NOT
NULL with a foreign key, and there is no system principal that sees everything.
The answer a schedule produces is exactly what that person would have got by
asking the question themselves, and it is visible to them and to nobody else.

The tempting alternative is a service account with broad access that posts
summaries into a channel. It is also how a memory system becomes the thing that
leaks: the digest is assembled from everything the service account can read and
delivered to everyone in the channel. Not offering it is the point.

**A run that proposes still waits for a person.** Nothing about being scheduled
changes CLAUDE.md rule 2. A pending row is attributed to the principal the
schedule runs as, and appears on their approvals screen like any other.

**A failing schedule keeps its place.** next_run_at moves forward when the row
is claimed, not when the run succeeds, so a skill that raises every time does
not spin — it fails once per cadence and records why.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from prometheus_client import Counter
from pydantic import BaseModel, ConfigDict, Field

from agent.loop import Agent, Answer
from agent.skills import Skill, SkillError, prepare
from core.db import Connection

LOG = logging.getLogger("hippo.agent.scheduled")

RUNS = Counter("hippo_scheduled_skill_runs_total", "Scheduled skill runs.", ("skill", "outcome"))

CADENCES = ("hourly", "daily", "weekly")


class ScheduleError(Exception):
    """A schedule that cannot be created or run, said so it can be fixed."""


class Schedule(BaseModel):
    """One standing question."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    skill: str
    runs_as: UUID
    inputs: dict[str, str] = Field(default_factory=dict)
    cadence: str = "daily"
    at_hour: int = 9
    at_weekday: int = 1
    enabled: bool = True
    last_run_at: datetime | None = None
    last_error: str | None = None
    next_run_at: datetime | None = None

    @property
    def describes(self) -> str:
        """Readable enough for a UI row and for a log line."""
        if self.cadence == "hourly":
            return "every hour"
        when = f"{self.at_hour:02d}:00 UTC"
        if self.cadence == "daily":
            return f"every day at {when}"
        days = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        return f"every {days[self.at_weekday - 1]} at {when}"


class Due(BaseModel):
    """A schedule the database has just handed to this worker."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    skill: str
    runs_as: UUID
    inputs: dict[str, str] = Field(default_factory=dict)


def create(
    conn: Connection,
    *,
    skill: Skill,
    runs_as: UUID,
    inputs: dict[str, str] | None = None,
    cadence: str = "daily",
    at_hour: int = 9,
    at_weekday: int = 1,
    created_by: UUID | None = None,
) -> Schedule:
    """Record a standing question, after checking it can actually be asked.

    The inputs are rendered here rather than at the first run. A schedule whose
    inputs do not satisfy its skill would otherwise sit quietly until the hour
    it was meant to fire and then fail, which is the worst moment to find out.
    """
    if cadence not in CADENCES:
        raise ScheduleError(f"cadence must be one of {', '.join(CADENCES)}")

    try:
        prepare(skill, inputs)
    except SkillError as exc:
        raise ScheduleError(str(exc)) from exc

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO skill_schedules "
            "    (skill, runs_as, inputs, cadence, at_hour, at_weekday, created_by, next_run_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, next_skill_run(%s, %s, %s, now())) "
            "RETURNING id, next_run_at",
            (
                skill.name,
                runs_as,
                _json(inputs or {}),
                cadence,
                at_hour,
                at_weekday,
                created_by,
                cadence,
                at_hour,
                at_weekday,
            ),
        )
        row = cur.fetchone()
    assert row is not None

    LOG.info(
        "schedule created",
        extra={"skill": skill.name, "runs_as": str(runs_as), "cadence": cadence},
    )
    return Schedule(
        id=UUID(str(row[0])),
        skill=skill.name,
        runs_as=runs_as,
        inputs=dict(inputs or {}),
        cadence=cadence,
        at_hour=at_hour,
        at_weekday=at_weekday,
        next_run_at=row[1],
    )


def _json(value: dict[str, str]) -> Any:
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def claim(conn: Connection, limit: int = 20) -> list[Due]:
    """Take what is due, moving each schedule forward as it is taken.

    Forward on claim rather than on success, so a skill that raises every time
    fails once per cadence instead of spinning. The cost is that a run lost to
    a crashed worker waits for the next tick, which for a daily digest is the
    right trade: a duplicate digest is worse than a late one.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, skill, runs_as, inputs FROM claim_due_skills(%s)", (limit,))
        rows = cur.fetchall()

    return [
        Due(
            id=UUID(str(row[0])),
            skill=str(row[1]),
            runs_as=UUID(str(row[2])),
            inputs={str(k): str(v) for k, v in dict(row[3] or {}).items()},
        )
        for row in rows
    ]


def run_one(
    conn: Connection,
    due: Due,
    skills: dict[str, Skill],
    agent: Agent,
) -> Answer | None:
    """Run one claimed schedule as the principal it names.

    Returns the answer, or None when it could not run. Never raises: one broken
    schedule must not stop the others, and the reason is recorded on the row
    where whoever owns it will see it.
    """
    skill = skills.get(due.skill)
    if skill is None:
        # A skill removed from disk while a schedule still names it. Reported
        # rather than deleted: the file may be coming back, and deleting
        # somebody's standing question because of a deploy order is worse.
        _record_failure(conn, due, f"no skill named {due.skill!r} is installed")
        RUNS.labels(skill=due.skill, outcome="missing").inc()
        return None

    try:
        run = prepare(skill, due.inputs)
    except SkillError as exc:
        _record_failure(conn, due, str(exc))
        RUNS.labels(skill=due.skill, outcome="invalid").inc()
        return None

    try:
        answer = agent.answer(
            conn,
            due.runs_as,
            run.question,
            k=skill.k,
            # The same narrowing an interactive run gets. A scheduled skill has
            # no more authority than the person it runs as, and no more than
            # its own declaration.
            allow=frozenset(skill.actions),
        )
    except Exception as exc:
        _record_failure(conn, due, f"{type(exc).__name__}: {exc}")
        RUNS.labels(skill=due.skill, outcome="failed").inc()
        LOG.error(
            "scheduled skill failed",
            extra={"skill": due.skill, "runs_as": str(due.runs_as), "error": str(exc)[:200]},
        )
        return None

    _record_success(conn, due)
    RUNS.labels(skill=due.skill, outcome="ok").inc()
    LOG.info(
        "scheduled skill ran",
        extra={
            "skill": due.skill,
            "runs_as": str(due.runs_as),
            "proposed": answer.proposal is not None,
            "trace": None if answer.trace_id is None else str(answer.trace_id),
        },
    )
    return answer


def run_due(
    conn: Connection,
    skills: dict[str, Skill],
    agent: Agent,
    *,
    limit: int = 20,
) -> int:
    """Claim and run everything due. Returns how many ran successfully."""
    due = claim(conn, limit)
    if not due:
        return 0
    return sum(1 for item in due if run_one(conn, item, skills, agent) is not None)


def _record_success(conn: Connection, due: Due) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE skill_schedules SET last_run_at = now(), last_error = NULL WHERE id = %s",
            (due.id,),
        )


def _record_failure(conn: Connection, due: Due, reason: str) -> None:
    """Kept on the row, not only in the log.

    Whoever owns a standing question is the person who needs to know it has
    been failing, and they are not reading the worker's logs.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE skill_schedules SET last_run_at = now(), last_error = %s WHERE id = %s",
            (reason[:500], due.id),
        )
    LOG.warning("scheduled skill did not run", extra={"skill": due.skill, "reason": reason[:200]})


def for_principal(conn: Connection, principal_id: UUID) -> list[Schedule]:
    """Somebody's own standing questions.

    Scoped through my_schedules(), which expands to every account the same
    person holds — a schedule created from their Jira login should be visible
    from their Slack one.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM my_schedules(%s)", (principal_id,))
        columns = [description.name for description in cur.description or []]
        return [
            Schedule.model_validate(dict(zip(columns, row, strict=True))) for row in cur.fetchall()
        ]


def set_enabled(conn: Connection, schedule_id: UUID, principal_id: UUID, enabled: bool) -> bool:
    """Pause or resume, but only your own.

    The principal check is in the statement rather than in a prior read, so
    there is no window between deciding it is yours and changing it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE skill_schedules SET enabled = %s "
            "WHERE id = %s AND runs_as IN (SELECT principal_id FROM _same_person(%s))",
            (enabled, schedule_id, principal_id),
        )
        return cur.rowcount > 0


def delete(conn: Connection, schedule_id: UUID, principal_id: UUID) -> bool:
    """Remove one, again only your own."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM skill_schedules "
            "WHERE id = %s AND runs_as IN (SELECT principal_id FROM _same_person(%s))",
            (schedule_id, principal_id),
        )
        return cur.rowcount > 0
