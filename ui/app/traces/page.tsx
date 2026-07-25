import Link from "next/link";
import { api, type TraceSummary } from "@/lib/api";
import { requireUser } from "@/lib/session";

export default async function TracesPage() {
  await requireUser();
  const traces = await api.get<TraceSummary[]>("/api/v1/traces");

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Traces</h1>
      <p className="mt-2 mb-6 text-sm text-[var(--color-muted)]">
        Every question you have asked, and every step that produced the answer.
      </p>

      {traces.length === 0 ? (
        <p className="rounded border border-[var(--color-line)] bg-white p-6 text-sm">
          Nothing yet.
        </p>
      ) : (
        <ul className="divide-y divide-[var(--color-line)] rounded border border-[var(--color-line)] bg-white">
          {traces.map((trace) => (
            <li key={trace.id} className="px-4 py-3">
              <Link href={`/traces/${trace.id}`} className="block">
                <span className="text-sm">{trace.question}</span>
                <span className="mt-1 flex gap-3 text-xs text-[var(--color-muted)]">
                  <span>{trace.route}</span>
                  <span>{trace.duration_ms} ms</span>
                  <span>{trace.input_tokens + trace.output_tokens} tokens</span>
                  {trace.error ? (
                    <span className="text-[var(--color-warn)]">failed</span>
                  ) : null}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
