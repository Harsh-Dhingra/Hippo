import { NextResponse } from "next/server";
import { ApiError, NotAuthenticated, api } from "@/lib/api";

/** Write a note. A plain form post, so it works without JavaScript. */
export async function POST(request: Request) {
  const form = await request.formData();
  const content = String(form.get("content") ?? "").trim();
  const scopeId = String(form.get("scope_id") ?? "");

  try {
    await api.post("/api/v1/notes", {
      content,
      scope_id: scopeId || null,
      pinned: form.get("pinned") === "true",
    });
  } catch (error) {
    if (error instanceof NotAuthenticated) {
      return NextResponse.redirect(new URL("/login", request.url), { status: 303 });
    }
    const message = error instanceof ApiError ? error.message : "could not save that";
    return NextResponse.redirect(
      new URL(`/notes?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }
  return NextResponse.redirect(new URL("/notes", request.url), { status: 303 });
}
