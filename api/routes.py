"""The v1 REST surface.

ARCHITECTURE §2 gives this layer auth, sessions and approval buttons, and no
business logic. That is worth taking literally: every route is a thin
translation between HTTP and a function that already exists and is already
tested elsewhere. When a route grows a decision, the decision belongs one layer
down.

**The agent path runs with less privilege than the API.** A query is served
inside `SET LOCAL ROLE hippo_agent`, so the code that assembles a prompt holds
EXECUTE on visible_chunks() and INSERT on actions and traces, and SELECT on
nothing. Without it, CLAUDE.md rule 1 would be true of the agent module and
false of the process actually serving users: hippo_api can read entities and
connectors, and a prompt built under that role would have a second path to
content. `SET LOCAL` rather than `SET`, so the reduction cannot outlive its
transaction or leak into the next request on a pooled connection.

**Errors say as little as possible about what exists.** A wrong password, an
unknown email, someone else's trace and a trace that never existed all produce
the same response. A different one is a way to enumerate.
"""

# No `from __future__ import annotations` here on purpose: FastAPI resolves
# these annotations at runtime, and the dependency aliases below are
# function-local, so stringised annotations become forward references it cannot
# look up. Everything used here is valid at runtime on the supported Python.

import logging
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from datetime import datetime
from typing import Annotated, Any, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from agent.links import ConnectorDirectory, load_directory
from agent.loop import Agent
from agent.timeline import build as build_timeline
from agent.trace import list_traces, load_trace
from api import approvals, auth, notes
from core.db import Connection
from resolver.embeddings import EmbeddingProvider

LOG = logging.getLogger("hippo.api.routes")

AGENT_ROLE = "hippo_agent"


class ConnectionSource(Protocol):
    """Anything that hands out pooled connections.

    A Protocol rather than the concrete pool so the app can pass an indirection
    that reads the pool at request time: the pool is opened by the lifespan,
    after the router is built.
    """

    def connection(self, timeout: float | None = None) -> AbstractContextManager[Connection]: ...


@contextmanager
def as_agent(conn: Connection) -> Iterator[Connection]:
    """Run inside the agent's privileges for the rest of this transaction."""
    with conn.cursor() as cur:
        cur.execute(f'SET LOCAL ROLE "{AGENT_ROLE}"')
    yield conn


# ---------------------------------------------------------------------------
# Wire models. Separate from the domain models on purpose: what a route
# returns is a compatibility promise, and coupling it to an internal shape
# turns every refactor into a breaking change.
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class UserResponse(BaseModel):
    id: UUID
    email: str
    display_name: str | None
    is_admin: bool
    # Reported rather than hidden: a user with no principal is not broken, they
    # are someone sync has not met yet, and the UI should say so instead of
    # rendering an empty answer as if it were an answer.
    has_access: bool


class SessionResponse(BaseModel):
    token: str
    expires_at: datetime
    user: UserResponse


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    k: int = Field(default=12, ge=1, le=100)


class CitationResponse(BaseModel):
    marker: int
    entity_id: UUID
    entity_type: str
    title: str | None
    url: str | None


class ProposalResponse(BaseModel):
    id: UUID
    action_type: str
    summary: str
    risk_class: str
    status: str


class QueryResponse(BaseModel):
    answer: str
    citations: list[CitationResponse]
    trace_id: UUID | None
    refused: bool
    proposal: ProposalResponse | None
    model: str
    input_tokens: int
    output_tokens: int


class MomentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    entity_id: UUID
    entity_type: str
    title: str | None
    occurred_at: datetime | None
    hops: int
    via: str | None
    relation: str
    url: str | None
    is_context: bool


class TimelineResponse(BaseModel):
    subject: UUID
    moments: list[MomentResponse]
    starts_at: datetime | None
    ends_at: datetime | None


class NoteResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope_id: UUID
    scope_type: str
    scope_name: str
    author: UUID
    is_mine: bool
    about_entity: UUID | None
    content: str
    pinned: bool
    superseded_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ScopeResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    scope_type: str
    name: str


class NoteRequest(BaseModel):
    content: str = Field(min_length=1, max_length=notes.MAX_LENGTH)
    scope_id: UUID | None = None
    about_entity: UUID | None = None
    pinned: bool = False


class NoteEdit(BaseModel):
    content: str = Field(min_length=1, max_length=notes.MAX_LENGTH)


class PinRequest(BaseModel):
    pinned: bool


class ActionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    action_type: str
    status: str
    risk_class: str
    payload: dict[str, Any]
    target_entity: UUID | None
    summary: str | None
    connector_kind: str
    requested_by: UUID
    approved_by: UUID | None
    approved_by_policy: str | None
    declined_by: UUID | None
    rolled_back_by: UUID | None
    error: str | None
    created_at: datetime


def build_router(
    db: ConnectionSource,
    agent: Callable[[Connection], Agent],
    embedder: EmbeddingProvider | None = None,
) -> APIRouter:
    """Assemble the v1 routes.

    The agent arrives as a callable rather than an instance so it can be built
    on first use — its connector directory needs a database, and building the
    app must not require one.
    """
    router = APIRouter(prefix="/api/v1")
    cached_directory: dict[str, ConnectorDirectory] = {}

    def _directory(conn: Connection) -> ConnectorDirectory:
        """Loaded once. It changes when an operator adds a connector, not per
        request, and it carries no content — only how to build a link."""
        if "directory" not in cached_directory:
            cached_directory["directory"] = load_directory(conn)
        return cached_directory["directory"]

    def connection() -> Iterator[Connection]:
        with db.connection() as conn:
            yield conn

    # Type aliases, so PascalCase is correct here despite the scope.
    Conn = Annotated[Connection, Depends(connection)]  # noqa: N806

    def bearer(authorization: Annotated[str | None, Header()] = None) -> str:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "not authenticated")
        return authorization.split(" ", 1)[1].strip()

    def current_user(conn: Conn, token: Annotated[str, Depends(bearer)]) -> auth.User:
        try:
            return auth.authenticate(conn, token)
        except auth.AuthError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc

    CurrentUser = Annotated[auth.User, Depends(current_user)]  # noqa: N806

    def asking_principal(user: CurrentUser) -> UUID:
        """The principal a request runs as.

        403 rather than an empty answer when there is none: "you have access to
        nothing" and "nothing matched" are different facts, and the second must
        not be used to report the first.
        """
        if user.principal_id is None:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "no source-system account is linked to this login yet",
            )
        return user.principal_id

    Principal = Annotated[UUID, Depends(asking_principal)]  # noqa: N806

    # -- sessions ----------------------------------------------------------

    @router.post("/sessions", tags=["auth"], status_code=status.HTTP_201_CREATED, summary="Log in")
    def create_session(conn: Conn, body: LoginRequest) -> SessionResponse:
        try:
            session = auth.login(conn, body.email, body.password)
        except auth.AuthError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        return SessionResponse(
            token=session.token,
            expires_at=session.expires_at,
            user=_user(session.user),
        )

    @router.delete(
        "/sessions/current",
        tags=["auth"],
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Log out",
    )
    def delete_session(conn: Conn, token: Annotated[str, Depends(bearer)]) -> None:
        auth.logout(conn, token)

    @router.delete(
        "/sessions",
        tags=["auth"],
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Log out of every session",
    )
    def delete_all_sessions(conn: Conn, user: CurrentUser) -> None:
        auth.logout_everywhere(conn, user.id)

    @router.get("/me", tags=["auth"], summary="Who am I")
    def me(user: CurrentUser) -> UserResponse:
        return _user(user)

    # -- queries -----------------------------------------------------------

    @router.post(
        "/queries",
        tags=["agent"],
        summary="Ask a question",
        description=(
            "Answers from what the asking person can see, and nothing else. "
            "A request to change something produces a pending action instead "
            "of an answer; nothing is executed here."
        ),
    )
    def ask(conn: Conn, principal_id: Principal, body: QueryRequest) -> QueryResponse:
        # Built before the role drops. The connector directory comes from the
        # connectors table, which hippo_agent cannot read and does not need to:
        # agent/links.py loads it once, from a component that can, and hands it
        # over. Building it inside the reduction would be asking the agent to
        # read a table the whole design says it should not reach.
        resolved = agent(conn)
        with as_agent(conn):
            answer = resolved.answer(conn, principal_id, body.question, k=body.k)
        return QueryResponse(
            answer=answer.text,
            citations=[
                CitationResponse(
                    marker=citation.marker,
                    entity_id=citation.entity_id,
                    entity_type=citation.entity_type,
                    title=citation.title,
                    url=citation.url,
                )
                for citation in answer.citations
            ],
            trace_id=answer.trace_id,
            refused=answer.refused,
            proposal=(
                None
                if answer.proposal is None
                else ProposalResponse(
                    id=answer.proposal.id,
                    action_type=answer.proposal.action_type,
                    summary=answer.proposal.summary,
                    risk_class=answer.proposal.risk_class,
                    status=answer.proposal.status,
                )
            ),
            model=answer.model,
            input_tokens=answer.usage.input_tokens,
            output_tokens=answer.usage.output_tokens,
        )

    # -- actions -----------------------------------------------------------

    @router.get("/actions", tags=["actions"], summary="Actions you asked for")
    def get_actions(
        conn: Conn,
        principal_id: Principal,
        action_status: str | None = None,
        limit: int = 50,
    ) -> list[ActionResponse]:
        return [
            ActionResponse.model_validate(action)
            for action in approvals.list_actions(
                conn, principal_id, status=action_status, limit=limit
            )
        ]

    @router.get("/actions/{action_id}", tags=["actions"], summary="One action")
    def get_one_action(conn: Conn, principal_id: Principal, action_id: UUID) -> ActionResponse:
        return _translate(lambda: approvals.get_action(conn, principal_id, action_id))

    @router.post(
        "/actions/{action_id}/approve",
        tags=["actions"],
        summary="Approve a pending action",
        description=(
            "Records the approval. Nothing is executed here: the sync worker "
            "captures the inverse and performs the write, because it is the "
            "only component holding source-system credentials."
        ),
    )
    def approve_action(conn: Conn, principal_id: Principal, action_id: UUID) -> ActionResponse:
        return _translate(lambda: approvals.approve(conn, principal_id, action_id))

    @router.post(
        "/actions/{action_id}/decline",
        tags=["actions"],
        summary="Decline a pending action",
    )
    def decline_action(conn: Conn, principal_id: Principal, action_id: UUID) -> ActionResponse:
        return _translate(lambda: approvals.decline(conn, principal_id, action_id))

    @router.post(
        "/actions/{action_id}/rollback",
        tags=["actions"],
        summary="Undo an executed action",
        description=(
            "Requests the undo. The sync worker performs it using the inverse "
            "captured before the action ran, and the status moves when it has "
            "actually happened — a status that changed here would send someone "
            "looking for a change that is still live in the source system."
        ),
    )
    def rollback_action(conn: Conn, principal_id: Principal, action_id: UUID) -> ActionResponse:
        return _translate(lambda: approvals.request_rollback(conn, principal_id, action_id))

    # -- notes -------------------------------------------------------------

    def _note(load: Callable[[], notes.Note]) -> NoteResponse:
        try:
            return NoteResponse.model_validate(load())
        except notes.NoteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such note") from exc
        except notes.NotYoursError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except notes.NoteError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    @router.get(
        "/scopes",
        tags=["memory"],
        summary="Where you can write a note",
        description=(
            "Personal is yours alone, team is the group's, org is everyone's. The "
            "same scopes decide who can read a note back, so this is both the "
            "write list and the read list."
        ),
    )
    def get_scopes(conn: Conn, principal_id: Principal) -> list[ScopeResponse]:
        return [
            ScopeResponse.model_validate(scope) for scope in notes.scopes_for(conn, principal_id)
        ]

    @router.get(
        "/notes",
        tags=["memory"],
        summary="Notes you can see",
        description=(
            "What the system has been told, by you and by anyone sharing a scope "
            "with you. A note is retrievable memory: it is found and cited like "
            "synced content, so this list is also a list of what can change an "
            "answer."
        ),
    )
    def get_notes(conn: Conn, principal_id: Principal, limit: int = 100) -> list[NoteResponse]:
        return [
            NoteResponse.model_validate(note)
            for note in notes.list_notes(conn, principal_id, limit)
        ]

    @router.post(
        "/notes",
        tags=["memory"],
        status_code=status.HTTP_201_CREATED,
        summary="Write a note",
    )
    def post_note(conn: Conn, principal_id: Principal, body: NoteRequest) -> NoteResponse:
        draft = notes.NoteDraft(
            content=body.content,
            scope_id=body.scope_id,
            about_entity=body.about_entity,
            pinned=body.pinned,
        )
        return _note(lambda: notes.write(conn, principal_id, draft, embedder))

    @router.patch("/notes/{note_id}", tags=["memory"], summary="Change what a note says")
    def patch_note(
        conn: Conn, principal_id: Principal, note_id: UUID, body: NoteEdit
    ) -> NoteResponse:
        return _note(lambda: notes.edit(conn, principal_id, note_id, body.content, embedder))

    @router.post("/notes/{note_id}/pin", tags=["memory"], summary="Pin or unpin")
    def pin_note(
        conn: Conn, principal_id: Principal, note_id: UUID, body: PinRequest
    ) -> NoteResponse:
        return _note(lambda: notes.set_pinned(conn, principal_id, note_id, body.pinned))

    @router.post(
        "/notes/{note_id}/supersede",
        tags=["memory"],
        summary="Retire a note",
        description=(
            "Stops the note informing answers and keeps what it said. 'What did "
            "this used to say' is the question an editable memory exists to "
            "answer, so the everyday action is reversible."
        ),
    )
    def supersede_note(conn: Conn, principal_id: Principal, note_id: UUID) -> NoteResponse:
        return _note(lambda: notes.supersede(conn, principal_id, note_id))

    @router.post("/notes/{note_id}/restore", tags=["memory"], summary="Un-retire a note")
    def restore_note(conn: Conn, principal_id: Principal, note_id: UUID) -> NoteResponse:
        return _note(lambda: notes.restore(conn, principal_id, note_id, embedder))

    @router.delete(
        "/notes/{note_id}",
        tags=["memory"],
        status_code=status.HTTP_204_NO_CONTENT,
        summary="Erase a note permanently",
        description=(
            "Irreversible, and a separate verb from supersede on purpose: "
            "someone who meant 'stop using this' should not reach 'it never "
            "existed' by clicking the same button twice."
        ),
    )
    def delete_note(conn: Conn, principal_id: Principal, note_id: UUID) -> None:
        try:
            notes.erase(conn, principal_id, note_id)
        except notes.NoteNotFoundError as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such note") from exc
        except notes.NotYoursError as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc

    # -- timeline ------------------------------------------------------------

    @router.get(
        "/timeline/{entity_id}",
        tags=["memory"],
        summary="What happened around one thing, in order",
        description=(
            "Walks the graph out from an entity and orders what it reaches by "
            "when the source says it happened — not by when it was synced. "
            "Filtered by the same ACL closure retrieval uses, so an entry "
            "appears here exactly when it could appear in an answer. Entities "
            "the source gave no time for are returned as context rather than "
            "as events."
        ),
    )
    def get_timeline(
        conn: Conn, principal_id: Principal, entity_id: UUID, hops: int = 2, limit: int = 100
    ) -> TimelineResponse:
        directory = _directory(conn)
        chain = build_timeline(
            conn, principal_id, entity_id, hops=hops, limit=limit, directory=directory
        )
        span = chain.span
        return TimelineResponse(
            subject=chain.subject,
            moments=[MomentResponse.model_validate(moment) for moment in chain.moments],
            starts_at=span[0] if span else None,
            ends_at=span[1] if span else None,
        )

    # -- traces ------------------------------------------------------------

    @router.get("/traces", tags=["trace"], summary="Your recent queries")
    def get_traces(conn: Conn, principal_id: Principal, limit: int = 50) -> list[dict[str, Any]]:
        return list_traces(conn, principal_id, limit)

    @router.get(
        "/traces/{trace_id}",
        tags=["trace"],
        summary="Every step of one query",
        description=(
            "The plan, the retrieved chunks in rank order with the mode that "
            "found each and whether the answer cited it, the system prompt, and "
            "the token cost. Chunk content is not stored; the recorded hashes "
            "let a reconstruction be verified."
        ),
    )
    def get_one_trace(conn: Conn, principal_id: Principal, trace_id: UUID) -> dict[str, Any]:
        trace = load_trace(conn, principal_id, trace_id)
        if trace is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such trace")
        return trace

    return router


def _user(user: auth.User) -> UserResponse:
    return UserResponse(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        has_access=user.can_see_anything,
    )


def _translate(load: Callable[[], approvals.Action]) -> ActionResponse:
    """The approval layer's two failures, as two status codes."""
    try:
        return ActionResponse.model_validate(load())
    except approvals.ActionNotFoundError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such action") from exc
    except approvals.ActionConflictError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
