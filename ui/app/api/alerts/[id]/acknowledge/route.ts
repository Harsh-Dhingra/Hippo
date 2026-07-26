import { NextResponse } from "next/server";
import { NotAuthenticated, api } from "@/lib/api";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id } = await context.params;
  try {
    await api.post(`/api/v1/alerts/${encodeURIComponent(id)}/acknowledge`);
  } catch (error) {
    if (error instanceof NotAuthenticated) {
      return NextResponse.redirect(new URL("/login", request.url), { status: 303 });
    }
  }
  return NextResponse.redirect(new URL("/alerts", request.url), { status: 303 });
}
