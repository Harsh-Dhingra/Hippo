/**
 * Starting a single sign-on login.
 *
 * A plain form post, so this works with JavaScript turned off like every other
 * action in this app. The API mints the state and holds the PKCE verifier; all
 * this does is send the browser onward and remember which login it started.
 *
 * THE STATE COOKIE
 *
 * The flow row in the database is already single-use, which stops a state being
 * replayed. This cookie answers a different question: whether the browser
 * arriving at the callback is the one that began the login. Without it an
 * attacker can complete *their own* login in somebody else's browser, and that
 * person then quietly works inside the attacker's account.
 *
 * Short-lived, httpOnly, and cleared on the way out.
 */

import { NextResponse } from "next/server";
import { ApiError, api } from "@/lib/api";
import { COOKIE_OPTIONS } from "@/lib/session";

export const SSO_STATE_COOKIE = "hippo_sso_state";

type Start = { authorization_url: string; state: string };

export async function POST(request: Request) {
  const form = await request.formData().catch(() => null);
  const raw = String(form?.get("redirect_to") ?? "");
  // Belt and braces: the API refuses anything that could leave the site, and
  // so does this. An open redirect on a login route is a phishing primitive.
  const redirect_to = raw.startsWith("/") && !raw.startsWith("//") ? raw : null;

  let start: Start;
  try {
    start = await api.post<Start>("/api/v1/auth/sso/start", { redirect_to });
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 404
        ? "single sign-on is not configured"
        : "could not reach the identity provider";
    return NextResponse.redirect(
      new URL(`/login?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }

  const response = NextResponse.redirect(start.authorization_url, { status: 303 });
  response.cookies.set(SSO_STATE_COOKIE, start.state, {
    ...COOKIE_OPTIONS,
    maxAge: 600,
  });
  return response;
}
