import { NextResponse } from "next/server";
import { SESSION_COOKIE, api } from "@/lib/api";

export async function POST(request: Request) {
  // Revoke server-side as well as forgetting the cookie. A cookie a browser has
  // dropped is still a live session to anyone who captured the token.
  await api.delete("/api/v1/sessions/current").catch(() => undefined);
  const response = NextResponse.redirect(new URL("/login", request.url), { status: 303 });
  response.cookies.delete(SESSION_COOKIE);
  return response;
}
