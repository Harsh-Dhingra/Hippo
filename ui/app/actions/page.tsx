import { api, type Action } from "@/lib/api";
import { requireUser } from "@/lib/session";

export const dynamic = "force-dynamic";

const TONE: Record<string, string> = {
  pending: "bg-[#fffdf5] border-[#e6d9a8]",
  approved: "bg-[#f5f9ff] border-[#c9ddf7]",
  executing: "bg-[#f5f9ff] border-[#c9ddf7]",
  executed: "bg-[#f4fbf5] border-[#bfe3c6]",
  rolling_back: "bg-[#f4fbf5] border-[#bfe3c6]",
  rolled_back: "bg-white border-[var(--color-line)]",
  declined: "bg-white border-[var(--color-line)]",
  failed: "bg-[#fff6f5] border-[#f0c4bf]",
};

function Button({
  children,
  variant = "plain",
}: {
  children: React.ReactNode;
  variant?: "primary" | "plain";
}) {
  const style =
    variant === "primary"
      ? "bg-[var(--color-accent)] text-white"
      : "border border-[var(--color-line)] bg-white";
  return (
    <button type="submit" className={`rounded px-3 py-1.5 text-sm font-medium ${style}`}>
      {children}
    </button>
  );
}

export default async function ActionsPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  await requireUser();
  const { error } = await searchParams;
  const actions = await api.get<Action[]>("/api/v1/actions");

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Actions</h1>
      <p className="mt-2 mb-6 text-sm text-[var(--color-muted)]">
        Everything the agent proposed. Nothing here has happened unless it says so —
        the agent can only ask.
      </p>

      {error ? (
        <p role="alert" className="mb-4 text-sm text-[var(--color-warn)]">
          {error}
        </p>
      ) : null}

      {actions.length === 0 ? (
        <p className="rounded border border-[var(--color-line)] bg-white p-6 text-sm">
          Nothing proposed yet. Ask for something to be changed and it will appear here.
        </p>
      ) : (
        <ul className="space-y-3">
          {actions.map((action) => (
            <li
              key={action.id}
              className={`rounded border p-4 ${TONE[action.status] ?? "border-[var(--color-line)] bg-white"}`}
            >
              <div className="flex items-baseline gap-3">
                <span className="text-sm font-medium">
                  {action.action_type} on {action.target_title ?? "—"}
                </span>
                <span className="text-xs uppercase tracking-wide text-[var(--color-muted)]">
                  {action.status}
                </span>
                {action.risk_class === "consequential" ? (
                  <span className="text-xs text-[var(--color-muted)]">
                    needs a human
                  </span>
                ) : null}
              </div>

              <pre className="mt-2 overflow-x-auto rounded bg-[#f7f8fa] p-3 text-xs">
                {JSON.stringify(action.payload, null, 2)}
              </pre>

              {action.error ? (
                <p className="mt-2 text-sm text-[var(--color-warn)]">{action.error}</p>
              ) : null}

              <div className="mt-3 flex gap-2">
                {action.status === "pending" ? (
                  <>
                    <form action={`/api/actions/${action.id}/approve`} method="post">
                      <Button variant="primary">Approve</Button>
                    </form>
                    <form action={`/api/actions/${action.id}/decline`} method="post">
                      <Button>Decline</Button>
                    </form>
                  </>
                ) : null}
                {action.status === "executed" ? (
                  <form action={`/api/actions/${action.id}/rollback`} method="post">
                    <Button>Roll back</Button>
                  </form>
                ) : null}
                {action.status === "approved" ? (
                  <span className="text-sm text-[var(--color-muted)]">
                    Waiting for the sync worker, which holds the credentials.
                  </span>
                ) : null}
              </div>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
