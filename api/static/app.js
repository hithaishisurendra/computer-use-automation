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

/* ---------------------------------------------------------------- catalog */

async function renderCatalog() {
  const { body } = await api("/capabilities");
  const caps = body?.capabilities ?? [];
  view.innerHTML = caps.map((c) => {
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
         An unattended invocation stops there and asks for a person. Tick
         <b>attended</b> to run it with a human available.</div>`
      : "";

    return `<div class="card" data-cap="${esc(c.id)}" data-ver="${esc(c.version)}">
      <h2>${esc(c.name)}
        <span class="tag ${c.status === "draft" ? "draft" : ""}">${esc(c.status)}</span>
        ${c.requires_human ? '<span class="tag">needs a human</span>' : ""}
        ${c.required_role ? `<span class="tag role">${esc(c.required_role)}</span>` : ""}
      </h2>
      <div class="muted mono">${esc(c.id)} @ ${esc(c.version)} &middot; ${esc(c.app)} / ${esc(c.tenant)}</div>
      <p>${esc(c.description)}</p>
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
        <label><input type="checkbox" name="__attended"> attended &mdash; headed browser, a risky step pauses for you</label>
        <p><button class="act go" type="submit">Invoke</button></p>
      </form>
      <div class="result"></div>
    </div>`;
  }).join("") || `<div class="card"><p class="muted">No capabilities recorded yet.</p></div>`;

  view.querySelectorAll("form.invoke").forEach((form) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const card = form.closest(".card");
      const out = $(".result", card);
      const inputs = {};
      let attended = false;
      new FormData(form).forEach((v, k) => {
        if (k === "__attended") attended = true;
        else if (String(v).length) inputs[k] = v;
      });
      out.innerHTML = `<p class="muted">Running&hellip;${attended
        ? " A browser window will open on the machine running the API." : ""}</p>`;
      const { status, body } = await api(
        `/capabilities/${encodeURIComponent(card.dataset.cap)}/${encodeURIComponent(card.dataset.ver)}/invoke`,
        { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ inputs, attended }) });
      out.innerHTML = `<p>HTTP ${status} ${pill(body?.status ?? body?.classification ?? "unknown")}
        ${body?.run_id ? `<a href="#" data-run="${esc(body.run_id)}">open run</a>` : ""}</p>
        <pre>${esc(JSON.stringify(body, null, 2))}</pre>`;
      wireRunLinks(out);
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
        <td>${pill(r.status)}${r.attended ? ' <span class="tag">attended</span>' : ""}</td>
        <td>${when(r.started_at)}</td>
        <td>${r.duration_ms ? Math.round(r.duration_ms) + " ms" : "—"}</td>
        <td class="mono">${esc(JSON.stringify(r.inputs ?? {}))}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted">No runs yet.</td></tr>`}
    </tbody></table></div>`;
  wireRunLinks(view);
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
          ["expected on resume", stopped.expected ?? ""],
          ["observed", stopped.observed ?? ""],
          ["url", html(`<span class="mono">${esc(req.state?.url ?? "")}</span>`)],
          ["completed", html(`<span class="mono">${esc((req.completed_steps ?? []).join(", "))}</span>`)],
          ["inputs", html(`<span class="mono">${esc(JSON.stringify(req.inputs ?? {}))}</span>`)],
        ])}
        ${evidenceBlock(it.run_id, it.evidence)}
        <h3>Hand control back</h3>
        <label>What did you do? <input type="text" class="notes"
          placeholder="e.g. posted the transfer manually and saw the confirmation"></label>
        <p><button class="act go" data-do="resume">Resume</button>
           <button class="act stop" data-do="abort">Abort</button></p>
        <div class="outcome"></div>
      </div>`;
    }).join("");

  view.querySelectorAll("[data-do]").forEach((btn) =>
    btn.addEventListener("click", async () => {
      const card = btn.closest(".card");
      const out = $(".outcome", card);
      out.innerHTML = '<p class="muted">Signalling&hellip;</p>';
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

async function refreshPending() {
  const { body } = await api("/interventions");
  const n = body?.count ?? 0;
  const badge = $("#pending-count");
  badge.textContent = n;
  badge.hidden = n === 0;
}

const views = { catalog: renderCatalog, runs: renderRuns,
                interventions: renderInterventions };

document.querySelectorAll("nav button").forEach((b) =>
  b.addEventListener("click", () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.remove("on"));
    b.classList.add("on");
    current = b.dataset.view;
    views[current]();
  }));



/* ------------------------------------------------------------------- chat */

const chatLog = [];

function chose(c) {
  if (!c) return "";
  return `<div class="chose">Called <code>${esc(c.capability)}</code>
    @ ${esc(c.version)}
    ${c.required_role ? `<span class="tag role">${esc(c.required_role)}</span>` : ""}
    ${c.status === "draft" ? '<span class="tag draft">draft</span>' : ""}
    with <code>${esc(JSON.stringify(c.inputs ?? {}))}</code></div>`;
}

function renderChat() {
  view.innerHTML = `<div class="card">
      <h2>Chat</h2>
      <p class="muted">A thin driver over the capability API, standing in for the
        AI agent. It picks a capability and its arguments; the API runs it. Every
        answer below shows which capability was chosen, so the mapping is visible
        rather than implied.</p>
      <div class="chat-log">${chatLog.map((t) => t.you
        ? `<div class="turn you"><span class="said">${esc(t.you)}</span></div>`
        : `<div class="turn"><span class="said">${esc(t.reply)}</span></div>
           ${chose(t.chose)}
           ${t.available ? `<div class="chose">Available:<ul>${t.available.map((a) =>
              `<li><code>${esc(a.id)}</code> &mdash; ${esc(a.description)}
               ${a.required_role ? `<span class="tag role">${esc(a.required_role)}</span>` : ""}</li>`
             ).join("")}</ul></div>` : ""}
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

views[current]();
refreshPending();
setInterval(refreshPending, 5000);
