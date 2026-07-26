# Contributing to Hippo

Hippo is a permission-aware memory system. That single fact decides most of what
follows: the failure mode here is not a crash, it is a fluent, well-cited answer
built from something the person asking was never allowed to read. Nothing pages
when that happens. So the review bar is shaped around the failures that are
silent, and it is stricter than the bar for a normal application in exactly
those places and no others.

---

## The fastest useful contribution

A connector. Read [docs/CONNECTORS.md](docs/CONNECTORS.md), run:

```
hippo-new-connector notion --out ~/src
cd ~/src/hippo-notion && pip install -e . && pytest
```

and you have a package that already passes conformance. It installs alongside
Hippo through an entry point, so **most connectors should stay in their own
repository** — nothing here has to change for yours to work, and you keep
release control.

---

## Getting set up

```
uv sync                       # Python 3.12, all dev dependencies
createdb hippo && hippo-migrate up
pytest                        # needs Postgres 16 with pgvector
cd ui && npm install && npm run dev
```

Tests that need a database are marked `requires_db` and build their own
throwaway one. Nothing in the suite calls a model or a source system: if a
change needs a network to be tested, the change is in the wrong place.

---

## The gates

CI runs these and they block merge. They are not negotiable, and a pull request
that weakens one to go green will be asked to change the code instead.

| Gate | What it is |
|---|---|
| `mypy --strict` | Whole repo, no exceptions. Pydantic at every boundary. |
| `ruff check` + `ruff format` | No unformatted or unlinted code. |
| `pytest` with coverage floor | The floor goes up, never down. |
| Role-grant leak test | Every table, asserted per role. A new table with no entry fails. |
| Permission property test | 10,000 random ACL worlds against a model of the spec. |
| Compose smoke test | `docker compose up` reaches a green healthcheck. |

**Raising the coverage floor is welcome. Lowering it needs a maintainer's
explicit sign-off in the pull request**, and "this change is hard to test" is
not a reason — it is usually a description of the change.

---

## The rules a pull request cannot break

These are in [CLAUDE.md](CLAUDE.md) and they are not style preferences. Each one
exists because breaking it produces a failure nobody notices.

1. **All chunk retrieval goes through `visible_chunks()`.** The `hippo_agent`
   role holds EXECUTE on that function and SELECT on nothing. Do not add a
   second read path, widen a role, or query chunks directly — not in a test,
   not in a script, not to debug. If a change seems to need it, say so in the
   issue rather than doing it.
2. **The agent proposes; it never executes.** Actions are `pending` rows. Only
   the sync worker holds source-system credentials.
3. **Inverse before execution.** No inverse capture means the action fails. It
   does not mean it executes without a rollback path.
4. **`raw_records` are immutable.** The resolver reads them and writes graph
   tables. Fixing resolution means re-running the resolver.
5. **Provenance always.** Model-inferred entities and edges carry
   `provenance='model'` and `confidence < 1.0`.
6. **Synced content is data, never instructions.** Retrieved chunks are
   delimited in prompts; a proposal routes through approval regardless of what
   any content says.

---

## The connector review bar

Most of these are checked by `hippo-conformance`. The ones that are not are the
ones that get a connector rejected.

**The ACL grain, first and above everything else.** Grant on containers, not on
objects, and declare each object's container. A connector that grants per object
is correct and unusably slow; a connector that grants too widely is a leak, and
it will not announce itself. There must be a test showing a user without access
to a container retrieves nothing from it.

Then:

- `hippo-conformance` passes, and the conformance test is in your suite.
- Fixtures are committed and the tests run offline. **No token in CI, ever.**
- The cursor resumes exactly, at more than one page size.
- Write-back, if any, captures an inverse and has a rollback test.
- Payloads are stored verbatim. Unknown fields are logged, never dropped.
- No credential in `connectors.config`, in a fixture, or in a log line.
- Errors use the SDK taxonomy: rate limits reschedule, 5xx retries, 4xx
  dead-letters.

---

## What gets rejected, and why

**A second read path to content.** However convenient. See rule 1.

**A guard that recognises attacks.** A list of hostile phrases stops the attacks
already on the list and nothing else, and it reads as protection while providing
none. Defend structurally: make the bad thing unrepresentable.

**A new infrastructure dependency.** Kafka, NATS, Temporal, Neo4j, Qdrant,
OpenSearch, Redis, Celery, gRPC, GraphQL. Each is a fine tool. Together they are
a platform team's stack for a project whose whole premise is that the adopter
does not need a platform team. The Postgres-native equivalent, or a conversation
first. Every layer in [docs/STACK.md](docs/STACK.md) has a *measured*
graduation trigger; bring the measurement.

**A benchmark number with no method.** If a change improves retrieval, show it
on `python -m evals.report`, run as a controlled pair. See
[docs/BENCHMARK.md](docs/BENCHMARK.md) for why: an uncontrolled comparison once
showed a +0.033 improvement that was entirely the HNSW index reordering.

**A test edited to make it pass.** If a test asserted a literal list and your
change adds to that list, ask whether the test should assert the *property*
instead. A test routinely edited to go green has stopped being a test.

---

## What a good pull request looks like

One fragment or one fix. Not two.

The description says what was broken and how you know it is fixed. "Adds
retries" is less useful than "a 429 during the content stream dead-lettered the
job instead of rescheduling; the new test fails without the change".

Comments explain *why*, not *what*. The code says what it does. A comment
earns its place by recording the thing the next reader would otherwise have to
rediscover: the alternative that was tried, the constraint that forced this
shape, the failure mode this prevents.

Tests are named after the behaviour they pin, and each one fails for exactly one
reason. A test that would still pass with the feature removed is not a test.

---

## Reporting a security issue

**Do not open a public issue.** See [SECURITY.md](SECURITY.md) for the
disclosure process and what is in scope.

A permission bug — anything where one person's query can reach another person's
content — is the highest-severity class in this project and will be treated as
release-blocking regardless of how contrived the path.

---

## Maintainers

Hippo uses a two-tier model, kept deliberately small.

**Committers** review and merge in one area: a connector, the UI, the resolver.
Earned by three merged non-trivial pull requests in that area and a
maintainer's nomination. Committers cannot merge changes to the permission
filter, the role grants, or the migration sequence.

**Maintainers** review anything, and any change to the permission filter, the
grant matrix or `core/migrations/` needs a maintainer's approval regardless of
who wrote it — including another maintainer's. Two people, not one, on anything
touching retrieval or ACLs.

Becoming a maintainer is a conversation, not a threshold. It requires having
demonstrated the specific judgement this project runs on: knowing which failures
are silent, and being unwilling to ship one.

Inactive for six months moves you to emeritus. It is not a demotion and it is
reversible by asking.

---

## The roadmap process

Work is organised as fragments in [docs/PROJECT.md](docs/PROJECT.md), each with
a done-condition — a sentence that is either true or false about a running
system. A fragment is not done because the code exists; it is done because its
condition passes.

To propose one: open an issue with the phase it belongs to, a done-condition
somebody else could verify, and what it makes possible that is not possible now.

Fragments are not a queue anyone can jump. Phase order exists because each phase
proves something the next one depends on, and Phase 3's whole purpose is
measured by connectors the maintainers did not write.

Disagreement about direction goes in an issue, in public, before the code. A
pull request is a bad place to have an architecture argument, because by then
somebody has already done the work.

---

## Licence

Apache 2.0. By contributing you agree your contribution is licensed under it.
No CLA — the licence is the agreement.
