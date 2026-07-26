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
| Embeddings | **Config-abstracted; `mxbai-embed-large`, 1024-dim, measured at P2-EVAL-1** (see below) | Hardcoding a provider | Model deprecation or eval regression; re-embed is a resolver re-run, by design |
| API | **REST + OpenAPI. That's it.** | gRPC internal, GraphQL external, triple-protocol | A second first-party client (VS Code ext, CLI) demonstrates real REST pain — expected answer: never |
| Frontend | **Next.js + TypeScript + Tailwind** (the one place the pasted doc and I agree) | Server-rendered Python templates, SPA frameworks du jour | n/a |
| Deploy v0 | **docker compose: app + postgres. Two containers.** | K8s-first, Helm-first | Real multi-node adopters exist → charts at P4-ENT-2, compose stays forever as the front door |
| Auth | **Session auth v0 → OIDC at P2-GOV-3 → SCIM at P4-ENT-1** | Building auth cleverness early | Per plan |
| Token verification | **PyJWT + cryptography** (P2-GOV-3) | Authlib, hand-rolled RSA verification | Never hand-rolled; a second library only if PyJWT stops being maintained |
| Agent-tool surface | **One MCP server** (P3-SRF-2) | A native plugin per coding agent | Never — n plugins is n codebases re-implementing auth and drifting apart |
| Skill definitions | **YAML via `yaml.safe_load`** (P3-AGT-1) | TOML, a DSL, a database table | A skill needs control flow, which would mean it has stopped being configuration |
| Observability | **Prometheus endpoints + structured JSON logs from commit one; Grafana dashboard shipped in /deploy** | OTel full-trace mesh day one | OTel when a real adopter asks |
| CI quality gate | **mypy --strict, ruff, pytest w/ coverage floor, compose smoke test, role-grant leak test — all blocking, from commit one** | "We'll add tests later" | Never |

### On adding PyJWT (P2-GOV-3)

The refused list above is about infrastructure — things that add a process to
operate, a backup story, a second permission implementation. A JWT library adds
none of those, and the alternative is worse in a specific way: verifying an
RS256 signature by hand means implementing PKCS#1 v1.5 padding checks, and the
list of ways that goes subtly wrong is long and well documented. Python's
standard library has no RSA, so "no dependency" is not on the menu.

PyJWT over Authlib because it does one thing. Authlib is a full OAuth client and
server framework; the parts of it this project would use are the parts PyJWT
already is, and the rest is surface. `cryptography` arrives with it and is the
same library `httpx` already pulls in for TLS.

The check that matters is not in the library either way: the algorithm allowlist,
the issuer comparison, the audience, the nonce. Those are ours, in `api/oidc.py`,
and they are what `tests/test_oidc.py` spends most of its length on.

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

---

## The embedding pick, measured

This table deferred the choice "to a measured eval". The eval exists now
(`evals/`), so here is the measurement and the decision, on the 432-chunk
seeded corpus at k=20:

| model | recall | MRR | lexical | semantic | traversal | embed time |
|---|---|---|---|---|---|---|
| `hashing` (offline default) | 0.756 | 0.392 | 1.00 | **0.33** | 0.75 | 1.0s |
| **`mxbai-embed-large`** | **0.833** | 0.495 | 1.00 | **0.92** | 0.46 | 17.0s |
| `bge-m3` | 0.767 | 0.527 | 1.00 | **1.00** | 0.12 | 51.5s |

Reproduce with `python -m evals.embeddings` against any endpoint speaking the
OpenAI `/embeddings` shape. Both candidates were run through Ollama, so this
needs no account and no key.

**Why `mxbai-embed-large`.** Best overall recall, 1024 dimensions so the
`vector(1024)` column needs no migration, and three times faster to embed than
`bge-m3` — which matters, because embedding is the slowest step of a full
resync. `bge-m3` scores perfect semantic recall and the best MRR, and is the
right answer for a multilingual corpus; it loses here on the total.

**The finding worth carrying forward.** A better embedding model *costs*
traversal recall: 0.75 with the lexical baseline, 0.46 with `mxbai`, 0.12 with
`bge-m3`. Not a mistuned constant — sweeping the graph penalty across 10, 5, 2
and 0 moved traversal not at all, and dropping it below 5 cost semantic recall
instead. It is competition for a fixed k: a good embedder fills the top of the
result set with genuinely relevant direct hits, and a chunk reachable only
through an edge is legitimately outranked by them.

Fixing that means not making graph expansion compete on the same budget —
reserving slots, or a second retrieval pass — which is a design change and is
recorded here rather than smuggled into a constant.

**Why the default provider is still `hashing`.** Zero setup. `docker compose
up` has to work on a machine with no inference endpoint and no key, and the
demo has to be drivable in the first thirty seconds. The lexical baseline is
poor at paraphrase and honest about it; switching to the measured model is two
environment variables.

**What would change this.** A model that beats 0.833 overall recall on the same
corpus, or a corpus where the semantic and traversal numbers trade differently.
Re-run the comparison; do not argue from a leaderboard.
