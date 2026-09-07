/* The dashboard.
 *
 * Reads the capability API and adds nothing of its own. Every value rendered
 * here arrived in an API response that had already gone through the run's
 * redaction sink -- this file does no formatting that could reconstruct a
 * masked value, and it has no access to anything except fetch().
 *
 * It also does not drive the browser. When a run pauses at an irreversible
 * step, a person performs that step in the live window on the machine running
 * the API; the buttons here only signal the paused run to continue or stop.
 * The UI says so where it matters, because the opposite assumption is easy to
 * make and dangerous.
 */
const $ = (sel, el = document) => el.querySelector(sel);
const view = $("#view");
let current = "chat";

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const api = async (path, opts) => {
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch { body = null; }
  return { status: r.status, body };
};

const pill = (status) =>
  `<span class="pill s-${esc(status)}">${esc(status)}</span>`;

const when = (ts) => (ts ? new Date(ts * 1000).toLocaleTimeString() : "—");

/* A definition list. Values are escaped HERE rather than by each caller.
 *
 * It used to interpolate the value raw and trust every call site to have run
 * esc() first -- which works until someone adds a row and forgets, and the
 * page renders app-controlled text as markup. A legacy console that echoes a
 * member's input back into an error message is exactly the source that would
 * exploit it. Callers that genuinely need markup (a link, a mono span) pass
 * `html(...)`, which is explicit and greppable.
 */
const html = (markup) => ({ __html: markup });
const kv = (pairs) =>
  `<dl class="grid">${pairs
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${v && v.__html ? v.__html : esc(v)}</dd>`)
    .join("")}</dl>`;


/* ------------------------------------------------------- naming & filters */

/* A short heading, derived from the capability id.
 *
 * The artifact's `name` is the recording goal, which reads as a sentence and
 * gets truncated mid-word in a heading. The id is already a name -- it just
 * needs spacing and casing.
 *
 * The leading token shared by EVERY id in the catalogue is dropped, so a set
 * that all begins the same way loses the prefix that carries no information.
 * Derived from the data rather than from a list of known words: this file is
 * not allowed to know anything about the target application, and a hardcoded
 * prefix would be exactly that knowledge smuggled in as presentation.
 */
function commonPrefixToken(ids) {
  if (ids.length < 2) return null;
  const heads = ids.map((id) => String(id).split("_")[0]);
  const first = heads[0];
  if (!first || !heads.every((h) => h === first)) return null;
  if (ids.some((id) => String(id).split("_").length < 2)) return null;
  return first;
}

/* One <option> for the application filter. A named helper rather than an
 * inline template so the markup is written once; every value it interpolates
 * still goes through esc(). */
function appOption(name, chosen, count) {
  const selected = name === chosen ? " selected" : "";
  return `<option value="${esc(name)}"${selected}>${esc(name)} (${esc(count)})</option>`;
}

function shortName(id, prefix) {
  let parts = String(id).split("_");
  if (prefix && parts[0] === prefix && parts.length > 1) parts = parts.slice(1);
  return parts
    .map((w) => (w ? w[0].toUpperCase() + w.slice(1) : w))
    .join(" ");
}

/* A field name as a label: share_balance -> "Share balance".
 *
 * A single-letter token belongs to the word after it rather than standing on
 * its own: `e_mail` is one field called email, and splitting it produced
 * "E mail", which reads as a typo in the one place a caller is being told
 * what to supply. */
const fieldLabel = (name) => {
  const parts = [];
  String(name).split("_").forEach((w) => {
    const last = parts.length - 1;
    if (last >= 0 && parts[last].length === 1) parts[last] += w;
    else parts.push(w);
  });
  const words = parts.join(" ");
  return words ? words[0].toUpperCase() + words.slice(1) : words;
};

/* View and filter live in the URL fragment so a reload keeps them. Written
 * with replaceState rather than by assigning location.hash, which would push
 * a history entry for every tab click. */
function readState() {
  const p = new URLSearchParams(String(location.hash || "").replace(/^#/, ""));
  return { view: p.get("view"), app: p.get("app") };
}

function writeState(patch) {
  const cur = readState();
  const next = { ...cur, ...patch };
  const p = new URLSearchParams();
  if (next.view) p.set("view", next.view);
  if (next.app) p.set("app", next.app);
  history.replaceState(null, "", "#" + p.toString());
}

/* ------------------------------------------------------- result rendering */

/* One block per classification, because they mean genuinely different things.
 *
 * The full response stays available underneath, collapsed. It is the thing a
 * reviewer needs when the summary is not enough, and removing it would trade
 * away the debuggability the result contract exists to provide.
 */
const details = (body) =>
  `<details class="raw"><summary>Show full response</summary>
     <pre>${esc(JSON.stringify(body, null, 2))}</pre></details>`;

const outputLines = (outputs) => {
  const entries = Object.entries(outputs || {});
  if (!entries.length) return "";
  return `<dl class="answer">${entries
    .map(([k, v]) => `<dt>${esc(fieldLabel(k))}</dt><dd>${esc(v)}</dd>`)
    .join("")}</dl>`;
};

const runLink = (id) =>
  id ? ` <a href="#" data-run="${esc(id)}">run ${esc(id)}</a>` : "";

const took = (ms) => (ms ? `<span class="muted"> · ${Math.round(ms)} ms</span>` : "");

function resultSummary(status, body) {
  const b = body || {};
  const cls = b.classification ?? b.status ?? "unknown";
  const result = b.result || b;
  const head = (label, extra = "") =>
    `<div class="verdict v-${esc(cls)}"><b>HTTP ${esc(status)}</b> &middot; ${label}${extra}</div>`;

  if (cls === "success") {
    return head("success", took(result.duration_ms) + runLink(b.run_id ?? result.run_id))
      + outputLines(result.outputs)
      + (Object.keys(result.outputs || {}).length
          ? "" : `<p class="muted">No declared outputs. This capability performs work
                  rather than answering a question.</p>`)
      + details(body);
  }

  if (cls === "business_outcome") {
    /* An ANSWER, and it must not read as a failure. Same visual weight as
     * success: the application was asked a question and gave one. The
     * classification in the payload stays `business_outcome` -- that is the
     * brief's vocabulary and the API contract does not change. */
    return head("Answered", runLink(b.run_id ?? result.run_id))
      + `<p class="answer-text">${esc(result.message ?? "")}</p>`
      + details(body);
  }

  if (cls === "escalation_required") {
    const step = result.blocked_step ?? b.blocked_step
      ?? (b.escalation || {}).step_id ?? "";
    const expected = ((b.escalation || {}).stopped || {}).expected
      ?? (b.escalation || {}).expected_on_resume ?? "";
    return head("waiting for a person")
      + `<p>Stopped${step ? ` at step <code>${esc(step)}</code>` : ""} because it is
         irreversible. The browser session is open and waiting. Perform the step
         there, then finish it under
         <a href="#" data-goto="interventions">Interventions</a>.</p>`
      + (expected ? `<p class="muted">On finishing it will check: ${esc(expected)}</p>` : "")
      + runLinkLine(b.run_id ?? result.run_id)
      + details(body);
  }

  if (cls === "not_performed" || cls === "expired") {
    return head(cls === "expired" ? "nobody came in time" : "not performed")
      + `<p>${esc(result.message ?? "")}</p>`
      + `<p class="muted">Nothing was committed. The page was re-checked rather
         than anyone being taken at their word.</p>`
      + details(body);
  }

  if (cls === "caller_error") {
    const violations = result.violations || [];
    return head("the arguments did not satisfy the contract")
      + (violations.length
          ? `<ul>${violations.map((v) =>
              `<li><code>${esc(v.input)}</code> &mdash; ${esc(v.message)}</li>`).join("")}</ul>`
          : `<p>${esc(result.message ?? "")}</p>`)
      + `<p class="muted">No browser was opened.</p>`
      + details(body);
  }

  if (cls === "auth_failure") {
    return head("system credentials problem, not the caller's")
      + `<p>${esc(result.message ?? "")}</p>`
      + `<p class="muted">The system's own credentials for this application need
         attention. Nothing about the request was wrong.</p>`
      + details(body);
  }

  const failure = result.failure || {};
  return head("failed", runLink(b.run_id ?? result.run_id))
    + kv([
        ["step", failure.step_id ?? ""],
        ["expected", failure.expected ?? ""],
        ["observed", failure.observed ?? ""],
      ])
    + (result.evidence?.screenshot
        ? `<p><a href="#" data-run="${esc(b.run_id ?? result.run_id ?? "")}">
             open the run to see the failure screenshot</a></p>` : "")
    + details(body);
}

const runLinkLine = (id) =>
  id ? `<p>${runLink(id)}</p>` : "";


/* ---------------------------------------------------------------- catalog */

async function renderCatalog() {
  const { body } = await api("/capabilities");
  const all = body?.capabilities ?? [];

  /* The filter list is derived from what the API returned, and so is the
   * default: the application with the most recorded capabilities. Neither is
   * hardcoded, because this page is not allowed to know which applications
   * exist -- that is the seam the capability API keeps it on the far side of.
   */
  const apps = [...new Set(all.map((x) => x.app).filter(Boolean))].sort();
  const tally = {};
  all.forEach((x) => { if (x.app) tally[x.app] = (tally[x.app] || 0) + 1; });
  const busiest = apps.slice().sort((a, b) => (tally[b] || 0) - (tally[a] || 0))[0];
  const wanted = readState().app || busiest || "";
  const chosen = wanted === "all" || apps.includes(wanted) ? wanted : (busiest || "all");
  writeState({ app: chosen });

  const caps = chosen === "all" ? all : all.filter((x) => x.app === chosen);
  const prefix = commonPrefixToken(all.map((x) => x.id));

  const filter = `<div class="card filter">
    <label for="app-filter">Application</label>
    <select id="app-filter">
      <option value="all"${chosen === "all" ? " selected" : ""}>All (${all.length})</option>
      ${apps.map((a) => appOption(a, chosen, tally[a])).join("")}
    </select>
    <span class="muted">Showing ${caps.length} of ${all.length}.</span>
  </div>`;

  view.innerHTML = filter + `<div class="cards">` + caps.map((c) => {
    if (c.status === "unloadable") {
      return `<div class="card"><h2>${esc(c.id)} <span class="tag">unloadable</span></h2>
        <pre>${esc(c.error)}</pre></div>`;
    }
    const inputs = (c.inputs ?? []).map((i) => `
      <label>${esc(i.name)}
        <span class="muted">${esc(i.type)}</span>
        ${i.required ? '<span class="req">*</span>' : ""}
        ${i.sensitivity !== "public" ? `<span class="tag">${esc(i.sensitivity)}</span>` : ""}
      </label>
      <input type="text" name="${esc(i.name)}"
             placeholder="${esc(i.example ?? (i.sensitivity !== "public" ? "no example recorded" : ""))}">
      <div class="muted" style="font-size:12px">${esc(i.description)}</div>`).join("");

    const outputs = (c.outputs ?? []).length
      ? `<h3>Returns</h3><ul class="mono">` +
        c.outputs.map((o) => `<li>${esc(o.name)} : ${esc(o.type)}${
          o.sensitivity !== "public" ? ` <span class="tag">${esc(o.sensitivity)}</span>` : ""
        }</li>`).join("") + `</ul>`
      : `<h3>Returns</h3><p class="muted">Nothing. This capability performs work rather than answering a question.</p>`;

    const outcomes = (c.outcomes ?? []).length
      ? `<h3>Known business outcomes</h3><ul>` +
        c.outcomes.map((o) => `<li><code class="mono">${esc(o.name)}</code> &mdash; ${esc(o.message)}</li>`).join("") +
        `</ul>`
      : "";

    const risky = c.risky_steps?.length
      ? `<div class="banner stop"><b>Contains an irreversible step</b> (${esc(c.risky_steps.join(", "))}).
         The run stops there, keeps the live session open and waits for a person.
         It appears under <b>Interventions</b> until someone finishes with it or the
         pause deadline passes.</div>`
      : "";

    return `<div class="card" data-cap="${esc(c.id)}" data-ver="${esc(c.version)}">
      <h2>${esc(shortName(c.id, prefix))}</h2>
      <div class="badges">
        <span class="tag ${c.status === "draft" ? "draft" : ""}">${
          c.status === "draft" ? "Draft" : esc(c.status)}</span>
        ${c.requires_human ? '<span class="tag">Contains an irreversible step</span>' : ""}
        ${c.required_role ? `<span class="tag role">Requires ${esc(c.required_role)}</span>` : ""}
        ${c.superseded_by ? `<span class="tag">Superseded by ${esc(c.superseded_by)}</span>` : ""}
      </div>
      <div class="muted mono">${esc(c.id)} @ ${esc(c.version)} &middot; ${esc(c.app)} / ${esc(c.tenant)}</div>
      <p>${esc(c.description)}</p>
      ${c.superseded_by ? `<div class="banner"><b>Superseded by ${esc(c.superseded_by)}.</b>
        Still invocable by pinning this version, which is what versions are for. It is
        not offered for selection by name &mdash; a caller asking for this capability
        gets the current contract.</div>` : ""}
      ${c.status === "draft" ? `<div class="banner"><b>Draft.</b> Recorded by discovery and
        not yet approved by a human. Its locators and its risk classification are a
        first guess for review.</div>` : ""}
      ${risky}
      ${c.required_role ? `<div class="banner"><b>Requires a ${esc(c.required_role)}.</b>
        This capability signs on with the ${esc(c.required_role)} credential set,
        because the application refuses the action to a lesser operator. An agent
        can read this from the catalogue before invoking rather than discovering
        it from a refusal.</div>` : ""}
      ${outputs}${outcomes}
      <h3>Invoke</h3>
      <form class="invoke">${inputs}
        <p><button class="act go" type="submit">Invoke</button></p>
      </form>
      <div class="result"></div>
    </div>`;
  }).join("") + `</div>` ||
    `<div class="card"><p class="muted">No capabilities recorded yet.</p></div>`;

  const picker = $("#app-filter", view);
  if (picker) picker.addEventListener("change", () => {
    writeState({ app: picker.value });
    renderCatalog();
  });

  view.querySelectorAll("form.invoke").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const card = form.closest(".card");
      const out = $(".result", card);
      const inputs = {};
      new FormData(form).forEach((v, k) => {
        if (String(v).length) inputs[k] = v;
      });
      out.innerHTML = `<p class="muted">Running&hellip; A browser window will open on the
        machine running the API.</p>`;
      const { status, body } = await api(
        `/capabilities/${encodeURIComponent(card.dataset.cap)}/${encodeURIComponent(card.dataset.ver)}/invoke`,
        { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ inputs }) });
      out.innerHTML = resultSummary(status, body);
      wireRunLinks(out);
      wireGoto(out);
      refreshPending();
    });
  });
}

/* ------------------------------------------------------------------- runs */

async function renderRuns() {
  const { body } = await api("/runs");
  const runs = body?.runs ?? [];
  view.innerHTML = `<div class="card"><h2>Run history</h2>
    <p class="muted">Every run this process has served. In memory only &mdash; it empties on restart.</p>
    <table><thead><tr><th>Run</th><th>Capability</th><th>Status</th><th>Started</th>
      <th>Duration</th><th>Inputs</th></tr></thead><tbody>
      ${runs.map((r) => `<tr>
        <td class="mono"><a href="#" data-run="${esc(r.run_id)}">${esc(r.run_id)}</a></td>
        <td>${esc(r.capability?.id ?? "")}</td>
        <td>${pill(r.status)}</td>
        <td>${when(r.started_at)}</td>
        <td>${r.duration_ms ? Math.round(r.duration_ms) + " ms" : "—"}</td>
        <td class="mono">${esc(JSON.stringify(r.inputs ?? {}))}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">No runs yet.</td></tr>`}
    </tbody></table></div>`;
  wireRunLinks(view);
}

/* Links that switch tab, so "finish it under Interventions" is one click
 * rather than an instruction the reader has to follow by hand. */
function wireGoto(root) {
  root.querySelectorAll("[data-goto]").forEach((a) =>
    a.addEventListener("click", (e) => { e.preventDefault(); show(a.dataset.goto); }));
}

function wireRunLinks(root) {
  root.querySelectorAll("[data-run]").forEach((a) =>
    a.addEventListener("click", (e) => { e.preventDefault(); renderRunDetail(a.dataset.run); }));
}

/* ------------------------------------------------------------- run detail */

function traceTable(trace) {
  if (!trace?.length) return "";
  return `<h3>Steps</h3><table><thead><tr><th>Step</th><th>Action</th><th>Status</th>
    <th>Rung</th><th>Strategy</th><th>Confidence</th><th>Attempts</th></tr></thead><tbody>
    ${trace.map((t) => {
      const r = t.resolution ?? {};
      return `<tr>
        <td class="mono">${esc(t.step_id)}</td><td>${esc(t.action)}</td>
        <td>${esc(t.status)}</td>
        <td>${r.rung_index ?? "—"}</td>
        <td class="mono">${esc(r.strategy ?? "—")}${
          r.brittle ? ' <span class="tag">brittle</span>' : ""}</td>
        <td>${esc(r.confidence ?? "—")}</td>
        <td>${t.attempts ?? 1}</td></tr>`;
    }).join("")}</tbody></table>`;
}

function evidenceBlock(runId, files) {
  if (!files?.length) return "";
  const shots = files.filter((f) => f.kind === "screenshot");
  return `<h3>Evidence</h3>
    <ul>${files.map((f) => `<li><a href="/runs/${encodeURIComponent(runId)}/evidence/${
      encodeURIComponent(f.name)}" target="_blank">${esc(f.name)}</a>
      <span class="muted">${esc(f.bytes)} bytes</span>
      ${f.redacted ? "" : ' <span class="tag">not redacted</span>'}</li>`).join("")}</ul>
    ${shots.length ? `<div class="banner"><b>Screenshots are not redacted.</b>
      An image of a member record shows everything the page showed, and no text pass
      can mask it.</div>` + shots.map((f) =>
      `<p><img class="shot" alt="${esc(f.name)}"
        src="/runs/${encodeURIComponent(runId)}/evidence/${encodeURIComponent(f.name)}"></p>`).join("") : ""}`;
}

async function renderRunDetail(runId) {
  const { status, body } = await api(`/runs/${encodeURIComponent(runId)}`);
  if (status !== 200) {
    view.innerHTML = `<div class="card"><p>No run <code>${esc(runId)}</code> in this process.</p></div>`;
    return;
  }
  const result = body.result ?? body;
  const warnings = result.warnings ?? [];
  view.innerHTML = `<div class="card">
    <h2>Run <span class="mono">${esc(runId)}</span> ${pill(body.status ?? result.classification)}</h2>
    ${kv([
      ["capability", (body.capability ?? result.capability)?.id ?? ""],
      ["version", (body.capability ?? result.capability)?.version ?? ""],
      ["duration", result.duration_ms ? Math.round(result.duration_ms) + " ms" : ""],
      ["inputs", html(`<span class="mono">${esc(JSON.stringify(result.inputs ?? {}))}</span>`)],
      ["outputs", result.outputs ? html(`<span class="mono">${esc(JSON.stringify(result.outputs))}</span>`) : ""],
      ["outcome", result.outcome ?? ""],
      ["message", result.message ?? ""],
    ])}
    ${result.failure ? `<h3>Where it stopped</h3>${kv([
      ["step", result.failure.step_id],
      ["expected", result.failure.expected],
      ["observed", result.failure.observed]])}` : ""}
    ${warnings.length ? `<h3>Drift warnings</h3><ul>${
      warnings.map((w) => `<li>${esc(w)}</li>`).join("")}</ul>` : ""}
    ${traceTable(result.trace)}
    ${evidenceBlock(runId, body.evidence)}
  </div>`;
}

/* ---------------------------------------------------------- interventions */

async function renderInterventions() {
  const { body } = await api("/interventions");
  const items = body?.interventions ?? [];
  view.innerHTML = `<div class="card">
      <h2>Pending interventions</h2>
      <div class="banner stop"><b>This dashboard does not drive the browser.</b>
        A paused run is holding a live browser window on the machine running the API.
        Do the step yourself in that window, then press Resume here &mdash; the run
        re-checks its checkpoint before continuing. Resume does not perform anything.</div>
      ${items.length ? "" : '<p class="muted">Nothing is waiting.</p>'}
    </div>` + items.map((it) => {
      const req = it.request ?? {};
      const stopped = req.stopped ?? {};
      return `<div class="card" data-run="${esc(it.run_id)}">
        <h2>${esc(req.capability?.id ?? it.capability.id)}
          <span class="pill s-escalation_required">escalation_required</span></h2>
        ${kv([
          ["run", html(`<span class="mono">${esc(it.run_id)}</span>`)],
          ["blocked step", html(`<span class="mono">${esc(stopped.step_id ?? "")}</span>`)],
          ["why", stopped.reason ?? ""],
          ["what will be checked", stopped.expected ?? ""],
          ["observed", stopped.observed ?? ""],
          ["url", html(`<span class="mono">${esc(req.state?.url ?? "")}</span>`)],
          ["completed", html(`<span class="mono">${esc((req.completed_steps ?? []).join(", "))}</span>`)],
          ["inputs", html(`<span class="mono">${esc(JSON.stringify(req.inputs ?? {}))}</span>`)],
        ])}
        ${evidenceBlock(it.run_id, it.evidence)}
        <h3>Hand control back</h3>
        <div class="banner">This step posts and cannot be reversed. Automation has
          performed nothing. The browser is open on that screen &mdash; if you approve
          of this step, perform it there. Then press the button below and the run will
          <b>check the page</b> to see what actually happened. You are not telling it
          the outcome.</div>
        <label>Notes (optional, audit trail only) <input type="text" class="notes"
          placeholder="e.g. posted the transfer manually"></label>
        <p><button class="act go" data-do="done">I&rsquo;m done &mdash; check it</button></p>
        <div class="outcome"></div>
      </div>`;
    }).join("");

  view.querySelectorAll("[data-do]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const card = btn.closest(".card");
      const out = $(".outcome", card);
      out.innerHTML = '<p class="muted">Checking the page&hellip;</p>';
      const { status, body } = await api(
        `/runs/${encodeURIComponent(card.dataset.run)}/${btn.dataset.do}`,
        { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ notes: $(".notes", card).value, operator: "dashboard" }) });
      out.innerHTML = `<p>HTTP ${status} ${pill(body?.status ?? body?.classification ?? "")}</p>
        <pre>${esc(JSON.stringify(body?.result ?? body, null, 2))}</pre>`;
      refreshPending();
    }));
}

/* ------------------------------------------------------------------- shell */

/* A parked turn is stale the moment an operator finishes with it. The server
 * rewrites the stored turn; this notices and repaints. Only polls while there
 * is something waiting, and only while the Chat tab is the one on screen. */
async function refreshChat() {
  if (current !== "chat") return;
  if (!chatLog.some((t) => t.classification === "escalation_required")) return;
  const { body } = await api("/chat");
  if (!Array.isArray(body?.turns)) return;
  const typed = $(".chat-form input", view)?.value ?? "";
  chatLog.length = 0;
  chatLog.push(...body.turns);
  renderChat();
  const box = $(".chat-form input", view);
  if (box) box.value = typed;
}

async function refreshPending() {
  const { body } = await api("/interventions");
  const n = body?.count ?? 0;
  const badge = $("#pending-count");
  badge.textContent = n;
  badge.hidden = n === 0;
}

const views = { catalog: renderCatalog, runs: renderRuns,
                interventions: renderInterventions };

function show(name) {
  if (!views[name]) return;
  current = name;
  document.querySelectorAll("nav button").forEach((x) =>
    x.classList.toggle("on", x.dataset.view === name));
  writeState({ view: name });
  views[name]();
}

document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => show(b.dataset.view)));



/* ------------------------------------------------------------------- chat */

/* The transcript is held by the API, not by this page.
 *
 * Browser storage was the obvious place and is the wrong one: this file is
 * held to having no route out except fetch, and a transcript kept in the
 * browser would be a new place data comes to rest that the redaction sink
 * never sees. Every leak this project has had appeared at exactly that
 * kind of new surface. The server already scrubs what it stores, so the
 * transcript is read back from it like anything else.
 */
const chatLog = [];

function chose(c) {
  if (!c) return "";
  return `<div class="chose">Called <code>${esc(c.capability)}</code>
    @ ${esc(c.version)}
    ${c.required_role ? `<span class="tag role">${esc(c.required_role)}</span>` : ""}
    ${c.status === "draft" ? '<span class="tag draft">draft</span>' : ""}
    with <code>${esc(JSON.stringify(c.inputs ?? {}))}</code></div>`;
}

/* What the model could have chosen, as a contract rather than as prose.
 *
 * The recorded description is the discovery goal: it reads as a sentence,
 * names the member the flow happened to be found on, and carries `<param>`
 * placeholders. Useless to someone deciding what to ask for next. What they
 * need is a name and the arguments it takes.
 */
function availableList(items) {
  const prefix = commonPrefixToken(items.map((x) => x.id));
  return `<div class="chose"><b>What I can do</b>
    <ul class="offers">${items.map((a) => offerLine(a, prefix)).join("")}</ul></div>`;
}

function offerLine(a, prefix) {
  const needs = (a.needs || []);
  const required = needs.filter((n) => n.required).map((n) => n.name);
  const optional = needs.filter((n) => !n.required).map((n) => n.name);
  const args = [
    required.length ? `needs ${required.map(fieldLabel).join(", ")}` : "",
    optional.length ? `optional ${optional.map(fieldLabel).join(", ")}` : "",
  ].filter(Boolean).join(" &middot; ");
  return `<li><b>${esc(shortName(a.id, prefix))}</b>
    ${a.required_role ? `<span class="tag role">Requires ${esc(a.required_role)}</span>` : ""}
    ${a.requires_human ? '<span class="tag">Irreversible step</span>' : ""}
    ${args ? `<div class="muted">${args}</div>` : ""}</li>`;
}

function renderChat() {
  view.innerHTML = `<div class="card">
      <h2>Chat</h2>
      <div class="chat-log">${chatLog.map((t, ix) => t.you
        ? `<div class="turn you"><span class="said">${esc(t.you)}</span></div>`
        : `<div class="turn"><span class="said">${esc(t.reply)}</span></div>
           ${chose(t.chose)}
           ${t.available && ix === chatLog.length - 1 ? availableList(t.available) : ""}
           ${t.resolved_by_operator ? `<div class="chose">An operator finished this
              in the live session. The run re-checked the page and continued from
              what it found there.</div>` : ""}
           ${t.classification ? `<p>${pill(t.classification)}
              ${t.run_id ? `<a href="#" data-run="${esc(t.run_id)}">open run</a>` : ""}</p>` : ""}`
      ).join("") || '<p class="muted">Ask for something.</p>'}</div>
      <form class="chat-form">
        <input type="text" name="message" autocomplete="off"
               placeholder="e.g. what is the balance of share 100234-S0001-6 for member 100234?">
        <button class="act go" type="submit">Send</button>
      </form>
      <div class="examples">
        ${["What is the balance of share 100234-S0001-6 for member 100234?",
           "Look up member 999999's share balance",
           "Transfer 5.00 from 100987-MMKT-5 to 100987-MMKT-6 for member 100987, memo demo",
           "Delete all the accounts"].map((e) =>
          `<button data-example="${esc(e)}">${esc(e)}</button>`).join("")}
      </div>
    </div>`;

  view.querySelectorAll("[data-example]").forEach((b) =>
    b.addEventListener("click", () => {
      $(".chat-form input", view).value = b.dataset.example;
    }));
  wireRunLinks(view);

  const form = $(".chat-form", view);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const input = $("input", form);
    const message = input.value.trim();
    if (!message) return;
    chatLog.push({ you: message });
    chatLog.push({ reply: "Thinking…" });
    renderChat();
    input.value = "";
    const { body } = await api("/chat", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ message }) });
    chatLog.pop();
    chatLog.push({
      reply: body?.reply ?? body?.error ?? "No reply.",
      chose: body?.chose, available: body?.available,
      classification: body?.classification, run_id: body?.run_id });
    renderChat();
    refreshPending();
  });
}

views.chat = renderChat;

/* Hydrate the transcript before the first paint, so opening on the Chat tab
 * after a restart shows the conversation rather than an empty box. */
async function boot() {
  const { body } = await api("/chat");
  if (Array.isArray(body?.turns)) chatLog.push(...body.turns);
  /* The fragment decides the opening tab, so a reload lands where you were. */
  show(readState().view || current);
  refreshPending();
  setInterval(() => { refreshPending(); refreshChat(); }, 5000);
}

boot();
