/**
 * Running a skill from the browser.
 *
 * A plain form post, so this works with JavaScript off like every other action
 * in this app. The inputs arrive as `input.<name>` fields and are collected
 * back into an object; the API validates them, so nothing here has to guess
 * which a skill declared.
 */

import { NextResponse } from "next/server";
import { ApiError, api } from "@/lib/api";

const INPUT_PREFIX = "input.";

export async function POST(request: Request) {
  const form = await request.formData();
  const skill = String(form.get("skill") ?? "");
  if (!skill) {
    return NextResponse.redirect(new URL("/skills", request.url), { status: 303 });
  }

  const inputs: Record<string, string> = {};
  for (const [key, value] of form.entries()) {
    if (key.startsWith(INPUT_PREFIX) && typeof value === "string" && value !== "") {
      inputs[key.slice(INPUT_PREFIX.length)] = value;
    }
  }

  try {
    const answer = await api.post<{ trace_id: string | null }>(
      `/api/v1/skills/${encodeURIComponent(skill)}/run`,
      { inputs },
    );
    // Straight to the trace, which shows the question the skill actually asked
    // alongside the answer. For a skill, that is the interesting part: a
    // template quietly rendering into something else is the failure worth
    // being able to see.
    const target = answer.trace_id ? `/traces/${answer.trace_id}` : "/skills";
    return NextResponse.redirect(new URL(target, request.url), { status: 303 });
  } catch (error) {
    const message =
      error instanceof ApiError ? error.message : "could not reach the server";
    return NextResponse.redirect(
      new URL(`/skills?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }
}
