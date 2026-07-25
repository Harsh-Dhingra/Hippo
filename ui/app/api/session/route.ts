/**
 * Log in and log out.
 *
 * The only place a token is ever handled. It arrives from the API, goes
 * straight into an httpOnly cookie, and is never returned to the browser —
 * which is what stops one XSS anywhere in the app from becoming a readable
 * session for everything the victim can see.
 */

import { NextResponse } from "next/server";
import { ApiError, SESSION_COOKIE, api, type Session } from "@/lib/api";
import { COOKIE_OPTIONS } from "@/lib/session";

export async function POST(request: Request) {
  const form = await request.formData();
  const email = String(form.get("email") ?? "");
  const password = String(form.get("password") ?? "");

  let session: Session;
  try {
    session = await api.post<Session>("/api/v1/sessions", { email, password });
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : "could not reach the server";
    return NextResponse.redirect(
      new URL(`/login?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }

  const response = NextResponse.redirect(new URL("/ask", request.url), { status: 303 });
  response.cookies.set(SESSION_COOKIE, session.token, {
    ...COOKIE_OPTIONS,
    expires: new Date(session.expires_at),
  });
  return response;
}
