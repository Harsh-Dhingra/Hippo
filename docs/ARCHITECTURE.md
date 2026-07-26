# Architecture v0 — Hippo

**Status:** Proposed
**Date:** 2026-07-24
**Scope:** Self-hosted, single-tenant, single-org. Slack + Jira connectors. One agent loop. One write-back action class.
**Non-goals for v0:** multi-tenancy, SaaS hosting, no-code agent builder, voice, more than two connectors, horizontal scaling.

---

## 1. Mission constraint (shapes everything)

An organization's memory should belong to the organization. Therefore:

- Runs entirely on infrastructure the org controls (`docker compose up`).
- One database. Postgres is the graph, the vector store, the queue, and the audit log. A self-hoster who can run Postgres can run this.
- No phone-home, no telemetry by default, no external services required except the LLM API (pluggable, including local models later).

This constraint is the product. Every architecture decision below is downstream of it.

---

## 2. System overview

```
                        ┌──────────────────────────────────────────────┐
                        │                  Postgres 16                 │
                        │  raw_records │ entities │ edges │ chunks     │
                        │  acl_grants  │ principals │ actions │ jobs   │
                        │  (pgvector)  │ (LISTEN/NOTIFY as bus)        │
                        └──────┬───────────────┬───────────────┬───────┘
                               │               │               │
        writes raw + ACLs      │    reads raw, writes graph    │   reads graph (filtered)
                               │               │               │
   ┌───────────┐        ┌──────┴─────┐   ┌─────┴──────┐   ┌────┴───────┐
   │  Slack /  │◄──────►│    SYNC    │   │  RESOLVER  │   │   AGENT    │◄──── user (API/UI)
   │  Jira     │ 2-way  │   WORKERS  │   │  PIPELINE  │   │   SERVICE  │
   └───────────┘        └────────────┘   └────────────┘   └────┬───────┘
                               ▲                               │
                               └───────── write-back ◄─────────┘
                                     (via actions table)
```

Four processes, one database:

| Process | Responsibility | Never does |
|---|---|---|
| **Sync workers** | Pull from sources → `raw_records` + `acl_grants` + `sync_state`. Execute approved actions from `actions` table (write-back). | Interpret content. Touch `entities`. |
| **Resolver pipeline** | `raw_records` → `entities`, `edges`, `chunks` (+ embeddings). Identity resolution. Summarization. | Call source APIs. Bypass provenance. |
| **Agent service** | Query planning, retrieval, answer synthesis with citations, action proposals. | Read `chunks` except through the permission filter. Execute actions directly. |
| **API/UI** | REST API + minimal web UI. Auth, session, approval buttons. | Business logic. |

Separation rule: **the only component that talks to source systems is the sync worker; the only component that talks to the model is the agent service.** This makes connectors testable with fixtures and the agent testable with a seeded graph.

## 3. Data flow (read path)

1. User asks: "what's blocking the Acme renewal?"
2. Agent service resolves the asking user → principal + expanded groups (one query).
3. Query planner picks strategy: vector search over `chunks`, graph expansion from hit entities (1-2 hops over `edges`), or both.
4. **Every retrieval goes through `visible_chunks(principal, query_embedding)` — a single SQL function that joins `acl_grants` and scope membership. There is no other read path to chunks. This is enforced by Postgres grants: the agent's DB role has EXECUTE on the function and no SELECT on the underlying tables.**
5. Retrieved chunks + graph context assembled into prompt. Model answers with citations = `entity_id`s, rendered as deep links to the source system.
6. Answer logged (query, chunks used, model, tokens) for the trace view.

Decision: ACL enforcement in the database via role grants, not in application code. App-level filters get bypassed by the next contributor's helper function; a DB role without SELECT can't be.

## 4. Data flow (write path / actions)

1. Agent proposes an action: `{action_type, target_entity, payload, risk_class}` → row in `actions`, status `pending`.
2. Risk policy (config file, not code): `routine` auto-approves; `consequential` waits for a human click. v0 default: **everything is consequential.** Auto-approval is opt-in per action type.
3. On approval, sync worker (a) fetches current state of the target and stores it as `inverse_payload`, (b) executes, (c) marks `executed`. Rollback = execute the inverse, mark `rolled_back`.
4. Every state transition is a timestamped row. The audit log is the table itself.

Decision: the agent cannot execute actions. It can only insert `pending` rows. The DB role for the agent service has INSERT on `actions` but the sync worker role alone holds the source-system credentials. Compromised prompt ≠ executed action.

## 5. Sync engine

**Model per connector:** each connector implements four streams — `identities` (users/groups → principals), `content` (messages/issues → raw_records), `acls` (who sees what → acl_grants), `writeback` (execute approved actions).

**Mechanics:**
- Cursor-based incremental sync per stream, cursor stored in `sync_state.cursor` (jsonb — each connector defines its own cursor shape; Slack uses ts, Jira uses updated-since + pagination token).
- Full resync = delete cursor, not delete data. Upserts on `(connector_id, source_type, source_id)` make resync idempotent.
- Rate limiting per connector with exponential backoff; 429s update `sync_state.last_error` and reschedule, never crash the worker.
- Schema drift: each connector pins an expected source schema version; on unknown fields, store them anyway (payload is verbatim jsonb) and log a drift warning. Drift never drops data.
- ACL sync is not eventually-consistent-whenever: **ACL streams run at a higher frequency than content streams.** A revoked permission must propagate within minutes, not at the next nightly sync. v0 target: ACL sync ≤ 5 min, content sync ≤ 15 min.

**Job scheduling:** a `jobs` table + `SELECT ... FOR UPDATE SKIP LOCKED` polling loop. No Redis, no Celery, no Kafka.

Trade-off, stated honestly: Postgres-as-queue caps throughput far below a real broker. At v0 scale (one org, two connectors) it is orders of magnitude more than needed, and it keeps the deploy story at one container + one database. Revisit when a single org's event volume makes sync latency miss its targets. That day is far away and the migration path (jobs table → NOTIFY → broker) is well-trodden.

## 6. Resolver pipeline

Three stages, each idempotent, each re-runnable over all of `raw_records` without touching sync:

1. **Extraction** — raw record → candidate entity/edge assertions. Deterministic per source type (a Jira issue yields a ticket entity, authored edge, belongs_to project edge). No model calls.
2. **Identity resolution** — merge candidates into canonical entities. v0 rule set, in order: exact source-ID match → email match for persons → normalized-name match for accounts. Model-assisted fuzzy matching is explicitly out of v0; when it arrives, its edges carry `provenance='model'` and `confidence<1.0` so they can be distrusted or filtered wholesale.
3. **Enrichment** — chunking (per-message for Slack, per-field for Jira descriptions/comments), embedding, entity summaries. The only stage that calls a model, and summaries are regenerated, never appended, so stale summaries can't accumulate.

Decision: resolver reads only `raw_records`, writes only graph tables. If resolution logic is wrong, fix and re-run; source data is untouched. Re-resolution is a normal operation, not a disaster recovery.

## 7. Agent service

- **v0 is one agent** (answer + propose-action), not a framework. LangGraph for the loop since you know it cold, but the graph is small: plan → retrieve → (optional) expand → synthesize → (optional) propose action.
- Model access behind a single provider interface (Anthropic first, OpenAI-compatible second, local via Ollama third). The mission constraint demands the local option exists on the roadmap even if v0 quality with local models is poor.
- Full trace per query stored: plan, retrieval results (entity IDs only, not content, to keep traces cheap), prompt token counts, answer, citations. The trace view is a feature, not debug output — "check every thinking step" is table stakes in this category.

## 8. Non-functional targets (v0)

| Dimension | Target | Rationale |
|---|---|---|
| Deploy | `docker compose up`, 2 containers (app, postgres) | Self-hoster credibility |
| Scale | 1 org, ≤ 500 seats, ≤ 5M raw records | Covers the mid-market org that would actually self-host |
| Query latency | < 5s end-to-end with citations | Chat-acceptable |
| ACL propagation | ≤ 5 min from source revocation | The security story |
| Recovery | Postgres backup = full system backup | One thing to back up |

## 9. Security posture (v0)

- Secrets (source-system tokens) in environment / mounted secrets file, never in `connectors.config`, never in the DB.
- Three Postgres roles: `sync` (raw + acl + actions execute), `resolver` (raw read, graph write), `agent` (function-only chunk access, actions insert). Defense in depth is the schema.
- All source content stays in the org's Postgres. The only egress is prompts to the configured model API — and the trace log records exactly what went into every prompt, so the egress is auditable.
- Prompt-injection stance: content from Slack/Jira is data, not instructions. Retrieved chunks are wrapped and delimited in the prompt; the agent's action proposals always route through the human-approval gate regardless of what retrieved content says. v0's "everything is consequential" default is the mitigation.

## 10. Repo layout

```
/core        schema, migrations, permission filter, shared types
/sync        worker runtime + /sync/connectors/{slack,jira}/  (the SDK boundary)
/resolver    extraction, resolution, enrichment stages
/agent       loop, provider interface, trace
/api         REST + auth
/ui          minimal web (query, citations, approvals, trace view)
/deploy      docker compose, sample env
```

`/sync/connectors/` interface is designed as if external contributors will implement it in month three, because they will or the project fails Horizon 2.

## 11. Decisions log (summary)

| # | Decision | Rejected alternative | Revisit when |
|---|---|---|---|
| 1 | Postgres for everything | Neo4j graph, Qdrant vectors, Redis queue | Sync latency misses targets or >5M records |
| 2 | ACL filter as DB function + role grants | App-level filtering | Never — this one is load-bearing |
| 3 | Agent proposes, never executes | Direct tool execution | Never for consequential; routine auto-approve is config |
| 4 | raw_records / entities split | Resolve-on-ingest | Never — re-resolution is the recovery story |
| 5 | Single-tenant self-host | Multi-tenant SaaS-ready | A hosted offering exists (Horizon 3) |
| 6 | One agent, small LangGraph | Agent framework / no-code builder | Community demand post-Horizon 1 |
| 7 | Everything-consequential default | Risk-tiered auto-execution | After the approval UX and rollback are proven |
| 8 | OIDC subject is the user identity; email only maps to principals | Email as the account key | SCIM lands (Phase 4) and group membership arrives with it |
| 9 | Passwords stay alongside SSO | SSO-only once configured | Never — an install whose only way in is the IdP has none when the IdP is down |

**On 8.** The link from a login to a principal is an email match, because an
email is the only thing connectors agree on. That makes email an authorisation
input, so whoever gets to assert one decides what a session can read. Two
consequences the code enforces: an unverified `email_verified` claim maps to no
principal at all, and an address already claimed by a different IdP subject is
refused rather than adopted — a reissued address must not inherit the previous
holder's history.

## 12. Definition of done, v0

A fresh machine, `docker compose up`, connect one Slack workspace and one Jira project, wait for sync, then:

1. Ask "what's blocking [project]" → cited answer spanning a Slack thread and a Jira ticket, links resolve.
2. A second user who lacks access to the private channel asks the same question → answer contains nothing from that channel. **This is the demo. Not the happy path — the filtered path.**
3. "Add a comment on JIRA-123 summarizing this" → pending action → approve in UI → comment appears in Jira → rollback in UI → comment gone.
4. Trace view shows every step of all three.

Ship that, record the 3-minute demo of point 2 and 3, and Horizon 1 is done.
