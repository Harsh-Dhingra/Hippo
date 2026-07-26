import { api, type AuditEvent } from "@/lib/api";
import { requireUser } from "@/lib/session";

export const dynamic = "force-dynamic";

const STATUSES = [
  "",
  "pending",
  "approved",
  "declined",
  "executed",
  "rolled_back",
  "failed",
] as const;

/**
 * How every action got where it is.
 *
 * The actions page shows what each action is now. This shows the transitions —
 * including the ones the actions table cannot hold, like a decline that
 * happened before someone else approved. Append-only in the database: no
 * service role can edit or delete a row here.
 */
export default async function AuditPage({
  searchParams,
}: {
  searchParams: Promise<{ status?: string; since?: string }>;
}) {
  await requireUser();
  const { status, since } = await searchParams;

  const query = new URLSearchParams({ limit: "200" });
  if (status) query.set("event_status", status);
  if (since) query.set("since", since);

  const events = await api.get<AuditEvent[]>(`/api/v1/audit?${query}`);
  const exportQuery = new URLSearchParams(query);

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Audit</h1>
      <p className="mt-2 mb-6 text-sm text-[var(--color-muted)]">
        Every transition, not just the current state — who decided, when, and what
        the payload said at that moment. Append-only: nothing in the application
        can edit or delete these.
      </p>

      <form method="get" className="mb-6 flex flex-wrap items-end gap-3">
        <label className="text-sm">
          <span className="block text-xs text-[var(--color-muted)]">Status</span>
          <select
            name="status"
            defaultValue={status ?? ""}
            className="mt-1 rounded border border-[var(--color-line)] px-2 py-1.5 text-sm"
          >
            {STATUSES.map((value) => (
              <option key={value} value={value}>
                {value === "" ? "all" : value}
              </option>
            ))}
          </select>
        </label>
        <label className="text-sm">
          <span className="block text-xs text-[var(--color-muted)]">Since</span>
          <input
            type="date"
            name="since"
            defaultValue={since ?? ""}
            className="mt-1 rounded border border-[var(--color-line)] px-2 py-1.5 text-sm"
          />
        </label>
        <button
          type="submit"
          className="rounded border border-[var(--color-line)] bg-white px-3 py-1.5 text-sm"
        >
          Filter
        </button>
        <span className="ml-auto flex gap-2 text-sm">
          <a
            href={`/api/audit/export?fmt=csv&${exportQuery}`}
            className="rounded border border-[var(--color-line)] bg-white px-3 py-1.5"
          >
            CSV
          </a>
          <a
            href={`/api/audit/export?fmt=jsonl&${exportQuery}`}
            className="rounded border border-[var(--color-line)] bg-white px-3 py-1.5"
          >
            JSONL
          </a>
        </span>
      </form>

      {events.length === 0 ? (
        <p className="rounded border border-[var(--color-line)] bg-white p-6 text-sm">
          Nothing matches. Actions you request will appear here as they move.
        </p>
      ) : (
        <table className="w-full table-auto border border-[var(--color-line)] bg-white text-sm">
          <thead>
            <tr className="border-b border-[var(--color-line)] text-left text-xs text-[var(--color-muted)]">
              <th className="px-3 py-2 font-medium">When</th>
              <th className="px-3 py-2 font-medium">Transition</th>
              <th className="px-3 py-2 font-medium">Decided by</th>
              <th className="px-3 py-2 font-medium">Action</th>
            </tr>
          </thead>
          <tbody>
            {events.map((event) => (
              <tr key={event.id} className="border-b border-[var(--color-line)] last:border-0">
                <td className="whitespace-nowrap px-3 py-2 text-[var(--color-muted)]">
                  {new Date(event.at).toLocaleString()}
                </td>
                <td className="px-3 py-2">
                  {event.from_status ? (
                    <span className="text-[var(--color-muted)]">{event.from_status} → </span>
                  ) : null}
                  <span className="font-medium">{event.to_status}</span>
                </td>
                <td className="px-3 py-2 text-[var(--color-muted)]">
                  {event.actor_policy ? (
                    <span title="A written policy stood in for a click; no person looked at this.">
                      {event.decided_by}
                    </span>
                  ) : (
                    event.decided_by
                  )}
                </td>
                <td className="px-3 py-2">{event.summary ?? event.action_type ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
