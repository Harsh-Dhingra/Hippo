export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;

  return (
    <div className="mx-auto max-w-sm">
      <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
      <p className="mt-2 text-sm text-[var(--color-muted)]">
        You will see what your Slack and Jira accounts can see, and nothing else.
      </p>

      <form action="/api/session" method="post" className="mt-6 space-y-4">
        <label className="block">
          <span className="text-sm font-medium">Email</span>
          <input
            type="email"
            name="email"
            required
            autoComplete="username"
            className="mt-1 w-full rounded border border-[var(--color-line)] bg-white px-3 py-2 text-sm"
          />
        </label>
        <label className="block">
          <span className="text-sm font-medium">Password</span>
          <input
            type="password"
            name="password"
            required
            autoComplete="current-password"
            className="mt-1 w-full rounded border border-[var(--color-line)] bg-white px-3 py-2 text-sm"
          />
        </label>
        {error ? (
          <p role="alert" className="text-sm text-[var(--color-warn)]">
            {error}
          </p>
        ) : null}
        <button
          type="submit"
          className="w-full rounded bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white"
        >
          Sign in
        </button>
      </form>
    </div>
  );
}
