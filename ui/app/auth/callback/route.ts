/**
 * Where the identity provider sends people back.
 *
 * A GET, because that is what an IdP redirect is. The code in the query string
 * is exchanged server-side and the session token goes straight into the same
 * httpOnly cookie password login uses — the browser never holds either.
 *
 * The state is checked against the cookie set when the login began, so a
 * callback delivered to a browser that did not start this login goes nowhere.
 * The API checks it again against a single-use row; this is the half that can
 * tell one browser from another.
 */

import { NextResponse } from "next/server";
import { ApiError, SESSION_COOKIE, api, type Session } from "@/lib/api";
import { COOKIE_OPTIONS } from "@/lib/session";
import { SSO_STATE_COOKIE } from "@/app/api/sso/route";

type SsoSession = Session & { redirect_to?: string | null };

function failed(request: Request, message: string) {
  const response = NextResponse.redirect(
    new URL(`/login?error=${encodeURIComponent(message)}`, request.url),
    { status: 303 },
  );
  response.cookies.delete(SSO_STATE_COOKIE);
  return response;
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const state = url.searchParams.get("state");

  // The IdP declines by redirecting here with an error rather than a code.
  const refused = url.searchParams.get("error");
  if (refused) {
    return failed(request, "the identity provider declined the sign-in");
  }
  if (!code || !state) {
    return failed(request, "the sign-in did not complete");
  }

  const expected = request.headers
    .get("cookie")
    ?.split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(`${SSO_STATE_COOKIE}=`))
    ?.slice(SSO_STATE_COOKIE.length + 1);

  if (!expected || expected !== state) {
    return failed(request, "this sign-in was started in a different browser");
  }

  let session: SsoSession;
  try {
    session = await api.post<SsoSession>("/api/v1/auth/sso/callback", { code, state });
  } catch (error) {
    const message =
      error instanceof ApiError && error.status === 502
        ? "the identity provider is unavailable, please try again"
        : "could not sign you in";
    return failed(request, message);
  }

  const target =
    session.redirect_to && session.redirect_to.startsWith("/") ? session.redirect_to : "/ask";
  const response = NextResponse.redirect(new URL(target, request.url), { status: 303 });
  response.cookies.set(SESSION_COOKIE, session.token, {
    ...COOKIE_OPTIONS,
    expires: new Date(session.expires_at),
  });
  response.cookies.delete(SSO_STATE_COOKIE);
  return response;
}
