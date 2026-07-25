/**
 * Approve, decline, roll back.
 *
 * Plain form posts to a route handler rather than server actions, for one
 * reason worth stating: these three buttons are the human-approval gate that
 * CLAUDE.md rule 2 rests on, and a gate that stops working when a script fails
 * to load is not much of a gate. This way the demo is drivable with no
 * JavaScript at all, and it is drivable by a test.
 *
 * The verb is checked against a closed set. Interpolating whatever arrives in
 * the URL into an API path would let a link decide which endpoint gets called.
 */

import { NextResponse } from "next/server";
import { ApiError, NotAuthenticated, api } from "@/lib/api";

const VERBS = new Set(["approve", "decline", "rollback"]);

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string; verb: string }> },
) {
  const { id, verb } = await context.params;
  if (!VERBS.has(verb)) {
    return NextResponse.redirect(new URL("/actions?error=unknown+action", request.url), {
      status: 303,
    });
  }

  try {
    await api.post(`/api/v1/actions/${encodeURIComponent(id)}/${verb}`);
  } catch (error) {
    if (error instanceof NotAuthenticated) {
      return NextResponse.redirect(new URL("/login", request.url), { status: 303 });
    }
    const message = error instanceof ApiError ? error.message : "something went wrong";
    return NextResponse.redirect(
      new URL(`/actions?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }

  return NextResponse.redirect(new URL("/actions", request.url), { status: 303 });
}
