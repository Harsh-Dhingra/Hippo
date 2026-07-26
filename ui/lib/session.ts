/**
 * Reading and clearing the session cookie.
 *
 * Setting it happens in the login route handler, which is the only place that
 * ever sees a token. Everything else asks the API who it is talking to rather
 * than decoding anything locally: the server re-reads `disabled_at` and the
 * linked principal on every request, so a disabled account stops working now
 * rather than whenever a cached claim would have expired.
 */

import { redirect } from "next/navigation";
import { NotAuthenticated, api, type User } from "./api";

/**
 * Names the in-flight SSO login so the callback can tell it was this browser
 * that started it.
 *
 * Here rather than in the route that sets it: a Next.js route handler may only
 * export HTTP verbs and a short list of config fields, and `export const
 * SSO_STATE_COOKIE` in one is a build error that neither eslint nor
 * `tsc --noEmit` reports — only `next build` does.
 */
export const SSO_STATE_COOKIE = "hippo_sso_state";

export const COOKIE_OPTIONS = {
  httpOnly: true,
  sameSite: "lax",
  path: "/",
  // Off in local development, where the demo runs over http.
  secure: process.env.NODE_ENV === "production",
} as const;

/** The signed-in user, or a redirect to the login page. */
export async function requireUser(): Promise<User> {
  try {
    return await api.get<User>("/api/v1/me");
  } catch (error) {
    if (error instanceof NotAuthenticated) redirect("/login");
    throw error;
  }
}
