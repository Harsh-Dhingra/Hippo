# Hippo

**Open-source enterprise memory.** A permission-aware knowledge graph over your organization's systems — Slack, Jira, and more — with cited answers, full traceability, and human-approved actions. Self-hosted. One Postgres. Your infrastructure.

> Your organization's memory should belong to your organization.

## Why

Enterprise AI vendors are building "shared memory" platforms and calling the memory itself their moat. Read that from the customer's side: your company's collective knowledge, inside a proprietary box, priced per seat, forever. Hippo is the bet that the memory layer — like the operating system, the database, and the container runtime before it — is too important to be a walled garden.

## What it does

- **Syncs** your systems (Slack + Jira first) into a knowledge graph, preserving source truth and source permissions.
- **Answers** questions with hybrid retrieval (keyword + vector + graph) and citations that deep-link to the source. If you can't see it in the source system, Hippo can't show it to you — enforced in the database, not promised in app code.
- **Acts** only with approval. The agent proposes; a human approves; every action stores its inverse before executing, so rollback is a click, not a hope.
- **Explains** everything. Full trace per query: what was retrieved, what the model saw, why the answer says what it says.

## Design principles

1. One database. Postgres is the graph, the vectors, the search index, the queue, and the audit log. `docker compose up`, two containers, `pg_dump` is your disaster recovery.
2. Permissions are enforced by Postgres roles. The agent's database role physically cannot read content except through the permission filter. Inspect the grants yourself.
3. The agent never holds credentials. Write-back executes only in the sync worker, only after approval.
4. Source records are immutable. Resolution mistakes are fixed by re-running the resolver, never by re-syncing.
5. No telemetry. The only egress is your configured model API, and the trace log shows exactly what went into every prompt.

## Status

Pre-alpha. The founding documents are complete; implementation is underway. See [docs/PROJECT.md](docs/PROJECT.md) for the full roadmap as a fragment tree, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the system design, and [docs/STACK.md](docs/STACK.md) for stack decisions.

## License

Apache-2.0
