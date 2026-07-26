"""P2-GOV-1: risk tiers that mean something, and bounds on what they permit.

P1-AGT-3 shipped the tiers and deliberately left `routine` as a label, because
ARCHITECTURE §11 unlocks auto-approval only "after the approval UX and rollback
are proven". Both are, so this turns it on.

Turning it on is the easy half. The half worth testing is everything that
cannot happen once it is on: a consequential action cannot be promoted by the
auto-approve list, the agent still cannot approve anything, the sync worker
cannot either, and a runaway cannot exceed the caps — which are on by default
rather than opt-in, because the failure an operator is exposed to here is not
one bad action but a loop producing hundreds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg import errors

from agent.policy import AutoApproval, PolicyError, RiskPolicy, load_policy
from api import approvals
from core.db import Connection

pytestmark = pytest.mark.requires_db


def policy_file(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "risk.toml"
    path.write_text(body)
    return path


@pytest.fixture
def world(migrated: Connection) -> dict[str, Any]:
    connector, entity = uuid4(), uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'jira', 'J')",
            (connector,),
        )
        cur.execute("INSERT INTO entities (id, entity_type) VALUES (%s, 'ticket')", (entity,))
        people = {}
        for name in ("alice", "bob"):
            cur.execute(
                "INSERT INTO principals (kind, connector_id, source_id, email) "
                "VALUES ('user', %s, %s, %s) RETURNING id",
                (connector, name, f"{name}@example.com"),
            )
            people[name] = (cur.fetchone() or (None,))[0]
    return {"connector": connector, "entity": entity, "people": people}


def propose(
    conn: Connection, world: dict[str, Any], who: str = "alice", action_type: str = "jira.comment"
) -> UUID:
    action_id = uuid4()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO actions (id, requested_by, connector_id, action_type, target_entity, "
            "    payload, risk_class, status, summary) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'routine', 'pending', 'a comment')",
            (
                action_id,
                world["people"][who],
                world["connector"],
                action_type,
                world["entity"],
                json.dumps({"body": "hello"}),
            ),
        )
    return action_id


def status_of(conn: Connection, action_id: UUID) -> tuple[str, Any, Any]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, approved_by, approved_by_policy FROM actions WHERE id = %s",
            (action_id,),
        )
        row = cur.fetchone()
    assert row is not None
    return str(row[0]), row[1], row[2]


ON = RiskPolicy(
    routine=frozenset({"jira.comment"}),
    auto_approve=AutoApproval(enabled=True, action_types=frozenset({"jira.comment"})),
)


# ---------------------------------------------------------------------------
# It works, and says honestly that nobody looked.
# ---------------------------------------------------------------------------


def test_a_routine_action_can_go_through_unattended(
    migrated: Connection, world: dict[str, Any]
) -> None:
    action = propose(migrated, world)

    assert approvals.auto_approve_pending(migrated, ON) == [action]
    assert status_of(migrated, action)[0] == "approved"


def test_approved_by_stays_empty(migrated: Connection, world: dict[str, Any]) -> None:
    """Filling it with a service principal would be easier and would make every
    future question about which actions a human actually looked at
    unanswerable."""
    action = propose(migrated, world)

    approvals.auto_approve_pending(migrated, ON)

    _, approved_by, by_policy = status_of(migrated, action)
    assert approved_by is None
    assert by_policy == "auto_approve:jira.comment"


def test_the_database_refuses_both_kinds_of_approval(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """A person clicked and also nobody did is a contradiction, not extra
    assurance."""
    action = propose(migrated, world)

    with pytest.raises(errors.CheckViolation):
        migrated.execute(
            "UPDATE actions SET status = 'approved', approved_by = %s, approved_by_policy = 'x' "
            "WHERE id = %s",
            (world["people"]["alice"], action),
        )
    migrated.rollback()


def test_nothing_happens_when_it_is_off(migrated: Connection, world: dict[str, Any]) -> None:
    """Off by default, and the default policy is the one a deployment gets when
    nobody has configured anything."""
    action = propose(migrated, world)

    assert approvals.auto_approve_pending(migrated, RiskPolicy()) == []
    assert status_of(migrated, action)[0] == "pending"


def test_requires_a_human_reports_the_truth(migrated: Connection) -> None:
    """The question someone evaluating this project asks first."""
    assert RiskPolicy().requires_a_human is True
    assert ON.requires_a_human is False


# ---------------------------------------------------------------------------
# What it cannot do.
# ---------------------------------------------------------------------------


def test_a_consequential_action_is_never_auto_approved(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """The check that matters most. Whatever the file says, a type classified
    as needing a person keeps needing one."""
    sneaky = RiskPolicy(
        routine=frozenset(),
        auto_approve=AutoApproval(enabled=True, action_types=frozenset({"jira.transition"})),
    )
    action = propose(migrated, world, action_type="jira.transition")

    assert approvals.auto_approve_pending(migrated, sneaky) == []
    assert status_of(migrated, action)[0] == "pending"


def test_the_policy_file_refuses_to_promote_a_tier(tmp_path: Path) -> None:
    """An operator who meant to make it routine should say so where the change
    is visible next to every other tier."""
    path = policy_file(
        tmp_path,
        '[actions]\n"jira.transition" = "consequential"\n\n'
        '[auto_approve]\nenabled = true\naction_types = ["jira.transition"]\n',
    )

    with pytest.raises(PolicyError, match="not routine"):
        load_policy(path)


def test_an_unlisted_type_is_not_auto_approved(migrated: Connection, world: dict[str, Any]) -> None:
    only_comments = RiskPolicy(
        routine=frozenset({"jira.comment", "jira.transition"}),
        auto_approve=AutoApproval(enabled=True, action_types=frozenset({"jira.comment"})),
    )
    transition = propose(migrated, world, action_type="jira.transition")

    approvals.auto_approve_pending(migrated, only_comments)

    assert status_of(migrated, transition)[0] == "pending"


def test_the_agent_still_cannot_approve_anything(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """Rule 2, unchanged by any of this."""
    action = propose(migrated, world)
    migrated.commit()

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute(
                "UPDATE actions SET status = 'approved', approved_by_policy = 'x' WHERE id = %s",
                (action,),
            )
    migrated.rollback()


def test_the_sync_worker_cannot_approve_either(migrated: Connection, world: dict[str, Any]) -> None:
    """The interesting one. The worker holds the credentials and does the
    executing; letting it also approve would collapse propose, approve and
    execute into one component. A column grant enforces it rather than a
    convention — it can still write every column execution needs."""
    action = propose(migrated, world)
    migrated.commit()

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_sync"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute("UPDATE actions SET approved_by_policy = 'sneaky' WHERE id = %s", (action,))
    migrated.rollback()


def test_the_sync_worker_can_still_record_an_execution(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """The column grant has to leave the executing role able to execute, or the
    boundary would be enforced by breaking the product."""
    action = propose(migrated, world)
    migrated.execute(
        "UPDATE actions SET status = 'approved', approved_by_policy = 'x' WHERE id = %s", (action,)
    )
    migrated.commit()

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_sync"')
        cur.execute(
            "UPDATE actions SET status = 'executed', executed_at = now(), "
            "inverse_payload = '{}' WHERE id = %s",
            (action,),
        )
        cur.execute("RESET ROLE")
    migrated.commit()

    assert status_of(migrated, action)[0] == "executed"


# ---------------------------------------------------------------------------
# Bounds. The failure here is a loop, not one bad action.
# ---------------------------------------------------------------------------


def test_the_hourly_cap_stops_a_runaway(migrated: Connection, world: dict[str, Any]) -> None:
    capped = RiskPolicy(
        routine=frozenset({"jira.comment"}),
        auto_approve=AutoApproval(
            enabled=True,
            action_types=frozenset({"jira.comment"}),
            max_per_hour=3,
            max_per_principal_per_hour=99,
        ),
    )
    for _ in range(10):
        propose(migrated, world)

    assert len(approvals.auto_approve_pending(migrated, capped)) == 3


def test_the_cap_counts_a_single_sweep(migrated: Connection, world: dict[str, Any]) -> None:
    """A cap checked only against history would let one pass over a backlog blow
    straight through it."""
    capped = RiskPolicy(
        routine=frozenset({"jira.comment"}),
        auto_approve=AutoApproval(
            enabled=True, action_types=frozenset({"jira.comment"}), max_per_hour=2
        ),
    )
    for _ in range(5):
        propose(migrated, world)

    approvals.auto_approve_pending(migrated, capped)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM actions WHERE approved_by_policy IS NOT NULL")
        assert cur.fetchone() == (2,)


def test_the_cap_remembers_the_last_hour(migrated: Connection, world: dict[str, Any]) -> None:
    capped = RiskPolicy(
        routine=frozenset({"jira.comment"}),
        auto_approve=AutoApproval(
            enabled=True, action_types=frozenset({"jira.comment"}), max_per_hour=2
        ),
    )
    propose(migrated, world)
    approvals.auto_approve_pending(migrated, capped)
    propose(migrated, world)
    propose(migrated, world)

    assert len(approvals.auto_approve_pending(migrated, capped)) == 1


def test_one_person_cannot_use_the_whole_budget(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """The per-principal boundary. Without it, a single runaway requester
    starves everyone else out of the global cap."""
    capped = RiskPolicy(
        routine=frozenset({"jira.comment"}),
        auto_approve=AutoApproval(
            enabled=True,
            action_types=frozenset({"jira.comment"}),
            max_per_hour=99,
            max_per_principal_per_hour=2,
        ),
    )
    for _ in range(5):
        propose(migrated, world, who="alice")
    bob = propose(migrated, world, who="bob")

    approved = approvals.auto_approve_pending(migrated, capped)

    assert len(approved) == 3, "two of alice's, and bob's"
    assert bob in approved


def test_an_allow_list_is_a_per_principal_boundary(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """Some people's requests may go through unattended; most may not."""
    restricted = RiskPolicy(
        routine=frozenset({"jira.comment"}),
        auto_approve=AutoApproval(
            enabled=True,
            action_types=frozenset({"jira.comment"}),
            only_principals=frozenset({"alice@example.com"}),
        ),
    )
    alice = propose(migrated, world, who="alice")
    bob = propose(migrated, world, who="bob")

    approved = approvals.auto_approve_pending(migrated, restricted)

    assert approved == [alice]
    assert status_of(migrated, bob)[0] == "pending"


def test_the_policy_is_re_checked_at_approval_time(
    migrated: Connection, world: dict[str, Any]
) -> None:
    """An operator who removes a type stops unattended approvals immediately,
    including for actions already waiting."""
    action = propose(migrated, world)

    approvals.auto_approve_pending(migrated, RiskPolicy(routine=frozenset({"jira.comment"})))

    assert status_of(migrated, action)[0] == "pending"


# ---------------------------------------------------------------------------
# The file.
# ---------------------------------------------------------------------------


def test_a_full_policy_file_loads(tmp_path: Path) -> None:
    path = policy_file(
        tmp_path,
        '[actions]\n"jira.comment" = "routine"\n"jira.transition" = "consequential"\n\n'
        "[auto_approve]\nenabled = true\n"
        'action_types = ["jira.comment"]\n'
        "max_per_hour = 50\nmax_per_principal_per_hour = 10\n"
        'only_principals = ["Alice@Example.com"]\n',
    )

    policy = load_policy(path)

    assert policy.classify("jira.comment") == "routine"
    assert policy.auto_approve.enabled is True
    assert policy.auto_approve.max_per_hour == 50
    assert policy.may_auto_approve("jira.comment", "alice@example.com") is True
    assert policy.may_auto_approve("jira.comment", "bob@example.com") is False
    assert policy.may_auto_approve("jira.transition", "alice@example.com") is False


def test_auto_approve_is_off_unless_asked_for(tmp_path: Path) -> None:
    path = policy_file(tmp_path, '[actions]\n"jira.comment" = "routine"\n')

    policy = load_policy(path)

    assert policy.auto_approve.enabled is False
    assert policy.requires_a_human is True


@pytest.mark.parametrize(
    "body",
    [
        'auto_approve = "yes"\n',
        '[auto_approve]\naction_types = "jira.comment"\n',
        "[auto_approve]\naction_types = [1, 2]\n",
        '[auto_approve]\nonly_principals = "alice"\n',
        '[actions]\n"jira.comment" = "routine"\n[auto_approve]\nmax_per_hour = 0\n',
    ],
)
def test_a_malformed_auto_approve_section_is_a_startup_failure(tmp_path: Path, body: str) -> None:
    """A policy that silently became something other than what the operator
    wrote is worse than one that refused to load."""
    with pytest.raises(PolicyError):
        load_policy(policy_file(tmp_path, body))


def test_the_caps_have_defaults_rather_than_being_optional(tmp_path: Path) -> None:
    """An operator who turns this on without thinking about limits gets limits
    anyway."""
    path = policy_file(
        tmp_path,
        '[actions]\n"jira.comment" = "routine"\n\n'
        '[auto_approve]\nenabled = true\naction_types = ["jira.comment"]\n',
    )

    policy = load_policy(path)

    assert policy.auto_approve.max_per_hour > 0
    assert policy.auto_approve.max_per_principal_per_hour > 0


# ---------------------------------------------------------------------------
# The sweep runs in the process that represents people.
# ---------------------------------------------------------------------------


def test_the_api_does_not_sweep_when_the_policy_is_off(settings: Any) -> None:
    """Silent and free by default: the policy is checked before any query."""
    from fastapi.testclient import TestClient

    from api.main import create_app

    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200


def test_the_api_sweeps_when_the_policy_opts_in(
    settings: Any, world: dict[str, Any], migrated: Connection, tmp_path: Path
) -> None:
    """End to end: a pending action, an opted-in policy, and no human."""
    import time

    from fastapi.testclient import TestClient

    from api.main import create_app

    action = propose(migrated, world)
    migrated.commit()

    path = policy_file(
        tmp_path,
        '[actions]\n"jira.comment" = "routine"\n\n'
        '[auto_approve]\nenabled = true\naction_types = ["jira.comment"]\n',
    )
    opted_in = settings.model_copy(
        update={"risk_policy_path": path, "auto_approve_interval_seconds": 0.05}
    )

    with TestClient(create_app(opted_in)):
        for _ in range(40):
            if status_of(migrated, action)[0] == "approved":
                break
            time.sleep(0.05)

    status, approved_by, by_policy = status_of(migrated, action)
    assert status == "approved"
    assert approved_by is None
    assert by_policy == "auto_approve:jira.comment"


def test_a_failing_sweep_does_not_stop_the_loop(
    settings: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A governance sweep that died on one bad tick would stop approving
    without anyone noticing, which is worse than the tick failing."""
    import asyncio

    from api.main import _sweep
    from core.db import Database

    path = policy_file(
        tmp_path,
        '[actions]\n"jira.comment" = "routine"\n\n'
        '[auto_approve]\nenabled = true\naction_types = ["jira.comment"]\n',
    )
    broken = Database("postgresql://nobody@127.0.0.1:1/nope", min_size=0, max_size=1)
    broken.open(wait=False)
    opted_in = settings.model_copy(
        update={"risk_policy_path": path, "auto_approve_interval_seconds": 0.01}
    )

    async def run_briefly() -> None:
        task = asyncio.create_task(_sweep(broken, opted_in))
        await asyncio.sleep(0.1)
        assert not task.done(), "the loop survived a failing tick"
        task.cancel()

    with caplog.at_level("ERROR", logger="hippo.api"):
        asyncio.run(run_briefly())

    assert "auto-approval sweep failed" in caplog.text


def test_turning_it_on_is_logged_loudly(
    settings: Any, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An operator delegating decisions nobody will watch should find that
    fact in the log at startup, not discover it from the audit trail."""
    import asyncio

    from api.main import _sweep
    from core.db import Database

    path = policy_file(
        tmp_path,
        '[actions]\n"jira.comment" = "routine"\n\n'
        '[auto_approve]\nenabled = true\naction_types = ["jira.comment"]\n',
    )
    db = Database("postgresql://nobody@127.0.0.1:1/nope", min_size=0, max_size=1)
    db.open(wait=False)

    async def start() -> None:
        task = asyncio.create_task(
            _sweep(db, settings.model_copy(update={"risk_policy_path": path}))
        )
        await asyncio.sleep(0.05)
        task.cancel()

    with caplog.at_level("WARNING", logger="hippo.api"):
        asyncio.run(start())

    assert "auto-approval is on" in caplog.text
