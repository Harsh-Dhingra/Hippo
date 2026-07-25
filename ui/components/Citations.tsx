import type { Citation } from "@/lib/api";

/**
 * The sources an answer actually used.
 *
 * A citation without a working link is a footnote rather than evidence, so one
 * that could not be resolved says so instead of rendering as a dead link that
 * looks the same as a live one.
 */
export function Citations({ citations }: { citations: Citation[] }) {
  if (citations.length === 0) return null;

  return (
    <section className="mt-6">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
        Sources
      </h2>
      <ol className="mt-2 space-y-1 text-sm">
        {citations.map((citation) => (
          <li key={citation.marker} className="flex gap-2">
            <span className="text-[var(--color-muted)]">[{citation.marker}]</span>
            {citation.url ? (
              <a
                href={citation.url}
                target="_blank"
                rel="noreferrer"
                className="text-[var(--color-accent)] underline underline-offset-2"
              >
                {citation.title ?? citation.entity_type}
              </a>
            ) : (
              <span title="no link could be built for this source">
                {citation.title ?? citation.entity_type}
              </span>
            )}
            <span className="text-[var(--color-muted)]">{citation.entity_type}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}
