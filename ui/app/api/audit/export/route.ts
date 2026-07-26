import { SESSION_COOKIE } from "@/lib/api";
import { cookies } from "next/headers";

const API_BASE = process.env.HIPPO_API_URL ?? "http://localhost:8000";

/**
 * Stream an export through, rather than buffering it here.
 *
 * The whole point of the export is that it may be large. Reading it into this
 * server to hand it on would put a year of audit log in memory twice.
 */
export async function GET(request: Request) {
  const token = (await cookies()).get(SESSION_COOKIE)?.value;
  if (!token) {
    return new Response("not authenticated", { status: 401 });
  }

  const incoming = new URL(request.url);
  const upstream = await fetch(`${API_BASE}/api/v1/audit/export${incoming.search}`, {
    headers: { Authorization: `Bearer ${token}` },
    cache: "no-store",
  });

  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      "Content-Type": upstream.headers.get("content-type") ?? "text/plain",
      "Content-Disposition":
        upstream.headers.get("content-disposition") ?? "attachment",
    },
  });
}
