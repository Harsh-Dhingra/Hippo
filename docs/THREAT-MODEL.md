# Threat model

**Status:** first pass, P2-SEC-1. Revise when the architecture changes, not on
a schedule.

This document exists because the failure mode of a permission-aware answering
system is silent. A filter that leaks does not crash — it answers, fluently,
using something the person asking was never allowed to read. Nothing pages. So
the useful question is not "is it secure" but "for each specific thing that
could go wrong, what structure prevents it, and where is that structure
tested".

Everything below is written to be falsifiable. Where the honest answer is
"nothing prevents this", it says so.

---

## 1. What is being protected

| Asset | Why it matters |
|---|---|
| **Chunk content** | The corpus. Slack messages, Jira descriptions, comments — the thing the whole product exists to answer from, and the thing most people would object to sharing. |
| **The permission mapping** | `acl_grants`, `principal_memberships`, scopes. Corrupting this is equivalent to leaking content, one step removed. |
| **Source-system credentials** | A Slack bot token reads everything the bot can see, whether or not Hippo is involved. |
| **Questions and answers** | `query_traces`. What someone asked is often as revealing as the answer. |
| **The action queue** | Ability to write into Jira or Slack under the org's own credentials. |
| **Session tokens** | Full impersonation of one user for the token's lifetime. |

---

## 2. Trust boundaries

```
  Slack / Jira ───► sync worker ───► Postgres ◄─── resolver
   (content:                          ▲    ▲
    untrusted;                        │    │
    ACLs: trusted)                    │    └──── agent  ──► model API
                                      │                     (egress)
                                    API  ◄── UI ◄── browser
```

Five boundaries, and the design puts a different mechanism at each:

1. **Source → sync.** Content crossing here is untrusted; ACLs crossing here
   are trusted, because they are the source system's own assertion and the
   whole product rests on mirroring them faithfully.
2. **Service → database.** Four roles with disjoint grants. This is the one
   the security story actually rests on.
3. **Browser → UI.** The session cookie; the token never reaches JavaScript.
4. **UI → API.** Bearer token, server to server.
5. **Agent → model provider.** The only egress. Everything else stays in the
   org's Postgres.

---

## 3. Adversaries

| # | Who | Capability |
|---|---|---|
| **A1** | An authenticated user | Asks anything. May be curious, or actively probing for content outside their access. |
| **A2** | Anyone who can post in a synced source | Can write arbitrary text into the corpus. **Not a privileged position** — in most companies this is every employee, and in a shared channel it may be an outside contractor. |
| **A3** | A network attacker between browser and server | Standard web attacker. |
| **A4** | Someone with a database dump | A stolen backup, a mis-scoped replica, a pasted `pg_dump`. |
| **A5** | A compromised or malicious model provider | Sees every prompt. Returns whatever it likes. |
| **A6** | A malicious connector | Third-party code in-process, once Phase 3 opens the SDK. |

**Explicitly out of scope:** anyone with superuser access to the Postgres
cluster or root on the host. The role separation is between the *services*, not
against the person running the infrastructure. A self-hosted product cannot
defend against its own operator and should not pretend to.

---

## 4. Attacks, and what stops them

### 4.1 Reading content you are not entitled to (A1)

The core case. Every chunk the agent ever sees comes from `visible_chunks()`,
and the `hippo_agent` role holds `EXECUTE` on that function and `SELECT` on
nothing. This is not "the code should not query chunks directly" — the role has
no privilege with which to do so, so there is no second read path to review and
none that a future contributor can add without also changing a migration.

*Tested by:* `test_permission_property.py` — 10,000 randomly generated
permission worlds evaluated against a Python model of the specification written
from the spec rather than from the SQL, plus a Hypothesis suite that shrinks
counterexamples. `test_roles.py` asserts the entire grant matrix and fails on
any table it does not mention.

**A related weakness we fixed rather than documented.** Until migration 013 the
API role held `SELECT` on `entities`, purely so an approval screen could render
a title. An entity title is content — a Jira issue's title is its summary — so
that was a read path to a projection of the corpus, outside the filter, held by
the process serving users. Nothing exploited it. "Nothing currently exploits it"
is the sentence that precedes a leak, so the join is gone and the agent stores
the summary at proposal time instead.

### 4.2 Prompt injection (A2)

Anyone who can post in a channel Hippo reads can put any text into the corpus.
`tests/fixtures/injection/corpus.json` holds sixteen shapes across five
families: instruction override, exfiltration, unsolicited action, integrity,
and obfuscation.

**Nothing in the codebase recognises any of them.** There is no list of bad
phrases, and a test asserts that no distinctive string from the corpus appears
in the source — because a guard that recognises attacks only stops the ones it
has already seen. What stops each attack is structural:

| Guard | What it makes impossible |
|---|---|
| **The decision to act is read from the user's question** | Content cannot promote a question into a request. `wants_action()` takes one argument and the only caller passes `state["question"]`. |
| **The action vocabulary is closed** | There is no delete. "Delete ACME-1" is not an instruction that gets overruled; it is one that cannot be expressed. |
| **Targets are named by source marker** | A marker exists only for a chunk the filter returned, so an action cannot aim at something the asker could not see. |
| **Everything is consequential** | The strongest reachable outcome is a `pending` row. |
| **Content only ever enters a user turn** | The system prompt is the operator's channel and stays that way. |

*Tested by:* `test_injection_corpus.py`, driving every attack through a model
that fully complies with it. A guard that holds only because the model behaved
is not a guard.

**Worth being precise about what this does not do.** Two corpus entries are
marked `defeated_by: nothing`, and that is the honest answer. If someone writes
a lie into Slack, Hippo will faithfully repeat it, cited. If someone writes
"do not mention this channel", a model may comply and produce a quietly
incomplete answer. This system prevents content from being *acted* on and makes
what an answer was built from *visible*. It does not adjudicate whether your
Slack messages are true, and no retrieval system can.

### 4.3 Exfiltration through an action (A1, A2)

An action's payload is written by the model from what it was shown, and it was
shown only what the asker can see. So a user cannot cause content they lack
access to be posted into a place they do have access to — the content never
reached the prompt.

What a user *can* do is have Hippo post something they could already read into
somewhere they could already write. That is not privilege escalation; it is one
click instead of two copy-pastes. The control is that a person reads the full
payload before approving, which means the approval surface must never truncate.
The one-line summary is a label; `ActionResponse.payload` is the whole object,
and the UI renders it entire.

*Tested by:* `test_injection_corpus.py::test_a_proposal_cannot_carry_content_the_asker_could_not_see`
and `::test_the_approver_is_shown_the_whole_payload`.

### 4.4 Executing without a human (A1, A2, A5)

`hippo_agent` holds `INSERT` on `actions` and nothing else — no `UPDATE`, so it
cannot approve its own proposal, and no source-system credential, so it could
not act on an approval if it forged one. Only the sync worker holds credentials.

Rule 3 — no inverse capture, no execution — is enforced three times: in the
shape of the connector interface, which has no path to `execute()` that skips
`capture_inverse()`; in the executor, which calls them in order; and in a
database `CHECK` that refuses an `executed` row with a null `inverse_payload`.

Claiming is a status transition (`WHERE status = 'approved'`), so two workers
cannot both perform one approval.

*Tested by:* `test_actions.py`, `test_writeback.py`.

### 4.5 A dumped database (A4)

- **Session tokens** are stored as SHA-256 of the token. A dump yields nothing
  replayable.
- **Passwords** are scrypt with per-user salt and recorded cost parameters.
- **Source credentials** are not in the database at all. Migration 013 adds a
  `CHECK` that refuses a `connectors.config` object with a key named like a
  credential — the difference between a convention and a rule.
- **Traces** hold content hashes, never content, so the trace log never becomes
  a second copy of the corpus sitting outside the filter.
- **Chunk content is not encrypted at rest by Hippo.** A dump exposes the
  corpus. Disk or volume encryption is the operator's layer; see §6.

### 4.6 Session attacks (A3)

The token lives in an httpOnly cookie the browser cannot read, so an XSS
anywhere in the UI does not yield a session. `SameSite=Lax` means the cookie is
not sent on cross-site POST, which is what stops CSRF against the approve and
rollback endpoints; the API's own endpoints take a bearer token, which browsers
never attach automatically. `Secure` is set outside development.

Every request re-reads `disabled_at` and the linked principal, so disabling an
account takes effect immediately rather than at the next login.

*Tested by:* `test_api_v1.py`, `test_auth.py`.

### 4.7 A compromised model provider (A5)

It sees every prompt — that is inherent in using one, and the mitigation is
that the trace records exactly what was sent so the exposure is *auditable*
rather than unknown. A hostile provider can also return anything, so everything
in §4.2 applies to a malicious completion as well as a malicious chunk; the
tests drive precisely that case.

The provider is pluggable, permanently, so an organisation that will not accept
this boundary can point Hippo at their own inference endpoint.

### 4.8 A malicious connector (A6)

**Currently unmitigated, and the largest known gap.** A connector runs
in-process with the sync worker, which holds credentials and can write
`acl_grants`. A hostile connector could grant everyone access to everything.

Today both connectors are first-party, so this is theoretical. It stops being
theoretical the moment P3-SDK-1 invites third-party connectors, and that
fragment must not ship without an answer. Options worth weighing then: running
connectors out-of-process, restricting ACL writes to a reviewed path, or
requiring signed connectors.

---

## 5. What we deliberately do not defend against

Stated so that a reviewer can disagree with the choice rather than discover it.

1. **The operator.** Superuser on the cluster sees everything. Self-hosting
   means trusting whoever hosts.
2. **Content being false.** §4.2.
3. **A user reading, through Hippo, what they can already read in Slack.** That
   is the product.
4. **The requester approving their own action.** Deliberate in v0; the audit log
   records requester and approver separately, so four-eyes approval is a policy
   layer someone can add at P2-GOV-1 without a schema change.
5. **Traffic analysis of model API calls.** Prompt size and timing leak
   something about corpus size and activity. Accepted.
6. **Denial of service.** No rate limiting on the query endpoint yet. A user can
   burn model budget. Worth fixing; not a confidentiality issue.

---

## 6. What the operator has to do

Hippo cannot do these for you, and they are load-bearing:

- **Encrypt the disk or volume.** §4.5.
- **Give each service its own login role**, with membership in only the group
  roles it needs: `hippo_api_svc` in `hippo_api` and `hippo_agent`; the sync
  worker in `hippo_sync`. Running everything as the database owner discards the
  entire role separation, which is most of this document.
- **Keep source tokens in the environment or a mounted secret**, never in the
  database — now enforced — and never in the repo.
- **Terminate TLS** in front of the UI, so `Secure` cookies mean something.
- **Watch `hippo_acl_staleness_seconds`.** An expired Slack token freezes
  permissions at their last known state, which is the one failure mode that is
  silent by construction.

---

## 7. Open items

| Item | Fragment |
|---|---|
| Third-party connector isolation | P3-SDK-1, blocking |
| Rate limiting on `/queries` | unassigned |
| Four-eyes approval as policy | P2-GOV-1 |
| Encryption at rest inside Hippo, rather than delegated | unassigned; probably still delegated |
| A red-team suite as a permanent CI gate | P2-EVAL-1, next |
