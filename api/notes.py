"""Notes people write, and the memory they change.

ARCHITECTURE calls this "transparent, editable memory": you can see what the
system believes and correct it. The correcting is the part that matters — a
note that did not change an answer would be a place to type into.

So writing a note projects it into the retrieval path (migration 015). It is
then found by `visible_chunks()` like anything else, cited like anything else,
and shown in the trace like anything else. There is no notes-shaped exception
to rule 1, and there is no second read path to review.

Three rules about who may do what, and each is about a different thing:

**Reading is decided by scope.** Personal is yours alone, team is the group's,
org is everyone's. That rule already exists in `_visible_scope_ids` and is the
same one the permission filter uses, so a note cannot be visible in a way
content is not.

**Editing is decided by authorship.** A team scope is shared, which makes it
somewhere several people read — not somewhere anyone may rewrite what a
colleague wrote. You edit your own.

**Deleting is a supersede, not an erase.** The row stays and stops being
retrievable. "What did this used to say" is the question an editable memory
exists to answer, and a delete that destroyed the answer would take the
transparency out of transparent memory.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from core.db import Connection
from resolver.embeddings import EmbeddingProvider, to_pgvector

LOG = logging.getLogger("hippo.api.notes")

MAX_LENGTH = 8_000


class NoteError(Exception):
    """The note cannot be written as asked."""


class NoteNotFoundError(NoteError):
    """No such note, or not one this person may see.

    One exception for both, so the API is not a way to discover that a note
    exists in a scope you cannot read.
    """


class NotYoursError(NoteError):
    """Someone else wrote it."""


class Scope(BaseModel):
    """A place a note can live."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    scope_type: str
    name: str


class Note(BaseModel):
    """A note, as its reader sees it."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    scope_id: UUID
    scope_type: str
    scope_name: str
    author: UUID
    is_mine: bool
    about_entity: UUID | None
    content: str
    pinned: bool
    superseded_at: Any | None
    created_at: Any
    updated_at: Any

    @property
    def live(self) -> bool:
        return self.superseded_at is None


class NoteDraft(BaseModel):
    """What a person is asking to record."""

    model_config = ConfigDict(frozen=True)

    content: str = Field(min_length=1, max_length=MAX_LENGTH)
    scope_id: UUID | None = None
    about_entity: UUID | None = None
    pinned: bool = False


def scopes_for(conn: Connection, principal_id: UUID) -> list[Scope]:
    """Where this person may write.

    The same set they can read from, because a note in a scope you cannot see
    is a note you cannot read back.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, scope_type, name FROM my_scopes(%s)", (principal_id,))
        found = [
            Scope(id=UUID(str(row[0])), scope_type=str(row[1]), name=str(row[2]))
            for row in cur.fetchall()
        ]

    if not any(scope.scope_type == "personal" for scope in found):
        # Created on demand: most people never write a note, and a scope per
        # principal would be a row per account in a table the permission filter
        # joins on every single query.
        with conn.cursor() as cur:
            cur.execute("SELECT ensure_personal_scope(%s)", (principal_id,))
            row = cur.fetchone()
        if row is not None:
            found.insert(0, Scope(id=UUID(str(row[0])), scope_type="personal", name="Personal"))
    return found


def list_notes(conn: Connection, principal_id: UUID, limit: int = 100) -> list[Note]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM my_notes(%s, %s)", (principal_id, limit))
        columns = [description.name for description in cur.description or []]
        return [Note.model_validate(dict(zip(columns, row, strict=True))) for row in cur.fetchall()]


def get_note(conn: Connection, principal_id: UUID, note_id: UUID) -> Note:
    for note in list_notes(conn, principal_id, limit=500):
        if note.id == note_id:
            return note
    raise NoteNotFoundError(str(note_id))


def write(
    conn: Connection,
    principal_id: UUID,
    draft: NoteDraft,
    embedder: EmbeddingProvider | None = None,
) -> Note:
    """Record a note, and make it part of what the agent can find."""
    scope_id = draft.scope_id
    if scope_id is None:
        scope_id = next(
            scope.id for scope in scopes_for(conn, principal_id) if scope.scope_type == "personal"
        )
    _require_writable(conn, principal_id, scope_id)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO memory_notes (scope_id, author, about_entity, content, pinned) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (scope_id, principal_id, draft.about_entity, draft.content, draft.pinned),
        )
        row = cur.fetchone()
    assert row is not None
    note_id = UUID(str(row[0]))

    _project(conn, note_id, draft.content, embedder)
    LOG.info("note written", extra={"note": str(note_id), "scope": str(scope_id)})
    return get_note(conn, principal_id, note_id)


def edit(
    conn: Connection,
    principal_id: UUID,
    note_id: UUID,
    content: str,
    embedder: EmbeddingProvider | None = None,
) -> Note:
    """Change what a note says. Yours only."""
    existing = get_note(conn, principal_id, note_id)
    if not existing.is_mine:
        raise NotYoursError("a shared scope is somewhere to read, not somewhere to rewrite")
    if not content.strip():
        raise NoteError("a note needs content; supersede it instead of emptying it")

    with conn.cursor() as cur:
        cur.execute("UPDATE memory_notes SET content = %s WHERE id = %s", (content, note_id))

    _project(conn, note_id, content, embedder)
    return get_note(conn, principal_id, note_id)


def set_pinned(conn: Connection, principal_id: UUID, note_id: UUID, pinned: bool) -> Note:
    """Pinning is about ordering, not about permission — anyone who can read a
    team note can pin it for the team, which is what makes a shared scope
    useful rather than one person's board."""
    get_note(conn, principal_id, note_id)
    with conn.cursor() as cur:
        cur.execute("UPDATE memory_notes SET pinned = %s WHERE id = %s", (pinned, note_id))
    return get_note(conn, principal_id, note_id)


def supersede(conn: Connection, principal_id: UUID, note_id: UUID) -> Note:
    """Stop a note informing answers, without destroying what it said."""
    existing = get_note(conn, principal_id, note_id)
    if not existing.is_mine:
        raise NotYoursError("only the author can retire a note")

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE memory_notes SET superseded_at = now() WHERE id = %s AND superseded_at IS NULL",
            (note_id,),
        )
        cur.execute("SELECT project_note(%s, NULL)", (note_id,))

    LOG.info("note superseded", extra={"note": str(note_id)})
    return get_note(conn, principal_id, note_id)


def restore(
    conn: Connection, principal_id: UUID, note_id: UUID, embedder: EmbeddingProvider | None = None
) -> Note:
    """Undo a supersede. The whole reason it is not a delete."""
    existing = get_note(conn, principal_id, note_id)
    if not existing.is_mine:
        raise NotYoursError("only the author can restore a note")

    with conn.cursor() as cur:
        cur.execute("UPDATE memory_notes SET superseded_at = NULL WHERE id = %s", (note_id,))
    _project(conn, note_id, existing.content, embedder)
    return get_note(conn, principal_id, note_id)


def erase(conn: Connection, principal_id: UUID, note_id: UUID) -> None:
    """Actually destroy it. Separate from supersede on purpose.

    Superseding is the everyday action and is reversible. This one is not, so
    it is a different verb rather than a flag on the same one — a person who
    meant "stop using this" should not be able to reach "it never existed" by
    clicking the same button twice.
    """
    existing = get_note(conn, principal_id, note_id)
    if not existing.is_mine:
        raise NotYoursError("only the author can erase a note")

    with conn.cursor() as cur:
        cur.execute("SELECT unproject_note(%s)", (note_id,))
        cur.execute("DELETE FROM memory_notes WHERE id = %s", (note_id,))
    LOG.info("note erased", extra={"note": str(note_id)})


def _project(
    conn: Connection, note_id: UUID, content: str, embedder: EmbeddingProvider | None
) -> None:
    """Make the note retrievable.

    The embedding is computed here rather than in the projection function,
    because SQL cannot call a model. Without one the note is still found by
    keyword search, which is the honest degraded mode rather than a failure.
    """
    embedding: str | None = None
    if embedder is not None:
        (vector,) = embedder.embed([content])
        embedding = to_pgvector(vector)

    with conn.cursor() as cur:
        cur.execute("SELECT project_note(%s, %s::vector)", (note_id, embedding))


def _require_writable(conn: Connection, principal_id: UUID, scope_id: UUID) -> None:
    if scope_id not in {scope.id for scope in scopes_for(conn, principal_id)}:
        raise NoteNotFoundError("no such scope")
