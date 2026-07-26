import { NextResponse } from "next/server";
import { ApiError, NotAuthenticated, api } from "@/lib/api";

// Closed set. Interpolating whatever arrives in the URL into an API path would
// let a link choose which endpoint gets called.
const VERBS = new Set(["edit", "pin", "supersede", "restore", "erase"]);

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string; verb: string }> },
) {
  const { id, verb } = await context.params;
  if (!VERBS.has(verb)) {
    return NextResponse.redirect(new URL("/notes?error=unknown+action", request.url), {
      status: 303,
    });
  }

  const form = await request.formData();
  const noteId = encodeURIComponent(id);

  try {
    if (verb === "edit") {
      await api.patch(`/api/v1/notes/${noteId}`, {
        content: String(form.get("content") ?? ""),
      });
    } else if (verb === "pin") {
      await api.post(`/api/v1/notes/${noteId}/pin`, {
        pinned: form.get("pinned") === "true",
      });
    } else if (verb === "erase") {
      await api.delete(`/api/v1/notes/${noteId}`);
    } else {
      await api.post(`/api/v1/notes/${noteId}/${verb}`);
    }
  } catch (error) {
    if (error instanceof NotAuthenticated) {
      return NextResponse.redirect(new URL("/login", request.url), { status: 303 });
    }
    const message = error instanceof ApiError ? error.message : "something went wrong";
    return NextResponse.redirect(
      new URL(`/notes?error=${encodeURIComponent(message)}`, request.url),
      { status: 303 },
    );
  }

  return NextResponse.redirect(new URL("/notes", request.url), { status: 303 });
}
