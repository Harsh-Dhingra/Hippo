import { notFound } from "next/navigation";
import { ApiError, api, type Trace } from "@/lib/api";
import { requireUser } from "@/lib/session";

/**
 * One query, in full.
 *
 * ARCHITECTURE §9 calls the trace a feature rather than debug output, and the
 * filtered-path demo is the reason: the retrieval list below is short for
 * someone without access because the filter returned little, not because
 * something was stripped afterwards. Showing the plan next to it is what makes
 * that visible — the same plan, a different result set.
 */
export default async function TracePage({ params }: { params: Promise<{ id: string }> }) {
  await requireUser();
  const { id } = await params;

  let trace: Trace;
  try {
    trace = await api.get<Trace>(`/api/v1/traces/${id}`);
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    throw error;
  }

  return (
    <div className="space-y-8">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">{trace.question}</h1>
        <p className="mt-1 flex gap-3 text-xs text-[var(--color-muted)]">
          <span>{trace.route}</span>
          <span>{trace.model ?? "no model call"}</span>
          <span>{trace.input_tokens + trace.output_tokens} tokens</span>
          <span>{trace.duration_ms} ms</span>
        </p>
      </header>

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
          Plan
        </h2>
        <p className="mt-2 rounded border border-[var(--color-line)] bg-white p-4 text-sm">
          {trace.plan?.rationale ?? "—"}
        </p>
      </section>

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
          Steps
        </h2>
        <ol className="mt-2 divide-y divide-[var(--color-line)] rounded border border-[var(--color-line)] bg-white">
          {trace.steps.map((step, index) => (
            <li key={index} className="flex items-baseline gap-3 px-4 py-2 text-sm">
              <span className="font-medium">{step.name}</span>
              <span className="text-xs text-[var(--color-muted)]">{step.duration_ms} ms</span>
              <span className="ml-auto text-xs text-[var(--color-muted)]">
                {Object.entries(step.detail)
                  .map(([key, value]) => `${key}=${String(value)}`)
                  .join("  ")}
              </span>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
          Retrieved ({trace.retrievals.length})
        </h2>
        <p className="mt-1 text-xs text-[var(--color-muted)]">
          Everything the permission filter returned, in rank order. What is not here was
          never shown to the model.
        </p>
        <table className="mt-2 w-full table-auto border border-[var(--color-line)] bg-white text-sm">
          <thead>
            <tr className="border-b border-[var(--color-line)] text-left text-xs text-[var(--color-muted)]">
              <th className="px-3 py-2 font-medium">#</th>
              <th className="px-3 py-2 font-medium">Source</th>
              <th className="px-3 py-2 font-medium">Found by</th>
              <th className="px-3 py-2 font-medium">Score</th>
              <th className="px-3 py-2 font-medium">Cited</th>
            </tr>
          </thead>
          <tbody>
            {trace.retrievals.map((hit) => (
              <tr key={hit.rank} className="border-b border-[var(--color-line)] last:border-0">
                <td className="px-3 py-2 text-[var(--color-muted)]">{hit.rank}</td>
                <td className="px-3 py-2">
                  {hit.entity_title ?? hit.entity_type ?? hit.entity_id}
                </td>
                <td className="px-3 py-2 text-[var(--color-muted)]">
                  {hit.retrieval_modes.join(", ")}
                </td>
                <td className="px-3 py-2 text-[var(--color-muted)]">{hit.score.toFixed(4)}</td>
                <td className="px-3 py-2">{hit.cited ? "yes" : ""}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {trace.system_prompt ? (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
            System prompt
          </h2>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            The only thing that left this machine, besides the sources above.
          </p>
          <pre className="mt-2 overflow-x-auto whitespace-pre-wrap rounded border border-[var(--color-line)] bg-white p-4 text-xs">
            {trace.system_prompt}
          </pre>
        </section>
      ) : null}

      {trace.answer ? (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
            Answer
          </h2>
          <p className="mt-2 whitespace-pre-wrap rounded border border-[var(--color-line)] bg-white p-4 text-sm">
            {trace.answer}
          </p>
        </section>
      ) : null}

      {trace.error ? (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
            Error
          </h2>
          <p className="mt-2 rounded border border-[var(--color-line)] bg-white p-4 text-sm text-[var(--color-warn)]">
            {trace.error}
          </p>
        </section>
      ) : null}
    </div>
  );
}
