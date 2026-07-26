import { api } from "@/lib/api";

type SsoStatus = {
  enabled: boolean;
  label: string;
  passwords_enabled: boolean;
};

/**
 * Asked at render time rather than baked in at build time, so an operator who
 * configures an IdP gets the button after a restart without rebuilding the UI.
 * Unauthenticated on purpose: nobody has a session yet when this page is drawn.
 */
async function ssoStatus(): Promise<SsoStatus> {
  try {
    return await api.get<SsoStatus>("/api/v1/auth/sso");
  } catch {
    // The API being unreachable is the sign-in form's problem to report, not a
    // reason to fail rendering the page it sits on.
    return { enabled: false, label: "Sign in with SSO", passwords_enabled: true };
  }
}

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ error?: string; next?: string }>;
}) {
  const { error, next } = await searchParams;
  const sso = await ssoStatus();
  // A path, never a URL. Whoever links here does not get to choose which site
  // somebody lands on after signing in.
  const redirectTo = next && next.startsWith("/") && !next.startsWith("//") ? next : "";

  return (
    <div className="mx-auto max-w-sm">
      <h1 className="text-xl font-semibold tracking-tight">Sign in</h1>
      <p className="mt-2 text-sm text-[var(--color-muted)]">
        You will see what your Slack and Jira accounts can see, and nothing else.
      </p>

      {error ? (
        <p role="alert" className="mt-4 text-sm text-[var(--color-warn)]">
          {error}
        </p>
      ) : null}

      {sso.enabled ? (
        <form action="/api/sso" method="post" className="mt-6">
          <input type="hidden" name="redirect_to" value={redirectTo} />
          <button
            type="submit"
            className="w-full rounded bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white"
          >
            {sso.label}
          </button>
        </form>
      ) : null}

      {sso.enabled && sso.passwords_enabled ? (
        <div className="mt-6 flex items-center gap-3" aria-hidden="true">
          <span className="h-px flex-1 bg-[var(--color-line)]" />
          <span className="text-xs uppercase tracking-wide text-[var(--color-muted)]">or</span>
          <span className="h-px flex-1 bg-[var(--color-line)]" />
        </div>
      ) : null}

      {sso.passwords_enabled ? (
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
          <button
            type="submit"
            className={
              sso.enabled
                ? "w-full rounded border border-[var(--color-line)] px-3 py-2 text-sm font-medium"
                : "w-full rounded bg-[var(--color-accent)] px-3 py-2 text-sm font-medium text-white"
            }
          >
            Sign in
          </button>
        </form>
      ) : null}
    </div>
  );
}
