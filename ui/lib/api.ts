/**
 * Talking to the Hippo API.
 *
 * THE BROWSER NEVER HOLDS THE TOKEN
 *
 * The obvious build is a client-side fetch with the bearer token in
 * localStorage. It is also the build where one XSS anywhere in the app hands an
 * attacker a session that reads everything the victim can read — which, for a
 * product whose entire claim is that it answers only what you are allowed to
 * see, is the worst possible bug.
 *
 * So the token lives in an httpOnly cookie that JavaScript cannot read, and
 * every call is made by this Next.js server, which attaches the Authorization
 * header. The browser holds a cookie it cannot inspect and talks only to its
 * own origin. That costs a hop and is worth it.
 *
 * SameSite=Lax rather than Strict: Strict would drop the cookie on a link
 * followed from Slack, which is exactly how someone arrives at a citation.
 */

import { cookies } from "next/headers";

export const SESSION_COOKIE = "hippo_session";

const API_BASE = process.env.HIPPO_API_URL ?? "http://localhost:8000";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

/** Signals that the caller should be sent back to the login page. */
export class NotAuthenticated extends ApiError {
  constructor(message = "not authenticated") {
    super(401, message);
    this.name = "NotAuthenticated";
  }
}

async function request<T>(
  path: string,
  init: RequestInit & { token?: string } = {},
): Promise<T> {
  const { token, ...rest } = init;
  const bearer = token ?? (await cookies()).get(SESSION_COOKIE)?.value;

  const response = await fetch(`${API_BASE}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(bearer ? { Authorization: `Bearer ${bearer}` } : {}),
      ...rest.headers,
    },
    // Answers depend on who is asking, so nothing here is cacheable.
    cache: "no-store",
  });

  if (response.status === 401) throw new NotAuthenticated();
  if (!response.ok) {
    const detail = await response
      .json()
      .then((body: { detail?: string }) => body.detail)
      .catch(() => undefined);
    throw new ApiError(response.status, detail ?? `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body?: unknown, token?: string) =>
    request<T>(path, {
      method: "POST",
      body: body === undefined ? undefined : JSON.stringify(body),
      token,
    }),
  patch: <T>(path: string, body?: unknown) =>
    request<T>(path, {
      method: "PATCH",
      body: body === undefined ? undefined : JSON.stringify(body),
    }),
  delete: <T>(path: string) => request<T>(path, { method: "DELETE" }),
};

// ---------------------------------------------------------------------------
// The shapes the API returns. Hand-written rather than generated from the
// OpenAPI document: this is the whole surface, it is small, and a generator
// would be a build step to maintain for four screens.
// ---------------------------------------------------------------------------

export type User = {
  id: string;
  email: string;
  display_name: string | null;
  is_admin: boolean;
  /** False when sync has not met this person yet. Not an error — a state. */
  has_access: boolean;
};

export type Session = { token: string; expires_at: string; user: User };

export type Citation = {
  marker: number;
  entity_id: string;
  entity_type: string;
  title: string | null;
  url: string | null;
};

export type Proposal = {
  id: string;
  action_type: string;
  summary: string;
  risk_class: string;
  status: string;
};

export type Answer = {
  answer: string;
  citations: Citation[];
  trace_id: string | null;
  refused: boolean;
  proposal: Proposal | null;
  model: string;
  input_tokens: number;
  output_tokens: number;
};

export type Action = {
  id: string;
  action_type: string;
  status: string;
  risk_class: string;
  payload: Record<string, unknown>;
  target_entity: string | null;
  summary: string | null;
  connector_kind: string;
  requested_by: string;
  approved_by: string | null;
  declined_by: string | null;
  rolled_back_by: string | null;
  error: string | null;
  created_at: string;
};

export type Scope = {
  id: string;
  scope_type: string;
  name: string;
};

export type Note = {
  id: string;
  scope_id: string;
  scope_type: string;
  scope_name: string;
  author: string;
  /** Reading is decided by scope; editing is decided by this. */
  is_mine: boolean;
  about_entity: string | null;
  content: string;
  pinned: boolean;
  superseded_at: string | null;
  created_at: string;
  updated_at: string;
};

export type Moment = {
  entity_id: string;
  entity_type: string;
  title: string | null;
  /** When the source says it happened, not when it was synced. */
  occurred_at: string | null;
  hops: number;
  via: string | null;
  relation: string;
  url: string | null;
  /** Undated: context for the chain rather than a step in it. */
  is_context: boolean;
};

export type Timeline = {
  subject: string;
  moments: Moment[];
  starts_at: string | null;
  ends_at: string | null;
};

export type TraceSummary = {
  id: string;
  question: string;
  route: string;
  model: string | null;
  answer: string | null;
  refused: boolean;
  action_id: string | null;
  error: string | null;
  input_tokens: number;
  output_tokens: number;
  duration_ms: number;
  created_at: string;
};

export type TraceStep = {
  name: string;
  duration_ms: number;
  detail: Record<string, unknown>;
};

export type TraceRetrieval = {
  rank: number;
  chunk_id: string;
  entity_id: string;
  entity_type: string | null;
  entity_title: string | null;
  content_hash: string | null;
  score: number;
  retrieval_modes: string[];
  cited: boolean;
};

export type Trace = TraceSummary & {
  plan: { query_text: string; rationale: string; hops: number; k: number };
  steps: TraceStep[];
  system_prompt: string | null;
  provider: string | null;
  citations: string[];
  retrievals: TraceRetrieval[];
};
