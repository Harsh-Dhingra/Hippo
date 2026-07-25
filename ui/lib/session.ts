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
