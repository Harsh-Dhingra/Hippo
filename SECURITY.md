# Security policy

Hippo answers questions using a company's private systems and can be asked to
write back to them. A bug in the wrong place does not crash — it answers,
confidently, with something the person asking was never allowed to read. We
would much rather hear about it from you.

## Reporting a vulnerability

Use GitHub's **Report a vulnerability** button on the Security tab, which opens
a private advisory. Please do not open a public issue for anything that looks
like a real vulnerability.

Include what you did, what happened, and what you expected. A failing test is
the most useful thing you can send; the permission suites in `tests/` are a
reasonable place to add one.

**What to expect.** An acknowledgement within 3 working days and an assessment
within 10. If we agree it is a vulnerability we will tell you our planned fix
and timing, and we will credit you in the advisory unless you would rather we
did not. If we disagree we will explain why, and you are free to disagree back
in public.

This is a small project. Those are commitments about responsiveness, not a
promise of a 24-hour turnaround, and we would rather state something we can
keep.

## Especially interesting

Anything in these categories is worth reporting even if you are not sure:

- **A second read path to content.** The agent's database role holds `EXECUTE`
  on `visible_chunks()` and `SELECT` on nothing. Any way to get chunk text out
  of the system that does not go through that function is the bug this project
  most wants to know about.
- **Answers containing something the asker cannot see.** Including partially,
  including in a summary, including in an error message.
- **An action that executes without a human**, or that executes with no
  captured inverse, or that targets something the requester could not see.
- **Anything a person can write into Slack or Jira that changes what Hippo
  does**, rather than what Hippo reports. `tests/fixtures/injection/corpus.json`
  has the shapes we have thought of; a new shape is a good report.
- **A credential anywhere it should not be** — the database, a log line, a
  trace, an error, `connectors.config`.
- **Session handling**: fixation, cookie scope, tokens reaching JavaScript.

## Not vulnerabilities

Being clear about these saves everyone time. See
[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md) for the reasoning.

- **The model believing something false it read in a synced source.** Hippo
  does not verify that your Slack messages are true. It stops content from
  being *acted* on and shows you what an answer was built from.
- **A user reading, through Hippo, something they can already read in Slack or
  Jira.** That is the product working.
- **The requester approving their own action.** Deliberate in v0 and documented;
  four-eyes approval is a policy question for P2-GOV-1.
- **An operator with database superuser access seeing everything.** The role
  separation is between the services, not against the person running the
  cluster.
- Findings from an automated scanner with no demonstrated impact, missing
  hardening headers with no exploit path, or reports about dependencies that
  are not reachable from any code path.

## Scope

This repository. If you are testing against someone else's deployment, get
their permission first; we cannot give it to you.

## Supported versions

Pre-alpha: only `main` is supported, and there are no released versions to
backport to yet. This section will get more specific when there is something to
be specific about.
