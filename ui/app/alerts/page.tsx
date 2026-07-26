import { api, type Alert } from "@/lib/api";
import { requireUser } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * The two failures that degrade quietly.
 *
 * A drifted connector keeps working and extracts slightly less. A failing sync
 * stream looks, from everywhere else, exactly like a stream with nothing to do
 * — and for the ACL stream those two states are a stale permission and a
 * current one.
 */
export default async function AlertsPage() {
  await requireUser();
  const alerts = await api.get<Alert[]>("/api/v1/alerts");

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Alerts</h1>
      <p className="mt-2 mb-6 text-sm text-[var(--color-muted)]">
        Schema drift and sync failures. Acknowledging one records that somebody
        looked; if the problem comes back it is raised again, because that is
        different news.
      </p>

      {alerts.length === 0 ? (
        <p className="rounded border border-[var(--color-line)] bg-[#f4fbf5] p-6 text-sm">
          Nothing is broken that Hippo can see. Sync is running and no connector
          has changed shape.
        </p>
      ) : (
        <ul className="space-y-3">
          {alerts.map((alert) => (
            <li
              key={alert.id}
              className="rounded border border-[#f0c4bf] bg-[#fff6f5] p-4"
            >
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="text-sm font-medium">{alert.headline}</span>
                {alert.is_recurring ? (
                  <span
                    className="text-xs text-[var(--color-warn)]"
                    title="One failure is a blip. This has repeated."
                  >
                    {alert.occurrences} times
                  </span>
                ) : null}
                <span className="text-xs text-[var(--color-muted)]">
                  since {new Date(alert.first_seen_at).toLocaleString()}
                </span>
              </div>
              <p className="mt-1 text-sm text-[var(--color-muted)]">{alert.detail}</p>
              <form
                action={`/api/alerts/${alert.id}/acknowledge`}
                method="post"
                className="mt-3"
              >
                <button
                  type="submit"
                  className="rounded border border-[var(--color-line)] bg-white px-3 py-1 text-sm"
                >
                  I have seen this
                </button>
              </form>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
