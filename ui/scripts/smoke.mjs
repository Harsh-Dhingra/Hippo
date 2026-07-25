/**
 * Drives the §12 demo through the UI, the way a mouse would.
 *
 * Every request here is one a browser makes: a form post to /api/session, page
 * loads, and form posts to the approve and rollback buttons. Nothing calls the
 * Hippo API directly, because the point is to check that the UI reaches it —
 * with the session cookie the browser holds and cannot read.
 *
 * Run against an already-seeded database and a running API and UI:
 *
 *   node scripts/smoke.mjs http://localhost:3100 alice@example.com <password>
 *
 * scripts/demo.sh sets all of that up and then calls this.
 */

const [, , base, email, password] = process.argv;
if (!base || !email || !password) {
  console.error("usage: node scripts/smoke.mjs <ui-url> <email> <password>");
  process.exit(2);
}

let cookie = "";
const checks = [];

function check(name, condition, detail = "") {
  checks.push({ name, ok: Boolean(condition), detail });
  console.log(`${condition ? "ok  " : "FAIL"}  ${name}${detail ? `  — ${detail}` : ""}`);
}

async function visit(path, init = {}) {
  const response = await fetch(`${base}${path}`, {
    ...init,
    headers: { ...(cookie ? { cookie } : {}), ...init.headers },
    redirect: "manual",
  });
  const setCookie = response.headers.get("set-cookie");
  if (setCookie) {
    const pair = setCookie.split(";")[0];
    // A delete arrives as an empty value; drop the cookie rather than sending
    // an empty one, so signing out is observable.
    cookie = pair.endsWith("=") ? "" : pair;
  }
  return response;
}

async function page(path) {
  const response = await visit(path);
  return { status: response.status, html: await response.text() };
}

// -- sign in ---------------------------------------------------------------

const form = new URLSearchParams({ email, password });
const login = await visit("/api/session", {
  method: "POST",
  body: form,
  headers: { "content-type": "application/x-www-form-urlencoded" },
});
check("signing in redirects to /ask", login.headers.get("location")?.endsWith("/ask"));
check("the session cookie is set", cookie.startsWith("hippo_session="));
check(
  "and the browser cannot read it",
  (login.headers.get("set-cookie") ?? "").toLowerCase().includes("httponly"),
);

// -- the pages a demo walks through ---------------------------------------

const ask = await page("/ask");
check("the ask page renders", ask.status === 200 && ask.html.includes("Ask"));

const actions = await page("/actions");
check("the actions page renders", actions.status === 200);

// The proposal seeded by demo.sh, waiting for a human.
const pending = actions.html.match(/\/api\/actions\/([0-9a-f-]{36})\/approve/);
check("a pending action is offered for approval", Boolean(pending), pending?.[1] ?? "none found");

if (pending) {
  const id = pending[1];
  const approved = await visit(`/api/actions/${id}/approve`, { method: "POST" });
  check("approving redirects back to the list", approved.status === 303);

  const after = await page("/actions");
  check("the action is now approved", after.html.includes("approved"));

  // The write itself belongs to the sync worker, which holds the credentials.
  // The UI cannot do it and should not be able to, so the demo runs it here —
  // the same separation the deployment has.
  if (process.env.HIPPO_DEMO_EXECUTOR) {
    const { execSync } = await import("node:child_process");
    execSync(process.env.HIPPO_DEMO_EXECUTOR, { stdio: "inherit" });
  }

  const executed = await page("/actions");
  check("the action shows as executed", executed.html.includes("executed"));
  if (executed.html.includes("/rollback")) {
    const rolledBack = await visit(`/api/actions/${id}/rollback`, { method: "POST" });
    check("rollback is accepted", rolledBack.status === 303);

    const queued = await page("/actions");
    check(
      "and is queued rather than claimed done",
      queued.html.includes("executed"),
      "the status moves when the undo has actually happened",
    );
  }
}

const traces = await page("/traces");
check("the traces page renders", traces.status === 200);

const traceLink = traces.html.match(/\/traces\/([0-9a-f-]{36})/);
if (traceLink) {
  const trace = await page(`/traces/${traceLink[1]}`);
  check("a trace shows its steps", trace.html.includes("Steps"));
  check("and what was retrieved", trace.html.includes("Retrieved"));
  check("and the prompt that left the machine", trace.html.includes("System prompt"));
}

// -- signing out -----------------------------------------------------------

await visit("/api/session/logout", { method: "POST" });
const locked = await visit("/ask");
check(
  "signing out ends the session",
  locked.status === 307 || locked.status === 303 || locked.status === 302,
  `got ${locked.status}`,
);

const failed = checks.filter((entry) => !entry.ok);
console.log(`\n${checks.length - failed.length}/${checks.length} checks passed`);
process.exit(failed.length === 0 ? 0 : 1);
