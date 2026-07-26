## What was broken, and how you know it is fixed

<!-- "A 429 during the content stream dead-lettered the job instead of
rescheduling; the new test fails without the change." Not "adds retries". -->

## Fragment

<!-- The fragment id from docs/PROJECT.md, or "fix" / "docs". One per PR. -->

## Checklist

- [ ] `ruff check`, `ruff format`, `mypy --strict` and `pytest` all pass locally
- [ ] Coverage did not go down
- [ ] No new read path to chunks outside `visible_chunks()`
- [ ] No credential in the repo, in a fixture, in `connectors.config`, or in a log line
- [ ] Tests fail without the change

## If this touches retrieval, ACLs, roles, or migrations

- [ ] The permission property test passes
- [ ] The role-grant leak test covers any new table
- [ ] Two maintainer reviews requested

## If this changes retrieval quality

- [ ] `python -m evals.report` numbers, before and after, as a controlled pair
      (see docs/BENCHMARK.md for why "controlled" matters here)
