"""P2-MEM-1: notes people write, and the answers they change.

The done-condition is view, edit, pin and delete on personal and team scopes.
The test that matters most is none of those: it is that a note written by a
person is *retrieved* by the agent and cited in an answer. A notes feature that
did not do that would be a text box, and "transparent, editable memory" would
be a phrase rather than a property.

The second most important is that a note obeys the same permission rule as
content. A note is a new way to put words in front of the model, so it is a new
way to leak — unless its visibility is decided by the mechanism that already
decides everything else's.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest

from agent.links import load_directory
from agent.loop import Agent
from agent.providers.base import Completion, CompletionRequest, Usage
from api import notes
from core.db import Connection
from resolver.embeddings import HashingEmbeddings
from sync.connectors.slack import FixtureTransport as SlackFixtures
from sync.connectors.slack import SlackConnector
from sync.runtime import SyncRuntime, project_acl_grants
from tests.pipeline import principal, resolve_and_enrich

pytestmark = pytest.mark.requires_db

FIXTURES = Path(__file__).resolve().parent / "fixtures"
NOTE = "The Acme renewal owner is Priya, not Alice. Route pricing questions to her."


class Recorder:
    name = "recorder"
    model = "recorder-1"

    def __init__(self) -> None:
        self.requests: list[CompletionRequest] = []

    @property
    def last_prompt(self) -> str:
        return self.requests[-1].messages[0].content

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        markers = " ".join(
            f"[{i}]" for i in range(1, request.messages[0].content.count("<source ") + 1)
        )
        return Completion(
            text=f"Here you go. {markers}",
            model=self.model,
            provider=self.name,
            stop_reason="end_turn",
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    def count_tokens(self, request: CompletionRequest) -> int:
        return 0


@pytest.fixture
def workspace(migrated: Connection) -> UUID:
    slack_id = uuid4()
    with migrated.cursor() as cur:
        cur.execute(
            "INSERT INTO connectors (id, kind, display_name) VALUES (%s, 'slack', 'Slack')",
            (slack_id,),
        )
    SyncRuntime(SlackConnector(SlackFixtures(FIXTURES / "slack")), slack_id).sync_all(migrated)
    resolve_and_enrich(migrated)
    project_acl_grants(migrated, slack_id)
    return slack_id


@pytest.fixture
def alice(migrated: Connection, workspace: UUID) -> UUID:
    return principal(migrated, workspace, "U-ALICE")


@pytest.fixture
def carol(migrated: Connection, workspace: UUID) -> UUID:
    return principal(migrated, workspace, "U-CAROL")


def ask(conn: Connection, who: UUID, question: str) -> tuple[Recorder, object]:
    model = Recorder()
    agent = Agent(model, embedder=HashingEmbeddings(), directory=load_directory(conn))
    return model, agent.answer(conn, who, question, k=40)


# ---------------------------------------------------------------------------
# The point of the feature.
# ---------------------------------------------------------------------------


def test_a_note_reaches_the_agent(migrated: Connection, alice: UUID) -> None:
    """The whole reason a note is projected rather than stored beside
    retrieval. A note that never reached a prompt would be a text box."""
    notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    model, _ = ask(migrated, alice, "Who owns the Acme renewal?")

    assert NOTE in model.last_prompt


def test_a_note_can_be_cited(migrated: Connection, alice: UUID) -> None:
    """It arrives as a source with a marker, like anything else."""
    notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    _, answer = ask(migrated, alice, "Who owns the Acme renewal?")

    cited = {hit.content for hit in answer.hits}  # type: ignore[attr-defined]
    assert NOTE in cited


def test_a_note_is_findable_without_an_embedder(migrated: Connection, alice: UUID) -> None:
    """No embedder is a degraded mode, not a failure: keyword search still
    reaches the note."""
    notes.write(migrated, alice, notes.NoteDraft(content=NOTE), None)

    model, _ = ask(migrated, alice, "Who owns the Acme renewal?")

    assert NOTE in model.last_prompt


def test_editing_a_note_changes_what_the_agent_sees(migrated: Connection, alice: UUID) -> None:
    """The correcting half of "transparent, editable memory". A note you can
    edit but whose edit does not reach the model is not a correction."""
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())
    corrected = "Correction: Priya left. Alice owns the Acme renewal again."

    notes.edit(migrated, alice, note.id, corrected, HashingEmbeddings())

    model, _ = ask(migrated, alice, "Who owns the Acme renewal?")
    assert corrected in model.last_prompt
    assert NOTE not in model.last_prompt


def test_superseding_stops_it_informing_answers(migrated: Connection, alice: UUID) -> None:
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    notes.supersede(migrated, alice, note.id)

    model, _ = ask(migrated, alice, "Who owns the Acme renewal?")
    assert NOTE not in model.last_prompt


def test_superseding_keeps_what_it_said(migrated: Connection, alice: UUID) -> None:
    """'What did this used to say' is the question an editable memory exists to
    answer."""
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    notes.supersede(migrated, alice, note.id)

    retired = notes.get_note(migrated, alice, note.id)
    assert retired.content == NOTE
    assert retired.live is False


def test_a_supersede_can_be_undone(migrated: Connection, alice: UUID) -> None:
    """The whole reason the everyday action is not a delete."""
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())
    notes.supersede(migrated, alice, note.id)

    notes.restore(migrated, alice, note.id, HashingEmbeddings())

    model, _ = ask(migrated, alice, "Who owns the Acme renewal?")
    assert NOTE in model.last_prompt


def test_erasing_is_a_different_verb(migrated: Connection, alice: UUID) -> None:
    """Someone who meant "stop using this" should not reach "it never existed"
    by clicking the same button twice."""
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    notes.erase(migrated, alice, note.id)

    assert notes.list_notes(migrated, alice) == []
    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM chunks WHERE content = %s", (NOTE,))
        assert cur.fetchone() == (0,)


# ---------------------------------------------------------------------------
# Permission: a note is a new way to put words in front of the model.
# ---------------------------------------------------------------------------


def test_a_personal_note_is_invisible_to_everyone_else(
    migrated: Connection, alice: UUID, carol: UUID
) -> None:
    notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    model, _ = ask(migrated, carol, "Who owns the Acme renewal?")

    assert NOTE not in model.last_prompt
    assert notes.list_notes(migrated, carol) == []


def test_a_personal_note_is_not_reachable_by_quoting_it(
    migrated: Connection, alice: UUID, carol: UUID
) -> None:
    """The red team's strongest probe, applied to notes: someone who has seen
    the text elsewhere and is checking whether Hippo will confirm it.

    Asserted on the sources rather than the whole prompt, because Carol typed
    the text herself — it is in her question by construction. What must not
    happen is it coming *back*.
    """
    notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    model, _ = ask(migrated, carol, NOTE)

    sources = model.last_prompt.split("Sources:", 1)[1]
    assert NOTE not in sources


def test_an_org_note_reaches_everybody(migrated: Connection, alice: UUID, carol: UUID) -> None:
    org = next(s for s in notes.scopes_for(migrated, alice) if s.scope_type == "org")
    shared = "Company-wide: all renewals now need a security review."

    notes.write(
        migrated, alice, notes.NoteDraft(content=shared, scope_id=org.id), HashingEmbeddings()
    )

    model, _ = ask(migrated, carol, "What do renewals need?")
    assert shared in model.last_prompt


def test_you_cannot_write_into_a_scope_you_cannot_see(
    migrated: Connection, alice: UUID, carol: UUID
) -> None:
    """A note in a scope you cannot read is a note you cannot read back, so the
    write side agrees with the read side."""
    notes.scopes_for(migrated, alice)
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT id FROM memory_scopes WHERE scope_type = 'personal' AND owner_principal = %s",
            (alice,),
        )
        alices_scope = (cur.fetchone() or (None,))[0]

    with pytest.raises(notes.NoteNotFoundError):
        notes.write(
            migrated, carol, notes.NoteDraft(content="sneaky", scope_id=UUID(str(alices_scope)))
        )


def test_the_default_scope_is_your_own(migrated: Connection, alice: UUID) -> None:
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE))

    assert note.scope_type == "personal"
    assert note.is_mine is True


def test_a_personal_scope_is_created_on_demand(migrated: Connection, alice: UUID) -> None:
    """Most people never write a note, and a scope per principal would be a row
    per account in a table the permission filter joins on every query."""
    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_scopes WHERE scope_type = 'personal'")
        before = (cur.fetchone() or (0,))[0]

    notes.scopes_for(migrated, alice)

    with migrated.cursor() as cur:
        cur.execute("SELECT count(*) FROM memory_scopes WHERE scope_type = 'personal'")
        assert (cur.fetchone() or (0,))[0] == before + 1


def test_asking_twice_does_not_make_two_scopes(migrated: Connection, alice: UUID) -> None:
    notes.scopes_for(migrated, alice)
    notes.scopes_for(migrated, alice)

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM memory_scopes WHERE scope_type='personal' AND owner_principal=%s",
            (alice,),
        )
        assert cur.fetchone() == (1,)


# ---------------------------------------------------------------------------
# Editing is authorship, reading is scope. Different questions.
# ---------------------------------------------------------------------------


def test_a_shared_scope_is_not_somewhere_anyone_may_rewrite(
    migrated: Connection, alice: UUID, carol: UUID
) -> None:
    org = next(s for s in notes.scopes_for(migrated, alice) if s.scope_type == "org")
    note = notes.write(
        migrated, alice, notes.NoteDraft(content="Alice wrote this", scope_id=org.id)
    )

    assert notes.get_note(migrated, carol, note.id).is_mine is False
    with pytest.raises(notes.NotYoursError):
        notes.edit(migrated, carol, note.id, "Carol rewrote it")


def test_only_the_author_can_retire_or_erase(
    migrated: Connection, alice: UUID, carol: UUID
) -> None:
    org = next(s for s in notes.scopes_for(migrated, alice) if s.scope_type == "org")
    note = notes.write(
        migrated, alice, notes.NoteDraft(content="Alice wrote this", scope_id=org.id)
    )

    with pytest.raises(notes.NotYoursError):
        notes.supersede(migrated, carol, note.id)
    with pytest.raises(notes.NotYoursError):
        notes.erase(migrated, carol, note.id)


def test_pinning_is_about_ordering_not_permission(
    migrated: Connection, alice: UUID, carol: UUID
) -> None:
    """Anyone who can read a team note can pin it for the team. That is what
    makes a shared scope useful rather than one person's board."""
    org = next(s for s in notes.scopes_for(migrated, alice) if s.scope_type == "org")
    note = notes.write(migrated, alice, notes.NoteDraft(content="shared", scope_id=org.id))

    pinned = notes.set_pinned(migrated, carol, note.id, True)

    assert pinned.pinned is True


def test_pinned_notes_sort_first(migrated: Connection, alice: UUID) -> None:
    notes.write(migrated, alice, notes.NoteDraft(content="ordinary"))
    notes.write(migrated, alice, notes.NoteDraft(content="important", pinned=True))

    listed = notes.list_notes(migrated, alice)

    assert listed[0].content == "important"


def test_a_note_that_does_not_exist_is_not_found(migrated: Connection, alice: UUID) -> None:
    with pytest.raises(notes.NoteNotFoundError):
        notes.get_note(migrated, alice, uuid4())


def test_an_empty_edit_is_refused(migrated: Connection, alice: UUID) -> None:
    """Emptying a note is a supersede spelled badly, and would leave a blank
    source in the retrieval path."""
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE))

    with pytest.raises(notes.NoteError, match="supersede"):
        notes.edit(migrated, alice, note.id, "   ")


# ---------------------------------------------------------------------------
# Re-projection is safe, which is what makes the note the source of truth.
# ---------------------------------------------------------------------------


def test_re_editing_does_not_accumulate_chunks(migrated: Connection, alice: UUID) -> None:
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    for text in ("first revision", "second revision", "third revision"):
        notes.edit(migrated, alice, note.id, text, HashingEmbeddings())

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM chunks c JOIN memory_notes n ON n.entity_id = c.entity_id "
            "WHERE n.id = %s",
            (note.id,),
        )
        assert cur.fetchone() == (1,)


def test_an_unchanged_note_keeps_its_embedding(migrated: Connection, alice: UUID) -> None:
    """Content-addressed, so re-projecting does not pay to compute a vector
    that has not changed."""
    note = notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())
    with migrated.cursor() as cur:
        cur.execute(
            "SELECT c.id FROM chunks c JOIN memory_notes n ON n.entity_id = c.entity_id "
            "WHERE n.id = %s",
            (note.id,),
        )
        first = (cur.fetchone() or (None,))[0]

    notes.edit(migrated, alice, note.id, NOTE, HashingEmbeddings())

    with migrated.cursor() as cur:
        cur.execute(
            "SELECT c.id FROM chunks c JOIN memory_notes n ON n.entity_id = c.entity_id "
            "WHERE n.id = %s",
            (note.id,),
        )
        assert (cur.fetchone() or (None,))[0] == first


def test_a_note_never_becomes_a_second_read_path(migrated: Connection, alice: UUID) -> None:
    """The agent reaches a note the same way it reaches a Slack message: the
    one granted function. It holds no grant on memory_notes at all."""
    from psycopg import errors

    notes.write(migrated, alice, notes.NoteDraft(content=NOTE), HashingEmbeddings())

    with migrated.cursor() as cur:
        cur.execute('SET ROLE "hippo_agent"')
        with pytest.raises(errors.InsufficientPrivilege):
            cur.execute("SELECT * FROM memory_notes")
    migrated.rollback()
