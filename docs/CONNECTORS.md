# Writing a connector

**Status:** P3-SDK-2. Against SDK 1.0.

A connector answers four questions about a source system:

| Stream | Question | Becomes |
|---|---|---|
| `identities` | who exists | principals |
| `content` | what was said | raw_records |
| `acls` | who can see what | ACL grants |
| `writeback` | how to act on it | executed actions |

That is the whole contract. If you can answer those four for a system, you can
connect it, and nothing in Hippo has to change for you to do it.

---

## Start from something that runs

```
pip install hippo
hippo-new-connector notion --out ~/src
cd ~/src/hippo-notion
pip install -e . && pytest
```

The generated package passes the conformance suite before you have changed a
line. That is deliberate: the two parts of a connector that are easy to get
wrong are the cursor and the ACL grain, and both fail silently. Starting from
something correct means the first time you break one, a test tells you.

Then replace `FixtureTransport` with a real HTTP client. Nothing above it
changes.

---

## The three rules

**A connector never touches the database.** It yields records; the sync runtime
persists them. That is what lets your connector be tested with no Postgres and
no network, and it keeps every permission-relevant write in one place instead of
in each contributor's code.

**A connector never sees an internal id.** You speak in source-system terms —
a Slack user id, a Jira issue key — because that is the only identifier you can
possibly know. The runtime resolves them.

**Payloads are verbatim.** Carry the source object unmodified. A field the SDK
has never heard of is stored rather than dropped, so schema drift costs a log
line and never a record.

---

## The cursor, which is the hard part

Each stream takes a cursor and yields pages. Three promises:

* `stream({})` yields every record you can see.
* `stream(page.cursor)` yields **exactly** what follows that page.
* The last page has `has_more=False`, and its cursor is what gets stored.

The middle one is where connectors go wrong. Yield too little and the records
between the page you crashed on and the page you resume at are lost forever,
and nothing will tell you — no error, no gap in a count, just an answer that
quietly does not mention something. Re-delivering a record is fine: the runtime
upserts on `(connector, source_type, source_id)`. Omitting one is never fine.

The cursor is yours. It is stored verbatim as jsonb and the runtime never
interprets it. Slack uses a message ts, Jira an updated-since plus a page token.

### Two kinds of stream

A **watermark** stream resumes and picks up what is new — "everything updated
since T". Store T. Never set `DONE`.

A **listing** stream pages through everything and, at the end, has nowhere
further to go: Slack has no incremental `users.list`. Its final cursor cannot
mean "resume here" and must not mean "read it all again", so it means finished:

```python
from sync.connectors.sdk import DONE, is_terminal


def identities(self, cursor):
    if is_terminal(cursor):
        yield Page(records=(), cursor=dict(cursor))  # one empty final page
        return
    ...
    yield Page(records=batch, cursor={DONE: True}, has_more=False)
```

The runtime starts the next pass from an empty cursor.

---

## ACLs, which is the part that matters most

**Grant on containers, not on objects.** Source systems share a channel, not
each of its ten thousand messages. Emit one `AclRecord` per container and
declare each object's container on its `ContentRecord`:

```python
ContentRecord(
    source_type="notion.page",
    source_id=page["id"],
    payload=dict(page),
    container=SourceRef(source_type="notion.space", source_id=page["space_id"]),
)
```

One grant per object is correct and unusably slow on a real workspace.

**There is no deny record.** Deny-by-default is the runtime's rule, so a grant
you stop emitting stops existing. That is what makes revocation a plain diff and
lets it propagate within minutes rather than at the next full sync.

**Get this right before you get anything else right.** Every other bug in a
connector produces a worse answer. This one produces an answer someone was not
allowed to see.

---

## Declaring what you can do

```python
Capabilities(
    kind="notion",
    schema_version="2026-01-01",  # the *source's* shape, not your code's
    actions=ACTIONS,  # () for a read-only connector
)
```

Declared, not discovered. The runtime decides whether to route an action to you
from this and nothing else, so declaring an action you cannot perform means an
approved action fails at execution — after a person has read it and clicked
approve.

Action payload models must set `extra="forbid"`. It is what stops a proposal
carrying a field you would pass through to the source system unexamined, and the
conformance suite checks it.

Put your action definitions in their own module with no transport import: the
sync worker needs them to execute and the agent needs them to know what may be
proposed, and the agent must never import a module that can hold a credential.

---

## Write-back: inverse before execution

```python
def capture_inverse(self, request) -> dict:
    """Read the target's current state, before changing it."""
```

If it cannot be read, raise `InverseCaptureError`. Do not return an empty
inverse to get past the check — an action with no rollback path must fail
rather than execute unrecoverably. The interface has three methods rather than
one so that this is expressed by its shape: there is no way to execute without
an earlier call that returned the inverse.

Capture enough to reconstruct the target, not a diff.

---

## Errors

| Raise | When | The runtime |
|---|---|---|
| `RateLimitedError(retry_after=...)` | the source asks you to slow down | reschedules, never spins |
| `TransientSourceError` | a 5xx, a timeout, a dropped connection | retries with backoff |
| `PermanentSourceError` | bad credentials, deleted resource, a 4xx | dead-letters immediately |
| `InverseCaptureError` | the write-back target's state could not be read | fails the action |

Never crash the worker. Never swallow an error silently — dead-letter over a
silent drop.

---

## Credentials

They come from the environment, never from `connectors.config` and never from
the repository:

```
HIPPO_NOTION_TOKEN                     # one connector of this kind
HIPPO_NOTION_TOKEN_<CONNECTOR_UUID>    # several, with different tokens
```

Your plugin's `build(config, token)` receives the config row and the resolved
token. It never holds one itself, which is what makes the registry safe to read
from the API process — which must never hold a source-system credential.

---

## Checking your work

```
hippo-conformance hippo_notion.connector:NotionConnector
hippo-conformance --list
```

Point it at a connector, a class, or a zero-argument factory. Exit 0 conforms,
1 does not, 2 could not resolve the argument.

**Run it against fixtures, never a live source.** Determinism and resume are
checked by syncing the same source repeatedly, so anything changing underneath
reports violations that are not there. This is also what makes it usable in a
pull request.

Every check corresponds to a way a connector silently loses or duplicates data:

* the cursor resumes exactly, at several page sizes
* two full syncs of an unchanged source produce the same records
* a single pass yields no duplicates
* `has_more=False` really is the last page
* cursors are JSON-serialisable
* ACL targets name containers that the content stream declares
* capabilities match the object, and a declared action can be performed
* inverse capture is required before execution

---

## Shipping it

```toml
[project.entry-points."hippo.connectors"]
notion = "hippo_notion:PLUGIN"
```

Install alongside Hippo and it is discovered. The built-in Slack and Jira
connectors register through this same group — no private path — so the mechanism
your connector depends on is the one Hippo exercises every time it starts.

---

## Before you open a pull request against Hippo itself

Most connectors should stay in their own repository; that is what the entry
point is for. If you are proposing one for this repository:

1. `hippo-conformance` passes, and the conformance test is in your test suite.
2. Fixtures are committed and the tests run offline. No token in CI, ever.
3. ACLs grant on containers, and there is a test proving a user without access
   to a container retrieves nothing from it.
4. Write-back, if any, captures an inverse and has a rollback test.
5. No credential in `connectors.config`, in a fixture, or in a log line.

The review bar is in `CONTRIBUTING.md`. The one that gets a connector rejected
regardless of everything else is the ACL grain, because the failure is silent
and the consequence is somebody reading something they should not.
