# STACK.md — The Stack, Decided

**Status:** Accepted. This closes the stack discussion. Changes to this file require a measured trigger (see column 4), not a preference.

**Governing principle (from ARCHITECTURE §1):** the adopter must be able to run this. Every component below is judged first on "can a mid-market IT team operate it," second on capability. A component that is technically superior but operationally expensive for adopters is the wrong component for this project.

---

## The decisions

| Layer | Decision | Rejected | Graduation trigger (when to revisit) |
|---|---|---|---|
| Language | **Python 3.12, strict mypy, pydantic at every boundary, ruff, uv** | Rust core, Go sync, polyglot | Sync workers miss the ≤5-min ACL target under measurement → sync runtime only rewritten in Rust behind same SDK contract (P4-SCALE-1) |
| Primary store, graph, metadata | **Postgres 16** | Neo4j, Memgraph, Apache AGE | Multi-hop graph queries >500ms p95 at real scale → AGE extension first (still Postgres), dedicated graph DB last |
| Vectors | **pgvector (HNSW)** | Qdrant, Weaviate, pinecone-likes | Recall/latency degrades past ~10M chunks measured | 
| Keyword search | **Postgres FTS (BM25-class), fused with vector + graph in P1-AGT-2** | OpenSearch, Elasticsearch, Meilisearch | FTS relevance provably insufficient on the eval harness (P2-EVAL-1), not on vibes |
| Queue / bus | **jobs table + SKIP LOCKED; LISTEN/NOTIFY for wakeups** | Kafka, NATS, Redis+Celery, RabbitMQ | Queue depth/latency misses sync targets for a single org (P4-SCALE-1) |
| Workflow / orchestration | **The jobs runtime + explicit state columns** | Temporal, Airflow, Prefect | A workflow genuinely needs >1-week durable timers or human-in-loop sagas beyond the actions table — none in Phases 1-3 |
| Agent loop | **LangGraph (small graph), provider-abstracted models** | Custom planner-DAG framework, "better LangGraph" | The loop outgrows a small graph AND the eval harness shows planning is the bottleneck |
| Model providers | **Anthropic + OpenAI-compatible day one; Ollama/vLLM path at P4-LOCAL-1** | Single-provider lock | n/a — pluggability is permanent |
| Embeddings | **Config-abstracted; default: a current strong open-weights model, 1024-dim** (final pick at P1-RES-3 with a small eval) | Hardcoding a provider | Model deprecation or eval regression; re-embed is a resolver re-run, by design |
| API | **REST + OpenAPI. That's it.** | gRPC internal, GraphQL external, triple-protocol | A second first-party client (VS Code ext, CLI) demonstrates real REST pain — expected answer: never |
| Frontend | **Next.js + TypeScript + Tailwind** (the one place the pasted doc and I agree) | Server-rendered Python templates, SPA frameworks du jour | n/a |
| Deploy v0 | **docker compose: app + postgres. Two containers.** | K8s-first, Helm-first | Real multi-node adopters exist → charts at P4-ENT-2, compose stays forever as the front door |
| Auth | **Session auth v0 → OIDC at P2-GOV-3 → SCIM at P4-ENT-1** | Building auth cleverness early | Per plan |
| Observability | **Prometheus endpoints + structured JSON logs from commit one; Grafana dashboard shipped in /deploy** | OTel full-trace mesh day one | OTel when a real adopter asks |
| CI quality gate | **mypy --strict, ruff, pytest w/ coverage floor, compose smoke test, role-grant leak test — all blocking, from commit one** | "We'll add tests later" | Never |

## Why the whole stack is one database (the argument, once, in full)

1. **The permission filter is one SQL function** because chunks, FTS index, vectors, and graph edges live in one engine. Every store added is a second permission implementation in a second query language — a new leak surface in the one place this project can never leak. This is the load-bearing reason and it is sufficient on its own.
2. **Backup = pg_dump.** The adopter's disaster story is one command. Six stateful services = six backup stories = no mid-market adoption.
3. **Transactions across "layers."** Entity + edges + chunks + ACL grants commit atomically. Across Neo4j+Qdrant+OpenSearch that's a distributed consistency problem you'd own forever, and eventual consistency in *permissions* is a security bug with a delay timer.
4. **The graduation triggers are real.** Nothing above is Postgres romanticism — every layer has a measured exit. The discipline is that exits require numbers from the eval harness or ops metrics, not architecture-doc aesthetics.

## The refused stack, named (so it stays refused)

Kafka, NATS, Temporal, Neo4j, Memgraph, Qdrant, OpenSearch, gRPC, GraphQL, Go-and-Rust-and-Python polyglot. Each is a fine tool. Together they are a platform team's stack for a project whose entire premise is that the adopter doesn't need a platform team. The pasted RFC's stack would make this project DevRev-shaped: impressive, heavy, and closed to everyone who can't operate it — which is the opposite of the mission.

## Merged from the RFC (credit where due)

- Hybrid retrieval (BM25+vector+graph+temporal) → P1-AGT-2, upgraded.
- Memory Timeline → P2-MEM-3, new fragment, differentiator.
- "Chat is one client, the API is the OS" → already ARCHITECTURE §SURFACE; affirmed.
- Explainability path → already P1-AGT-4; affirmed.
- Normalized event-stream connectors → structurally present (streams → raw_records, NOTIFY as bus); the Kafka form graduates at P4-SCALE-1 if ever.
