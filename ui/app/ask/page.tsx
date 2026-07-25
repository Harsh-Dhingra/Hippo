import { requireUser } from "@/lib/session";
import { AskForm } from "./AskForm";

export default async function AskPage() {
  const user = await requireUser();

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Ask</h1>
      <p className="mt-2 mb-6 text-sm text-[var(--color-muted)]">
        Answers are filtered by what {user.display_name ?? user.email} can see in Slack
        and Jira. That happens in the database, not in this page.
      </p>

      {user.has_access ? (
        <AskForm />
      ) : (
        // Said plainly rather than rendered as an empty answer: "you have
        // access to nothing" and "nothing matched" are different facts.
        <p className="rounded border border-[var(--color-line)] bg-white p-6 text-sm">
          No Slack or Jira account is linked to this login yet, so there is nothing you
          can see. That usually means sync has not run since you signed up.
        </p>
      )}
    </div>
  );
}
