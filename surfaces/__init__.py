"""Ways to reach Hippo that are not the REST API.

ARCHITECTURE section 2 puts auth, sessions and approval buttons in the API and
no business logic. A surface is one step further out: a protocol adapter with
none at all.

They are not all shaped the same. The MCP surface is a client — it runs on a
developer's machine, holds no database credential, and reaches content only
through a bearer token. The Slack surface is mounted in the API process,
because a webhook has to be received somewhere and mapping a Slack user to a
principal needs the database.

What every surface shares is the part that matters:

* **It adds no read path.** A query runs under SET LOCAL ROLE hippo_agent
  through visible_chunks(), exactly as the REST route does.
* **It always acts as one named principal.** There is no surface-level account,
  no workspace default and no shared token. Somebody unlinked is told they are
  unlinked, which is a true statement about their access rather than an empty
  answer that reads like one.
* **It cannot widen anything**, because the permission story lives in the
  database and in one SQL function, and a surface has nothing to widen it with.
"""
