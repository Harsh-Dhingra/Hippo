import { ApiError, api, type Timeline } from "@/lib/api";
import { requireUser } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * What happened around one thing, in order.
 *
 * The chain reads top to bottom in the order the *sources* say things happened,
 * not in the order Hippo synced them. Entities the source never dated — people,
 * channels, projects — are shown separately as context: they are why the chain
 * hangs together rather than steps in it.
 */
export default async function TimelinePage({
  params,
  searchParams,
}: {
  params: Promise<{ id: string }>;
  searchParams: Promise<{ hops?: string }>;
}) {
  await requireUser();
  const { id } = await params;
  const { hops } = await searchParams;

  let chain: Timeline;
  try {
    chain = await api.get<Timeline>(
      `/api/v1/timeline/${id}?hops=${hops === "3" ? 3 : 2}`,
    );
  } catch (error) {
    if (error instanceof ApiError) {
      return <p className="text-sm text-[var(--color-warn)]">{error.message}</p>;
    }
    throw error;
  }

  const events = chain.moments.filter((m) => !m.is_context);
  const context = chain.moments.filter((m) => m.is_context);

  if (chain.moments.length === 0) {
    return (
      <div>
        <h1 className="text-xl font-semibold tracking-tight">Timeline</h1>
        <p className="mt-4 rounded border border-[var(--color-line)] bg-white p-6 text-sm">
          Nothing here you have access to. That may mean this does not exist, or
          that it lives somewhere you cannot see.
        </p>
      </div>
    );
  }

  return (
    <div>
      <h1 className="text-xl font-semibold tracking-tight">Timeline</h1>
      <p className="mt-2 mb-8 text-sm text-[var(--color-muted)]">
        {events.length} event{events.length === 1 ? "" : "s"}
        {chain.starts_at && chain.ends_at ? (
          <>
            {" "}
            between {new Date(chain.starts_at).toLocaleDateString()} and{" "}
            {new Date(chain.ends_at).toLocaleDateString()}
          </>
        ) : null}
        , in the order the sources say they happened. Filtered to what you can see.
      </p>

      <ol className="relative border-l border-[var(--color-line)] pl-6">
        {events.map((moment) => (
          <li key={moment.entity_id} className="relative mb-6">
            <span
              className="absolute -left-[1.6rem] top-1.5 h-2 w-2 rounded-full bg-[var(--color-accent)]"
              aria-hidden
            />
            <div className="flex flex-wrap items-baseline gap-2 text-xs text-[var(--color-muted)]">
              <time dateTime={moment.occurred_at ?? undefined}>
                {moment.occurred_at
                  ? new Date(moment.occurred_at).toLocaleString()
                  : ""}
              </time>
              <span>{moment.entity_type}</span>
              <span>· {moment.relation}</span>
            </div>
            <p className="mt-1 text-sm">
              {moment.url ? (
                <a
                  href={moment.url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-[var(--color-accent)] underline underline-offset-2"
                >
                  {moment.title ?? moment.entity_type}
                </a>
              ) : (
                (moment.title ?? moment.entity_type)
              )}
            </p>
          </li>
        ))}
      </ol>

      {context.length > 0 ? (
        <section className="mt-8 border-t border-[var(--color-line)] pt-6">
          <h2 className="text-xs font-semibold uppercase tracking-wide text-[var(--color-muted)]">
            Context
          </h2>
          <p className="mt-1 text-xs text-[var(--color-muted)]">
            The sources give these no time of their own. They are why the chain
            hangs together rather than steps in it.
          </p>
          <ul className="mt-3 flex flex-wrap gap-2">
            {context.map((moment) => (
              <li
                key={moment.entity_id}
                className="rounded border border-[var(--color-line)] bg-white px-2 py-1 text-xs"
              >
                {moment.title ?? moment.entity_type}{" "}
                <span className="text-[var(--color-muted)]">{moment.entity_type}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <p className="mt-8 text-xs text-[var(--color-muted)]">
        <a href={`?hops=${hops === "3" ? 2 : 3}`} className="underline underline-offset-2">
          {hops === "3" ? "Narrow to two hops" : "Widen to three hops"}
        </a>
        {" — three tends to reach the whole workspace through a shared channel."}
      </p>
    </div>
  );
}
