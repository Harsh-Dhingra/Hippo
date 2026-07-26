import { api, type Note, type Scope } from "@/lib/api";
import { requireUser } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * What the system has been told, and what you can change about it.
 *
 * A note here is not a sticky note. It is projected into the retrieval path, so
 * it is found and cited exactly like a synced Slack message — which is what
 * makes "transparent, editable memory" a property rather than a phrase. The
 * copy on this page says so, because a person deciding whether to write
 * something down should know it will end up in answers.
 */
export default async function NotesPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  await requireUser();
  const { error } = await searchParams;
  const [notes, scopes] = await Promise.all([
    api.get<Note[]>("/api/v1/notes"),
    api.get<Scope[]>("/api/v1/scopes"),
  ]);

  const live = notes.filter((note) => !note.superseded_at);
  const retired = notes.filter((note) => note.superseded_at);

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Memory</h1>
      <p className="mt-2 mb-6 text-sm text-[var(--color-muted)]">
        Notes are retrievable memory: what you write here is found and cited when
        someone asks a question, exactly like a synced message. Personal is yours
        alone; a team or org note is visible to everyone who shares that scope.
      </p>

      {error ? (
        <p role="alert" className="mb-4 text-sm text-[var(--color-warn)]">
          {error}
        </p>
      ) : null}

      <form
        action="/api/notes"
        method="post"
        className="mb-8 rounded border border-[var(--color-line)] bg-white p-4"
      >
        <label className="block">
          <span className="text-sm font-medium">Tell Hippo something</span>
          <textarea
            name="content"
            required
            rows={3}
            placeholder="The Acme renewal owner is Priya, not Alice."
            className="mt-1 w-full rounded border border-[var(--color-line)] px-3 py-2 text-sm"
          />
        </label>
        <div className="mt-3 flex items-center gap-3">
          <select
            name="scope_id"
            className="rounded border border-[var(--color-line)] px-2 py-1.5 text-sm"
          >
            {scopes.map((scope) => (
              <option key={scope.id} value={scope.id}>
                {scope.scope_type === "personal" ? "Personal" : scope.name}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1.5 text-sm text-[var(--color-muted)]">
            <input type="checkbox" name="pinned" value="true" />
            Pin
          </label>
          <button
            type="submit"
            className="ml-auto rounded bg-[var(--color-accent)] px-3 py-1.5 text-sm font-medium text-white"
          >
            Remember this
          </button>
        </div>
      </form>

      {live.length === 0 ? (
        <p className="rounded border border-[var(--color-line)] bg-white p-6 text-sm">
          Nothing yet. Anything you write above starts informing answers straight
          away.
        </p>
      ) : (
        <ul className="space-y-3">
          {live.map((note) => (
            <NoteCard key={note.id} note={note} />
          ))}
        </ul>
      )}

      {retired.length > 0 ? (
        <section className="mt-10">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
            Retired ({retired.length})
          </h2>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            No longer informing answers. Kept, because what a note used to say is
            the question an editable memory exists to answer.
          </p>
          <ul className="mt-3 space-y-3 opacity-70">
            {retired.map((note) => (
              <NoteCard key={note.id} note={note} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}

function NoteCard({ note }: { note: Note }) {
  const retired = Boolean(note.superseded_at);
  return (
    <li className="rounded border border-[var(--color-line)] bg-white p-4">
      <div className="flex items-baseline gap-2 text-xs text-[var(--color-muted)]">
        <span>{note.scope_type === "personal" ? "Personal" : note.scope_name}</span>
        {note.pinned ? <span>pinned</span> : null}
        {!note.is_mine ? <span>written by someone else</span> : null}
      </div>

      {note.is_mine && !retired ? (
        <form action={`/api/notes/${note.id}/edit`} method="post" className="mt-2">
          <textarea
            name="content"
            rows={2}
            defaultValue={note.content}
            className="w-full rounded border border-[var(--color-line)] px-3 py-2 text-sm"
          />
          <button
            type="submit"
            className="mt-2 rounded border border-[var(--color-line)] px-3 py-1 text-sm"
          >
            Save
          </button>
        </form>
      ) : (
        <p className="mt-2 whitespace-pre-wrap text-sm">{note.content}</p>
      )}

      <div className="mt-3 flex gap-2">
        <form action={`/api/notes/${note.id}/pin`} method="post">
          <input type="hidden" name="pinned" value={note.pinned ? "false" : "true"} />
          <button type="submit" className="rounded border border-[var(--color-line)] px-3 py-1 text-sm">
            {note.pinned ? "Unpin" : "Pin"}
          </button>
        </form>
        {note.is_mine ? (
          <>
            <form action={`/api/notes/${note.id}/${retired ? "restore" : "supersede"}`} method="post">
              <button
                type="submit"
                className="rounded border border-[var(--color-line)] px-3 py-1 text-sm"
              >
                {retired ? "Use again" : "Stop using"}
              </button>
            </form>
            {retired ? (
              <form action={`/api/notes/${note.id}/erase`} method="post">
                <button
                  type="submit"
                  className="rounded border border-[var(--color-line)] px-3 py-1 text-sm text-[var(--color-warn)]"
                  title="Permanent. Superseding is the reversible one."
                >
                  Erase
                </button>
              </form>
            ) : null}
          </>
        ) : null}
      </div>
    </li>
  );
}
