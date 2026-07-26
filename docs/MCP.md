# Hippo in your coding agent

**Status:** P3-SRF-2. Against MCP, so this works with Claude Code, Codex and
anything else that speaks the protocol.

Your coding agent can already read the code. What it cannot read is why the
code is like that — the Slack thread where the decision was made, the ticket
that explains the constraint, the PR where somebody said "we tried that and it
broke billing". Hippo has all three, filtered to what you personally can see.

```
hippo_search("why does the renewal flow retry three times")
```

---

## Setting it up

You need a Hippo install and a token of your own.

**1. Get a token.** Log in to Hippo and copy the session token. It identifies
you, and every query the agent makes runs as you.

**2. Add the server.** For Claude Code:

```json
{
  "mcpServers": {
    "hippo": {
      "command": "hippo-mcp",
      "env": {
        "HIPPO_URL": "https://hippo.your-company.internal",
        "HIPPO_TOKEN": "your-token"
      }
    }
  }
}
```

Other MCP clients take the same three things in their own format: a command,
a URL and a token.

**3. Check it.** Ask your agent something only your company knows. If the token
is wrong you get a message saying so, rather than an empty answer.

---

## One token per person, and why it matters

**Do not configure a shared team token.** The server refuses to start without a
token and has no default, but nothing can stop somebody pasting the same one
into ten configs — so this is the part worth understanding.

Hippo answers "what can *you* see". The token is how it knows who you are. A
shared token makes every developer's queries run as whoever it belongs to, and
the failure is silent: the answers keep coming, fluently and with citations,
built from things the person asking was never entitled to read. Nothing errors.
Nothing logs a warning that means anything.

If you want a token that is not a person, the answer is that there isn't one.
That is the design.

---

## What it can do

| Tool | What it is for |
|---|---|
| `hippo_search` | Raw passages, for your agent to reason over |
| `hippo_ask` | A cited answer, synthesised by Hippo |
| `hippo_timeline` | What happened around one thing, in order |
| `hippo_skills` / `hippo_run_skill` | The saved questions your company has written |
| `hippo_actions` | What you have proposed, and what is waiting for you |
| `hippo_write_note` | Put a decision into memory deliberately |

`hippo_search` is usually the better one. It returns the source material and
lets your agent — which already has your code in context — do the reasoning.
`hippo_ask` runs Hippo's own model, which is a second model in the loop and a
second place the content has been.

---

## What it cannot do

**It cannot approve anything.** Hippo's write path is: propose a pending row,
a person reads it, a person approves, and only then does the sync worker
execute. If this surface exposed an `approve` tool, the same model that wrote a
proposal could accept it — and the separation the whole product rests on would
be a description of a code path rather than a guarantee.

So when your agent proposes a Jira comment, it will tell you it is pending and
that it cannot approve it. Go and look at it. That is the point.

**It cannot execute or roll back.** Those belong to the sync worker, which is
the only component holding your Slack and Jira credentials. A tool here would
put those credentials one call away from a model.

**It cannot see anything you cannot.** Not a policy — the permission filter runs
server-side in SQL, and this server holds no database credential at all. It is
an HTTP client with a bearer token.

---

## Two things to know before you roll it out

**Retrieved content is untrusted, and it is marked.** What comes back is Slack
messages and Jira descriptions that anyone in your company could have written,
and one of them may say "ignore your instructions". Every passage leaves here
fenced and labelled as data rather than as instructions, in the same shape
Hippo uses in its own prompts. Your agent's model still has to respect that;
this at least gives it the chance. See
[docs/THREAT-MODEL.md](THREAT-MODEL.md) §4.2.

**Your context leaves your infrastructure.** Hippo's pitch is that your memory
stays on hardware you control. This surface sends retrieved passages to
whichever model your coding agent uses, which may be somebody else's. That is
the trade you are making by using it, and it is worth saying out loud before a
security review says it for you.

If that is not acceptable, the Slack surface (P3-SRF-1) keeps everything inside
the boundary, and Hippo's own model provider is pluggable to a local endpoint.

---

## When something goes wrong

The server distinguishes three failures on purpose, because they need different
reactions:

| What you see | What it means |
|---|---|
| "Hippo rejected this token" | Expired or wrong. Sign in again, update `HIPPO_TOKEN`. |
| "not linked to a Slack, Jira or GitHub account yet" | You are authenticated and can see nothing. An administrator links your login after the next sync. |
| "could not reach Hippo at ..." | The server is down or the URL is wrong. Not a credential problem. |

The second one is deliberately not reported as an empty answer. "You have
access to nothing" and "nothing matched" are different facts, and using the
second to report the first is how a permission system stops being legible.
