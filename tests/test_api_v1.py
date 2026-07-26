"""P1-SRF-1's done-condition: the v1 surface, driven over HTTP.

These are integration tests in the sense that matters — a real app, a real
database, real migrations, and a request that goes through auth, the permission
filter and the trace writer on its way to a response. The model is the only
double, because CI must not call one.

The load-bearing tests are the ones about what a request cannot do: reach
another person's answer, approve twice, or read content while pretending to be
a different principal.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from agent.loop import Agent
from agent.providers.base import Completion, CompletionRequest, Usage
from api import auth
from api.main import create_app
from core.config import Settings
from core.db import Connection
from resolver.embeddings import HashingEmbeddings
from sync.connectors.jira import FixtureTransport as JiraFixtures
from sync.connectors.jira import JiraConnector
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime, project_acl_grants
from tests.pipeline import resolve_and_enrich

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PRIVATE_TEXT = "Acme is asking for 30 percent off to renew, do not repeat outside this channel"
ALICE_EMAIL = "alice@example.com"
CAROL_EMAIL = "carol@example.com"
PASSWORD = "correct horse battery staple"


class ScriptedModel:
    """Cites everything it is given, so the citation path is exercised."""

    name = "scripted"
    model = "scripted-1"

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.requests: list[CompletionRequest] = []

    @property
    def last_prompt(self) -> str:
        return self.requests[-1].messages[0].content

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        text = self.reply
        if text is None:
            markers = " ".join(
                f"[{i}]" for i in range(1, request.messages[0].content.count("<source ") + 1)
            )
            text = f"Here is what I found. {markers}"
        return Completion(
            text=text,
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=100, output_tokens=20),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@pytest.fixture
def model() -> ScriptedModel:
    return ScriptedModel()


@pytest.fixture
def client(
    settings: Settings, model: ScriptedModel, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    """The real app, with the model replaced and the offline embedder.

    Patched at the point where main.py builds them, so everything between the
    HTTP boundary and the database is the code that ships.
    """
    monkeypatch.setattr("api.main.build_provider", lambda _settings: model)
    monkeypatch.setattr("api.main.build_embeddings", lambda _settings: HashingEmbeddings())
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def world(client: TestClient, settings: Settings) -> Iterator[Connection]:
    """A synced workspace and two registered users."""
    from core.db import connect

    with connect(settings.database_url) as conn:
        slack_id, jira_id = uuid4(), uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO connectors (id, kind, display_name, config) VALUES "
                "(%s, 'slack', 'Slack', '{\"workspace_url\": \"https://acme.slack.com\"}'), "
                "(%s, 'jira', 'Jira', '{\"base_url\": \"https://acme.atlassian.net\"}')",
                (slack_id, jira_id),
            )
        SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(conn)
        SyncRuntime(JiraConnector(JiraFixtures(FIXTURES / "jira")), jira_id).sync_all(conn)
        resolve_and_enrich(conn)
        project_acl_grants(conn, slack_id)
        project_acl_grants(conn, jira_id)
        auth.create_user(conn, ALICE_EMAIL, PASSWORD, display_name="Alice")
        auth.create_user(conn, CAROL_EMAIL, PASSWORD, display_name="Carol")
        conn.commit()
        yield conn


def token_for(client: TestClient, email: str) -> str:
    response = client.post("/api/v1/sessions", json={"email": email, "password": PASSWORD})
    assert response.status_code == 201, response.text
    return str(response.json()["token"])


def headers(client: TestClient, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token_for(client, email)}"}


# ---------------------------------------------------------------------------
# Sessions.
# ---------------------------------------------------------------------------


def test_logging_in_returns_a_token(client: TestClient, world: Connection) -> None:
    response = client.post("/api/v1/sessions", json={"email": ALICE_EMAIL, "password": PASSWORD})

    assert response.status_code == 201
    body = response.json()
    assert body["token"]
    assert body["user"]["email"] == ALICE_EMAIL
    assert body["user"]["has_access"] is True


def test_the_token_is_never_stored(client: TestClient, world: Connection) -> None:
    """A dump of the sessions table must yield nothing replayable."""
    token = token_for(client, ALICE_EMAIL)

    with world.cursor() as cur:
        cur.execute("SELECT token_hash FROM sessions")
        stored = {str(row[0]) for row in cur.fetchall()}

    assert token not in stored
    assert auth.hash_token(token) in stored


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (ALICE_EMAIL, "wrong password"),
        ("nobody@example.com", PASSWORD),
        (ALICE_EMAIL, "CORRECT HORSE BATTERY STAPLE"),
    ],
)
def test_a_bad_login_is_rejected_identically(
    client: TestClient, world: Connection, email: str, password: str
) -> None:
    """An unknown address and a wrong password give the same answer, or the
    login form is a way to enumerate accounts."""
    response = client.post("/api/v1/sessions", json={"email": email, "password": password})

    assert response.status_code == 401
    assert response.json()["detail"] == "invalid email or password"


def test_an_email_is_matched_case_insensitively(client: TestClient, world: Connection) -> None:
    response = client.post(
        "/api/v1/sessions", json={"email": "ALICE@Example.com ", "password": PASSWORD}
    )

    assert response.status_code == 201


def test_me_needs_a_token(client: TestClient, world: Connection) -> None:
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_logging_out_ends_the_session(client: TestClient, world: Connection) -> None:
    auth_headers = headers(client, ALICE_EMAIL)
    assert client.get("/api/v1/me", headers=auth_headers).status_code == 200

    assert client.delete("/api/v1/sessions/current", headers=auth_headers).status_code == 204

    assert client.get("/api/v1/me", headers=auth_headers).status_code == 401


def test_logging_out_everywhere_ends_every_session(client: TestClient, world: Connection) -> None:
    """What a lost laptop needs."""
    first = headers(client, ALICE_EMAIL)
    second = headers(client, ALICE_EMAIL)

    assert client.delete("/api/v1/sessions", headers=first).status_code == 204

    assert client.get("/api/v1/me", headers=second).status_code == 401


def test_a_disabled_account_is_refused_immediately(client: TestClient, world: Connection) -> None:
    """Checked per request, not per login: disabling someone has to take effect
    now, not whenever their session happens to expire."""
    auth_headers = headers(client, ALICE_EMAIL)
    with world.cursor() as cur:
        cur.execute("UPDATE users SET disabled_at = now() WHERE email = %s", (ALICE_EMAIL,))
    world.commit()

    assert client.get("/api/v1/me", headers=auth_headers).status_code == 401


def test_a_user_with_no_principal_can_log_in_but_sees_nothing(
    client: TestClient, world: Connection, settings: Settings
) -> None:
    """Signing up grants nothing. 403 rather than an empty answer, because
    'you have access to nothing' and 'nothing matched' are different facts."""
    auth.create_user(world, "stranger@example.com", PASSWORD)
    world.commit()

    login = client.post(
        "/api/v1/sessions", json={"email": "stranger@example.com", "password": PASSWORD}
    )
    assert login.status_code == 201
    assert login.json()["user"]["has_access"] is False

    auth_headers = {"Authorization": f"Bearer {login.json()['token']}"}
    assert client.get("/api/v1/me", headers=auth_headers).status_code == 200
    assert (
        client.post("/api/v1/queries", json={"question": "anything"}, headers=auth_headers)
    ).status_code == 403


# ---------------------------------------------------------------------------
# Queries. §12 points 1 and 2, over HTTP.
# ---------------------------------------------------------------------------


def test_a_question_returns_a_cited_answer(client: TestClient, world: Connection) -> None:
    response = client.post(
        "/api/v1/queries",
        json={"question": "What is blocking the Acme renewal?", "k": 40},
        headers=headers(client, ALICE_EMAIL),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"]
    assert body["citations"]
    assert body["trace_id"]


def test_citations_carry_links_that_resolve(client: TestClient, world: Connection) -> None:
    response = client.post(
        "/api/v1/queries",
        json={"question": "What is blocking the Acme renewal?", "k": 40},
        headers=headers(client, ALICE_EMAIL),
    )

    linked = [c for c in response.json()["citations"] if c["url"]]
    assert linked
    for citation in linked:
        assert citation["url"].startswith(("https://acme.slack.com", "https://acme.atlassian.net"))


def test_the_filtered_path_over_http(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """The demo. Carol is not in #deals-acme, and the private content never
    reaches the prompt her request built."""
    client.post(
        "/api/v1/queries",
        json={"question": "What is blocking the Acme renewal?", "k": 40},
        headers=headers(client, CAROL_EMAIL),
    )

    assert PRIVATE_TEXT not in model.last_prompt


def test_the_same_question_reaches_a_member(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    client.post(
        "/api/v1/queries",
        json={"question": "What is Acme asking for on the renewal?", "k": 40},
        headers=headers(client, ALICE_EMAIL),
    )

    assert PRIVATE_TEXT in model.last_prompt


def test_a_query_runs_under_the_agent_role(client: TestClient, world: Connection) -> None:
    """The privilege reduction is the point: hippo_api can read entities, and a
    prompt built under that role would have a second path to content."""
    seen: list[str] = []
    original = Agent.answer

    def spy(self: Agent, conn: Connection, *args: Any, **kwargs: Any) -> Any:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user")
            seen.append(str((cur.fetchone() or ("?",))[0]))
        return original(self, conn, *args, **kwargs)

    Agent.answer = spy  # type: ignore[method-assign]
    try:
        client.post(
            "/api/v1/queries",
            json={"question": "renewal", "k": 5},
            headers=headers(client, ALICE_EMAIL),
        )
    finally:
        Agent.answer = original  # type: ignore[method-assign]

    assert seen == ["hippo_agent"]


def test_the_role_reduction_does_not_outlive_the_request(
    client: TestClient, world: Connection
) -> None:
    """SET LOCAL, so a pooled connection cannot carry it into the next
    request — where it would break a route that legitimately needs to read."""
    auth_headers = headers(client, ALICE_EMAIL)
    client.post("/api/v1/queries", json={"question": "renewal", "k": 5}, headers=auth_headers)

    assert client.get("/api/v1/me", headers=auth_headers).status_code == 200
    assert client.get("/api/v1/actions", headers=auth_headers).status_code == 200


def test_a_question_is_required(client: TestClient, world: Connection) -> None:
    response = client.post(
        "/api/v1/queries", json={"question": ""}, headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 422


def test_k_is_bounded_by_the_schema(client: TestClient, world: Connection) -> None:
    response = client.post(
        "/api/v1/queries",
        json={"question": "renewal", "k": 10_000},
        headers=headers(client, ALICE_EMAIL),
    )

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Actions. §12 point 3, first half.
# ---------------------------------------------------------------------------


def propose(client: TestClient, world: Connection, model: ScriptedModel) -> dict[str, Any]:
    """Ask for a comment, and return the proposal the API reports."""
    ask = "Add a comment on ACME-1 summarising this"
    marker = _jira_marker(world, ALICE_EMAIL, ask)
    model.reply = json.dumps(
        {"action_type": "jira.comment", "source": marker, "payload": {"body": "legal review"}}
    )
    response = client.post(
        "/api/v1/queries",
        json={"question": ask, "k": 40},
        headers=headers(client, ALICE_EMAIL),
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["proposal"] is not None, body
    return body


def test_a_request_returns_a_pending_proposal(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    body = propose(client, world, model)

    assert body["proposal"]["status"] == "pending"
    assert body["proposal"]["risk_class"] == "consequential"
    assert "waiting for your approval" in body["answer"]


def test_the_proposal_appears_in_the_action_list(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]

    response = client.get(
        "/api/v1/actions", params={"action_status": "pending"}, headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 200
    assert [action["id"] for action in response.json()] == [proposal["id"]]


def test_approving_records_the_approver_and_executes_nothing(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """ARCHITECTURE §4: approval is a status change. The sync worker holds the
    credentials and does the rest."""
    proposal = propose(client, world, model)["proposal"]

    response = client.post(
        f"/api/v1/actions/{proposal['id']}/approve", headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "approved"
    assert body["approved_by"] is not None
    with world.cursor() as cur:
        cur.execute("SELECT executed_at, inverse_payload FROM actions WHERE id = %s", (body["id"],))
        assert cur.fetchone() == (None, None)


def test_approving_twice_is_a_conflict(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """The row's own state is the lock. Two people clicking at once cannot
    produce two approvals."""
    proposal = propose(client, world, model)["proposal"]
    auth_headers = headers(client, ALICE_EMAIL)
    client.post(f"/api/v1/actions/{proposal['id']}/approve", headers=auth_headers)

    second = client.post(f"/api/v1/actions/{proposal['id']}/approve", headers=auth_headers)

    assert second.status_code == 409
    assert "approved" in second.json()["detail"]


def test_declining_is_not_failing(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """An action nobody wanted and an action that broke are different events,
    and only one of them is worth investigating."""
    proposal = propose(client, world, model)["proposal"]

    response = client.post(
        f"/api/v1/actions/{proposal['id']}/decline", headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    assert response.json()["declined_by"] is not None
    assert response.json()["error"] is None


def test_a_declined_action_cannot_then_be_approved(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]
    auth_headers = headers(client, ALICE_EMAIL)
    client.post(f"/api/v1/actions/{proposal['id']}/decline", headers=auth_headers)

    response = client.post(f"/api/v1/actions/{proposal['id']}/approve", headers=auth_headers)

    assert response.status_code == 409


def test_you_cannot_approve_someone_elses_action(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]

    response = client.post(
        f"/api/v1/actions/{proposal['id']}/approve", headers=headers(client, CAROL_EMAIL)
    )

    assert response.status_code == 404, "and 404, not 403: existence is not confirmed"


def test_declining_a_declined_action_is_a_conflict(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]
    auth_headers = headers(client, ALICE_EMAIL)
    client.post(f"/api/v1/actions/{proposal['id']}/decline", headers=auth_headers)

    second = client.post(f"/api/v1/actions/{proposal['id']}/decline", headers=auth_headers)

    assert second.status_code == 409


def test_one_action_can_be_fetched_on_its_own(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """What the approval screen loads."""
    proposal = propose(client, world, model)["proposal"]

    response = client.get(f"/api/v1/actions/{proposal['id']}", headers=headers(client, ALICE_EMAIL))

    assert response.status_code == 200
    assert response.json()["payload"] == {"body": "legal review"}


def test_you_cannot_fetch_someone_elses_action(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]

    response = client.get(f"/api/v1/actions/{proposal['id']}", headers=headers(client, CAROL_EMAIL))

    assert response.status_code == 404


def test_the_agent_is_built_once(client: TestClient, world: Connection) -> None:
    """Rebuilding the connector directory per request would be a query per
    question for data that changes when an operator adds a connector."""
    auth_headers = headers(client, ALICE_EMAIL)
    first = client.post(
        "/api/v1/queries", json={"question": "renewal", "k": 5}, headers=auth_headers
    )
    second = client.post(
        "/api/v1/queries", json={"question": "renewal again", "k": 5}, headers=auth_headers
    )

    assert first.status_code == second.status_code == 200


def test_an_unknown_action_is_not_found(client: TestClient, world: Connection) -> None:
    response = client.post(
        f"/api/v1/actions/{uuid4()}/approve", headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 404


def test_the_api_cannot_create_an_action_of_its_own(client: TestClient, world: Connection) -> None:
    """There is no POST /actions, and there should not be: a proposal comes
    from the agent, and an API that could mint its own would make the
    propose/approve split decorative."""
    assert (
        client.post("/api/v1/actions", json={}, headers=headers(client, ALICE_EMAIL)).status_code
        == 405
    )


def test_the_full_demo_loop(client: TestClient, world: Connection, model: ScriptedModel) -> None:
    """ARCHITECTURE §12 point 3, end to end over HTTP.

    Ask, approve, execute, roll back. The execution and the undo run through
    the write-back executor with a fixture Jira, because CI does not call an
    API — but everything between the HTTP boundary and the connector is the
    code that ships.
    """
    from pathlib import Path as _Path

    from sync.connectors.jira import FixtureTransport as _Fixtures
    from sync.connectors.jira import JiraConnector as _Jira
    from sync.writeback import execute_action, rollback_action

    auth_headers = headers(client, ALICE_EMAIL)
    proposal = propose(client, world, model)["proposal"]
    assert proposal["status"] == "pending"

    approved = client.post(f"/api/v1/actions/{proposal['id']}/approve", headers=auth_headers)
    assert approved.json()["status"] == "approved"

    # The sync worker's half. It holds the Jira credential; the API does not.
    transport = _Fixtures(_Path(__file__).resolve().parent / "fixtures" / "jira")
    factory = lambda _conn, _id: _Jira(transport)  # noqa: E731
    world.commit()
    assert execute_action(world, UUID(proposal["id"]), factory) == "executed"
    world.commit()
    assert transport.writes, "the comment reached Jira"

    executed = client.get(f"/api/v1/actions/{proposal['id']}", headers=auth_headers)
    assert executed.json()["status"] == "executed"

    requested = client.post(f"/api/v1/actions/{proposal['id']}/rollback", headers=auth_headers)
    assert requested.status_code == 200

    principal_id = auth.find_principal_for(world, ALICE_EMAIL)
    assert principal_id is not None
    assert rollback_action(world, UUID(proposal["id"]), principal_id, factory) == "rolled_back"
    world.commit()
    assert transport.deletes, "and the comment is gone again"

    final = client.get(f"/api/v1/actions/{proposal['id']}", headers=auth_headers)
    assert final.json()["status"] == "rolled_back"
    assert final.json()["rolled_back_by"] is not None


def test_requesting_a_rollback_enqueues_the_work(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """The API records the request; the worker performs it. A status that moved
    here would send someone looking for a change that is still live."""
    proposal = propose(client, world, model)["proposal"]
    auth_headers = headers(client, ALICE_EMAIL)
    client.post(f"/api/v1/actions/{proposal['id']}/approve", headers=auth_headers)
    world.execute(
        "UPDATE actions SET status = 'executed', executed_at = now(), "
        "inverse_payload = '{}' WHERE id = %s",
        (proposal["id"],),
    )
    world.commit()

    response = client.post(f"/api/v1/actions/{proposal['id']}/rollback", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["status"] == "executed", "not yet undone"
    with world.cursor() as cur:
        cur.execute("SELECT count(*) FROM jobs WHERE kind = 'action.rollback'")
        assert cur.fetchone() == (1,)


def test_only_an_executed_action_can_be_rolled_back(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]

    response = client.post(
        f"/api/v1/actions/{proposal['id']}/rollback", headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 409


def test_you_cannot_roll_back_someone_elses_action(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    proposal = propose(client, world, model)["proposal"]

    response = client.post(
        f"/api/v1/actions/{proposal['id']}/rollback", headers=headers(client, CAROL_EMAIL)
    )

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Traces. §12 point 4.
# ---------------------------------------------------------------------------


def test_a_query_is_retrievable_as_a_trace(client: TestClient, world: Connection) -> None:
    auth_headers = headers(client, ALICE_EMAIL)
    trace_id = client.post(
        "/api/v1/queries",
        json={"question": "What is blocking the Acme renewal?", "k": 40},
        headers=auth_headers,
    ).json()["trace_id"]

    response = client.get(f"/api/v1/traces/{trace_id}", headers=auth_headers)

    assert response.status_code == 200
    trace = response.json()
    assert [step["name"] for step in trace["steps"]] == ["plan", "retrieve", "synthesize"]
    assert trace["retrievals"]
    assert trace["system_prompt"]


def test_the_trace_list_is_scoped_to_you(client: TestClient, world: Connection) -> None:
    client.post(
        "/api/v1/queries",
        json={"question": "renewal", "k": 5},
        headers=headers(client, ALICE_EMAIL),
    )

    assert client.get("/api/v1/traces", headers=headers(client, ALICE_EMAIL)).json()
    assert client.get("/api/v1/traces", headers=headers(client, CAROL_EMAIL)).json() == []


def test_you_cannot_read_someone_elses_trace(client: TestClient, world: Connection) -> None:
    trace_id = client.post(
        "/api/v1/queries",
        json={"question": "renewal", "k": 5},
        headers=headers(client, ALICE_EMAIL),
    ).json()["trace_id"]

    response = client.get(f"/api/v1/traces/{trace_id}", headers=headers(client, CAROL_EMAIL))

    assert response.status_code == 404


def test_a_missing_trace_and_a_forbidden_one_look_the_same(
    client: TestClient, world: Connection
) -> None:
    trace_id = client.post(
        "/api/v1/queries",
        json={"question": "renewal", "k": 5},
        headers=headers(client, ALICE_EMAIL),
    ).json()["trace_id"]
    carol = headers(client, CAROL_EMAIL)

    forbidden = client.get(f"/api/v1/traces/{trace_id}", headers=carol)
    missing = client.get(f"/api/v1/traces/{uuid4()}", headers=carol)

    assert forbidden.status_code == missing.status_code
    assert forbidden.json() == missing.json()


# ---------------------------------------------------------------------------
# The spec itself.
# ---------------------------------------------------------------------------


def test_the_openapi_document_covers_every_v1_route(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    assert set(spec["paths"]) >= {
        "/api/v1/sessions",
        "/api/v1/sessions/current",
        "/api/v1/me",
        "/api/v1/queries",
        "/api/v1/actions",
        "/api/v1/actions/{action_id}",
        "/api/v1/actions/{action_id}/approve",
        "/api/v1/actions/{action_id}/decline",
        "/api/v1/actions/{action_id}/rollback",
        "/api/v1/notes",
        "/api/v1/notes/{note_id}",
        "/api/v1/notes/{note_id}/pin",
        "/api/v1/notes/{note_id}/supersede",
        "/api/v1/scopes",
        "/api/v1/timeline/{entity_id}",
        "/api/v1/traces",
        "/api/v1/traces/{trace_id}",
        "/healthz",
    }


def test_the_spec_documents_response_shapes(client: TestClient) -> None:
    spec = client.get("/openapi.json").json()

    schemas = spec["components"]["schemas"]
    assert "QueryResponse" in schemas
    assert "ActionResponse" in schemas
    assert set(schemas["QueryResponse"]["properties"]) >= {"answer", "citations", "trace_id"}


def test_the_spec_says_what_approval_does_not_do(client: TestClient) -> None:
    """The one thing a reader of this API most needs to know."""
    spec = client.get("/openapi.json").json()

    description = spec["paths"]["/api/v1/actions/{action_id}/approve"]["post"]["description"]
    assert "Nothing is executed here" in description


def test_every_v1_route_requires_authentication(client: TestClient, world: Connection) -> None:
    """Swept rather than listed, so a route added without auth fails here
    instead of shipping."""
    spec = client.get("/openapi.json").json()

    for path, methods in spec["paths"].items():
        if not path.startswith("/api/v1") or path == "/api/v1/sessions":
            continue
        for method in methods:
            response = client.request(
                method,
                path.replace("{action_id}", str(uuid4())).replace("{trace_id}", str(uuid4())),
                json={},
            )
            assert response.status_code == 401, f"{method.upper()} {path} did not require auth"


def _jira_marker(conn: Connection, email: str, question: str) -> int:
    from agent.retrieval import plan_query, retrieve

    principal_id = auth.find_principal_for(conn, email)
    assert principal_id is not None
    hits = retrieve(conn, principal_id, plan_query(question, k=40), HashingEmbeddings())
    for index, hit in enumerate(hits, start=1):
        if hit.source_type == "jira.issue":
            return index
    raise AssertionError("no jira issue retrieved")


def test_your_actions_are_yours_through_any_account(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """Migration 012, over HTTP. The agent records an action against whichever
    principal asked; a login resolves to whichever of that person's principals
    sorts first. Before this, a person's own proposal was invisible to them."""
    from resolver.resolution import link_principal_identities

    proposal = propose(client, world, model)["proposal"]
    with world.cursor() as cur:
        cur.execute("SELECT requested_by FROM actions WHERE id = %s", (proposal["id"],))
        recorded = (cur.fetchone() or (None,))[0]
    link_principal_identities(world)
    # Point the login at a different one of Alice's accounts than the agent used.
    with world.cursor() as cur:
        cur.execute(
            "UPDATE users SET principal_id = ("
            "  SELECT id FROM principals WHERE kind = 'user' "
            "  AND lower(btrim(email)) = %s AND id <> %s LIMIT 1"
            ") WHERE email = %s",
            (ALICE_EMAIL, recorded, ALICE_EMAIL),
        )
    world.commit()

    listed = client.get("/api/v1/actions", headers=headers(client, ALICE_EMAIL))

    assert [action["id"] for action in listed.json()] == [proposal["id"]]


def test_and_can_still_be_approved(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    from resolver.resolution import link_principal_identities

    proposal = propose(client, world, model)["proposal"]
    with world.cursor() as cur:
        cur.execute("SELECT requested_by FROM actions WHERE id = %s", (proposal["id"],))
        recorded = (cur.fetchone() or (None,))[0]
    link_principal_identities(world)
    with world.cursor() as cur:
        cur.execute(
            "UPDATE users SET principal_id = ("
            "  SELECT id FROM principals WHERE kind = 'user' "
            "  AND lower(btrim(email)) = %s AND id <> %s LIMIT 1"
            ") WHERE email = %s",
            (ALICE_EMAIL, recorded, ALICE_EMAIL),
        )
    world.commit()

    response = client.post(
        f"/api/v1/actions/{proposal['id']}/approve", headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 200
    assert response.json()["status"] == "approved"


# ---------------------------------------------------------------------------
# Notes (P2-MEM-1), over HTTP.
# ---------------------------------------------------------------------------


def test_writing_a_note_and_reading_it_back(client: TestClient, world: Connection) -> None:
    auth_headers = headers(client, ALICE_EMAIL)

    created = client.post(
        "/api/v1/notes", json={"content": "Priya owns the Acme renewal."}, headers=auth_headers
    )

    assert created.status_code == 201
    assert created.json()["scope_type"] == "personal"
    assert created.json()["is_mine"] is True
    listed = client.get("/api/v1/notes", headers=auth_headers).json()
    assert [note["content"] for note in listed] == ["Priya owns the Acme renewal."]


def test_a_note_changes_what_a_question_retrieves(
    client: TestClient, world: Connection, model: ScriptedModel
) -> None:
    """The point of the feature, end to end: written through the API, retrieved
    by the agent, in the prompt."""
    auth_headers = headers(client, ALICE_EMAIL)
    client.post(
        "/api/v1/notes",
        json={"content": "Renewal escalations go to Priya, not to the deal desk."},
        headers=auth_headers,
    )

    client.post(
        "/api/v1/queries",
        json={"question": "Who handles renewal escalations?", "k": 40},
        headers=auth_headers,
    )

    assert "Renewal escalations go to Priya" in model.last_prompt


def test_a_personal_note_is_not_in_someone_elses_list(
    client: TestClient, world: Connection
) -> None:
    client.post(
        "/api/v1/notes",
        json={"content": "only alice should see this"},
        headers=headers(client, ALICE_EMAIL),
    )

    listed = client.get("/api/v1/notes", headers=headers(client, CAROL_EMAIL)).json()

    assert listed == []


def test_the_scopes_endpoint_offers_somewhere_to_write(
    client: TestClient, world: Connection
) -> None:
    scopes = client.get("/api/v1/scopes", headers=headers(client, ALICE_EMAIL)).json()

    assert any(scope["scope_type"] == "personal" for scope in scopes)
    assert any(scope["scope_type"] == "org" for scope in scopes)


def test_editing_someone_elses_note_is_forbidden(client: TestClient, world: Connection) -> None:
    scopes = client.get("/api/v1/scopes", headers=headers(client, ALICE_EMAIL)).json()
    org = next(scope for scope in scopes if scope["scope_type"] == "org")
    note = client.post(
        "/api/v1/notes",
        json={"content": "alice wrote this", "scope_id": org["id"]},
        headers=headers(client, ALICE_EMAIL),
    ).json()

    response = client.patch(
        f"/api/v1/notes/{note['id']}",
        json={"content": "carol rewrote it"},
        headers=headers(client, CAROL_EMAIL),
    )

    assert response.status_code == 403


def test_superseding_then_restoring_over_http(client: TestClient, world: Connection) -> None:
    auth_headers = headers(client, ALICE_EMAIL)
    note = client.post("/api/v1/notes", json={"content": "temporary"}, headers=auth_headers).json()

    retired = client.post(f"/api/v1/notes/{note['id']}/supersede", headers=auth_headers)
    assert retired.json()["superseded_at"] is not None

    restored = client.post(f"/api/v1/notes/{note['id']}/restore", headers=auth_headers)
    assert restored.json()["superseded_at"] is None


def test_pinning_over_http(client: TestClient, world: Connection) -> None:
    auth_headers = headers(client, ALICE_EMAIL)
    note = client.post("/api/v1/notes", json={"content": "pin me"}, headers=auth_headers).json()

    pinned = client.post(
        f"/api/v1/notes/{note['id']}/pin", json={"pinned": True}, headers=auth_headers
    )

    assert pinned.json()["pinned"] is True


def test_erasing_a_note_over_http(client: TestClient, world: Connection) -> None:
    auth_headers = headers(client, ALICE_EMAIL)
    note = client.post("/api/v1/notes", json={"content": "gone soon"}, headers=auth_headers).json()

    assert client.delete(f"/api/v1/notes/{note['id']}", headers=auth_headers).status_code == 204
    assert client.get("/api/v1/notes", headers=auth_headers).json() == []


def test_an_unknown_note_is_not_found(client: TestClient, world: Connection) -> None:
    response = client.post(
        f"/api/v1/notes/{uuid4()}/supersede", headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 404


def test_an_empty_note_is_refused_by_the_schema(client: TestClient, world: Connection) -> None:
    response = client.post(
        "/api/v1/notes", json={"content": ""}, headers=headers(client, ALICE_EMAIL)
    )

    assert response.status_code == 422


def test_the_timeline_endpoint_orders_by_when_things_happened(
    client: TestClient, world: Connection
) -> None:
    """P2-MEM-3 over HTTP: pick a ticket, get a coherent cited chain."""
    with world.cursor() as cur:
        cur.execute("SELECT id FROM entities WHERE title = 'Acme renewal blocked on legal review'")
        ticket = (cur.fetchone() or (None,))[0]

    response = client.get(f"/api/v1/timeline/{ticket}?hops=3", headers=headers(client, ALICE_EMAIL))

    assert response.status_code == 200
    body = response.json()
    events = [m for m in body["moments"] if not m["is_context"]]
    times = [m["occurred_at"] for m in events]
    assert times == sorted(times)
    assert body["starts_at"] < body["ends_at"]
    assert any(m["url"] for m in events)


def test_a_timeline_is_filtered_to_the_viewer(client: TestClient, world: Connection) -> None:
    with world.cursor() as cur:
        cur.execute("SELECT id FROM entities WHERE title = 'Acme renewal blocked on legal review'")
        ticket = (cur.fetchone() or (None,))[0]

    alice = client.get(f"/api/v1/timeline/{ticket}?hops=3", headers=headers(client, ALICE_EMAIL))
    carol = client.get(f"/api/v1/timeline/{ticket}?hops=3", headers=headers(client, CAROL_EMAIL))

    titles = " ".join(m["title"] or "" for m in carol.json()["moments"])
    assert PRIVATE_TEXT not in titles
    assert len(carol.json()["moments"]) < len(alice.json()["moments"])


def test_a_timeline_for_something_invisible_is_empty(client: TestClient, world: Connection) -> None:
    """Not a 404 and not a partial chain: naming an entity you lack access to
    must not confirm that it exists."""
    with world.cursor() as cur:
        cur.execute("SELECT id FROM entities WHERE title = '#deals-acme'")
        private_channel = (cur.fetchone() or (None,))[0]

    response = client.get(
        f"/api/v1/timeline/{private_channel}", headers=headers(client, CAROL_EMAIL)
    )

    assert response.status_code == 200
    assert response.json()["moments"] == []
