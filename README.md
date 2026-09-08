# Computer-use automation: discover once, replay deterministically

An LLM drives a legacy back-office UI to accomplish a goal, the successful run
is recorded as a typed capability artifact, and that artifact replays
deterministically with **no model in the loop**. Recorded capabilities are
exposed as a callable API, driven by a chatbot, and watched on a dashboard.

Two targets ship, and pointing at a second one was a configuration exercise
rather than a rewrite:

- **MERIDIAN CORE** — a live, externally hosted credit-union console at
  `web-sample.interface-hiring.com`. Server-rendered, table layout, no test
  ids, a numbered menu, a per-transaction hidden token, and injectable faults.
- **CoreServ** — a purpose-written console in this repo (`coreserv/`), used
  for offline work. Real `<frameset>`, tables nested three deep, ids that
  rotate every render.

Design write-up: [`REPORT.md`](REPORT.md) (the core) and
[`REPORT-PHASE2.md`](REPORT-PHASE2.md) (what adapting to MERIDIAN took).
Decisions and their rejected alternatives:
[`docs/phase2-decisions.md`](docs/phase2-decisions.md). Runs and logs:
[`evidence/README.md`](evidence/README.md).

---

## Setup: clone to running, copy and paste

Python 3.11+. Every block below runs as-is.

**1. Install.**

```bash
git clone <repo-url> && cd interface.ai
git checkout phase-2-cua
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium          # ~300MB, one time
```

**2. Credentials.** Paste this whole block, then edit the one line with your
Anthropic key. Everything else is correct as written: the MERIDIAN operators
are the brief's public demo accounts.

```bash
cat > .env <<'EOF'
ANTHROPIC_API_KEY=sk-ant-REPLACE-ME
ANTHROPIC_WORKSPACE_ID=            # only if your key is workspace-scoped
GEMINI_API_KEY=                    # optional, for --provider gemini

MERIDIAN_OPERATOR=teller1
MERIDIAN_PASSWORD=password
MERIDIAN_SUPERVISOR=super1
MERIDIAN_SUPERVISOR_PASSWORD=password

CORESERV_USERNAME=operator
CORESERV_PASSWORD=devpassword

PAUSE_TIMEOUT_S=900
EOF
```

Artifacts store the **names** of credential variables, never values. A pasted
secret fails schema validation.

**A model key is needed only to record a new capability or to use the Chat
tab.** Replay imports no model client, so every capability, the API, the
catalogue, the dashboard and the whole escalation path work without one.

**3. Prove it works before running anything else.**

```bash
python -m pytest tests/ -q                              # 508 pass, 21 skip
python -m replay.run --capability member_share_balance --version 1.1.0 \
  --input member_ref=100234 --input share_ref=100234-S0001-12
```

The replay drives the live MERIDIAN site and prints a JSON result ending in
`"classification": "success"` with the share balance. No API key involved.

**4. Start everything.**

```bash
uvicorn api.service:app --port 8900        # terminal 1
uvicorn coreserv.main:app --port 8800      # terminal 2, only for the two CoreServ capabilities
```

**5. Check the target is not in a forced-error state.** MERIDIAN has a global
fault switch that persists across sessions, and someone may have left it on.

```bash
B=https://web-sample.interface-hiring.com; J=/tmp/meridian.jar; rm -f $J
curl -s -c $J -b $J -o /dev/null "$B/signon"
curl -s -c $J -b $J -o /dev/null -d "operator=teller1&password=password&branch=MAIN-001" "$B/signon"
curl -s -c $J -b $J "$B/settings" | grep -oE '<option[^>]*selected[^>]*>'
```

`<option value="" selected>` means normal operation. Anything else means a fault
is forced globally; clear it on the System Settings screen before demoing.

---

## Quick start

Open **<http://127.0.0.1:8900/ui>**. Four tabs:

| Tab | What it is |
|---|---|
| **Chat** | Plain English → a capability invocation. Shows which capability it chose and with what arguments. |
| **Catalog** | Every capability: typed inputs and outputs, declared business outcomes, whether it needs a supervisor, whether it contains an irreversible step. Invoke one directly. |
| **Runs** | Every run, colour-coded by classification. Seeded from `evidence/dashboard/` so a fresh clone opens with history rather than an empty table, and appended to as you use it. |
| **Interventions** | Runs parked at an irreversible step, holding a live session, with the captured state and one control: **I'm done — check it**. |

Try, in the Chat tab:

```
What is the balance of share 100234-S0001-12 for member 100234?
Look up the share balance for member 999999
Transfer 5.00 from 100987-MMKT-7 to 100987-MMKT-8 for member 100987, memo demo
```

The app is stateful in memory and every transfer you run moves real money on
it, so check a share holds enough before using it. `100987-MMKT-7` and
`100987-MMKT-8` are the pair the examples use. The first returns a balance. The second
returns **"No member exists with the supplied identifier"** — a business outcome, an answer rather than an error.
The third stops at the irreversible post step and hands you to Interventions.

---

## Pointing this at a different application

This is the part worth reading. Adapting to a new target is **a profile, a
policy and a recording** — no engine changes. A test enforces that: it fails
if any application name or app-shaped selector appears in executable code
under `replay/`, `perception/`, `escalation/`, `capability/` or `api/`.

### 1. Write an app profile

`config/app_profiles/<yourapp>.json`. Everything the engine needs to know
about *this specific application* lives here:

| Field | What it answers |
|---|---|
| `error_markers` | What text means session expired / server error / maintenance on this app |
| `recovery` | What clearing each condition *does* — `dismiss_control` (click, stay put) vs `reload_step_url` (re-request the step's URL). CoreServ's interstitial is a button that re-renders in place; MERIDIAN's is a link that navigates away and loses your position |
| `version_pattern` | Regex reading the app version off the page, for drift detection. Rejected at load if it cannot match |
| `content_frame` | Frame holding the working area, or `null` for a single-document app |
| `entry_path` | Where a flow starts, post-authentication |
| `commit_paths` | Endpoints that commit. A click that *lands* on one is irreversible whatever the button was called |
| `risk_verbs` / `near_miss_verbs` | Labels that suggest a commit. A first guess for review, not a determination |
| `sensitive_labels` | Field labels naming personal data, e.g. `E-mail`, `Phone` |
| `chrome_literals` | Values the app prints into its own furniture (a session id, the operator name) that must be scrubbed from evidence |
| `redaction` | Where known-sensitive literals come from. A profile with none reports itself **degraded**, loudly |
| `auth_defaults` | Sign-on path, the control elements, credential variable names, non-secret parameters like a branch code, and per-role credential sets |
| `business_outcomes` | Answers this app gives that are results, not faults — "no records matched", "insufficient funds" |
| `parameter_aliases` | Field labels too generic to name a parameter after (MERIDIAN labels its member search `Value`) |

Copy `config/app_profiles/meridian.json` and edit. Nothing else is required to
make the engine understand a new app.

### 2. Write a discovery policy

`config/discovery_policies/<yourapp>.json` — the allowlist the agent runs
under. Deliberately a separate file: a profile describes what the app **is**,
a policy declares what the agent may **do** to it.

```json
{
  "allowed_origins": ["https://yourapp.example.com"],
  "allowed_paths": ["/signon", "/menu", "/members", "/members/*"],
  "allowed_actions": ["navigate", "click", "fill", "select", "check", "extract"],
  "risky_action_handling": "require_confirmation",
  "max_steps": 25,
  "timeout_ms": 300000
}
```

Note `fnmatch`'s `*` crosses `/`, so `/members/*` permits everything beneath
it including post endpoints. That is intentional — the control over
irreversible actions is the risky-step gate, not this list — and
`config/discovery_policies/meridian.json` says so at length.

### 3. Check what perception sees, before recording anything

```bash
python -m scripts.a11y_diagnostic_meridian --base-url https://yourapp.example.com
```

Writes per-page accessibility dumps and a `findings.json` to
`evidence/a11y_diagnostic_meridian/`: accessible-name coverage per control,
which labelling rule produced each name, anything nameless, raw vs filtered
token counts, and whether hidden tokens or a live clock reach the tree. Worth
running first — it is how the frame model, the missing `columnheader` nodes
and the DOM-only token were all found on MERIDIAN before a line was adapted.

### 4. Record a capability

```bash
python -m discovery.run \
  --app yourapp \
  --target https://yourapp.example.com \
  --goal "Look up member 100234 and read the balance of share 100234-S0001-12" \
  --capability-id member_share_balance \
  --tenant demo --app-version 1.0.0
```

Writes `evidence/discovery/{run_id}/` with `cycles.jsonl` (every observe →
decide → act cycle and the model's reasoning), `artifact.json`, per-step
screenshots, and a `summary.json` carrying token usage and cost. The artifact
is validated by loading it back before the run reports success.

Useful flags:

| Flag | Why |
|---|---|
| `--role supervisor` | Record under a privileged operator. Writes `capability.required_role`, which the catalogue exposes so an agent knows before invoking |
| `--policy config/discovery_policies/yourapp-recording.json` | A relaxed policy for recording an irreversible flow — see below |
| `--provider gemini` | The other model client |
| `--max-seconds 420` | Wall-clock budget. Provider backoff is not counted against it |

**Goals are specifications. Write them precisely.** Two real failures: a loose
goal produced a capability that read a *share count* and called it a balance,
and "reach the confirmation screen" was satisfied by a review page. Name the
screen and name the values.

Install a recorded artifact:

```bash
mkdir -p capabilities/member_share_balance
cp evidence/discovery/<run_id>/artifact.json \
   capabilities/member_share_balance/1.0.0.json
```

The capability id inside the artifact must match the directory name.

### 5. Recording an irreversible flow

You cannot record a review→post flow without posting once, so a gate that
blocks discovery makes every capability ending in a post unrecordable. The
gate is relaxed *for recording*, by a person, in a named file:

```bash
python -m discovery.run --app meridian \
  --policy config/discovery_policies/meridian-recording.json \
  --role supervisor \
  --goal "Place a hold on member 100234's share ... and click Apply Hold" \
  --capability-id member_place_hold --target https://web-sample.interface-hiring.com
```

The **emitted artifact never inherits that posture** — the recorder always
writes `risky_action_handling: "require_confirmation"`, so a relaxed recording
session cannot produce a capability that posts unattended.

### 6. Replay it

```bash
python -m replay.run --capability member_share_balance --version 1.1.0 \
  --input member_ref=100234 --input share_ref=100234-S0001-12
```

A capability generalises exactly as far as its declared inputs, and this one is
the worked example. Version 1.0.0 was recorded from a goal that named one share,
so the share id was fixed in the locator: it replayed for any member holding an
`-S0001-12` share and returned a checkpoint failure for everyone else, while its
name promised something general. The recorder derives parameters from values
that were **typed or selected**, and a value appearing only inside a locator
scope has no path to becoming an input, so re-recording could not have fixed it.
Review widened it by hand into 1.1.0, where the share is a declared input, and
1.0.0 was retired. The goal is the specification, and that is what `status:
draft` exists to catch.

Replay exit codes: `0` success **and** business outcome, `1` hard failure,
`2` caller error, `3` auth failure. "No such member" is an answer, not a
crash, which is why it shares an exit code with success.

Discovery adds `4`: the run produced a valid artifact but stopped at an
irreversible step, so a script must not read "artifact written" as "flow
proven".

---

## Demo path (MERIDIAN, no local server needed)

```bash
uvicorn api.service:app --port 8900        # then open /ui
```

**1. A capability that answers.** Chat: *"What is the balance of share
100234-S0001-12 for member 100234?"* → `success`, the balance stated plainly.
The chosen capability and its arguments are shown beside the answer.

**2. A business outcome.** Chat: *"Look up the share balance for member
999999"* → `business_outcome`, *"No member exists with the supplied
identifier."* Never phrased as a failure.

**3. An irreversible step stopping for a human.** Chat: *"Transfer 5.00 from
100987-MMKT-7 to 100987-MMKT-8 for member 100987, memo demo"* →
`escalation_required`. Ten steps complete, `s11` (`post_transfer_button`)
blocks, and the reply says what would have to happen for the run to continue.
Nothing was posted.

The run does not end there. It keeps the browser session open and parks,
and **Interventions** shows it: the blocked step, why it stopped, what will be
checked when you are done, and the stuck screenshot.

There is no `attended` flag. It used to ask the caller to predict whether a
human would be available, which a calling agent cannot know, and the unattended
path tore the browser down before responding so its `202` named a session that
no longer existed. Every invocation now parks the same way; whether anyone comes
is answered by the pause deadline (`PAUSE_TIMEOUT_S`, default 15 minutes).

**3b. Finishing it.** The browser window is open on the confirm screen. If you
approve of the step, perform it there, then press **I'm done — check it**. The
run re-evaluates the blocked step's checkpoint against the live page and that
decides the outcome: `success` with the confirmation number if the transfer
posted, `not_performed` if it did not. One control, not Resume and Abort: the
button is a trigger to look, never a claim about what happened. **The transfer
is real, so do not perform it unless you mean to.**

**4. A supervisor-gated action.** `member_place_hold` declares
`required_role: supervisor`; the catalogue and dashboard show it. Run it with
`MERIDIAN_SUPERVISOR` pointed at `teller1` and it returns
`business_outcome / supervisor_override_required` — "this operator cannot do
this" is an answer.

**5. Injected faults.** MERIDIAN takes `?inject=<kind>` per request or a
global setting at `/settings`:

```
validation 400 · notfound 404 · permission 403 · timeout 440
maintenance 503 · server 500
```

A session timeout mid-flow is re-authenticated and retried **only** for steps
declaring `retry_after_reauth` — never for a risky one, which fails toward
escalation instead, because a post that may already have landed must not be
repeated.

### Offline, on CoreServ

```bash
uvicorn coreserv.main:app --port 8800      # separate terminal

python -m discovery.run --app coreserv --target http://localhost:8800 \
  --goal "Look up member 10003 and read their current savings balance" \
  --capability-id member_savings_balance_discovered

python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --input member_ref=10003
```

CoreServ's faults are server-side flags:

```bash
curl -sX POST localhost:8800/_faults -H 'Content-Type: application/json' \
     -d '{"fault":"member_not_found","enabled":true}'
curl -sX POST localhost:8800/_faults/reset
```

`member_not_found`, `restricted_member`, `maintenance_interstitial`,
`slow_response`, `session_expired`, `validation_error`, `server_error`.

---

## The capability API

Under the dashboard, and callable on its own. Every invocation runs a
deterministic replay; `api/` imports no engine module, asserted structurally.

```
GET  /capabilities                              the catalogue
GET  /capabilities/{id}/{version}               one contract
POST /capabilities/{id}/{version}/invoke        run it
GET  /runs · /runs/{id} · /runs/{id}/evidence   history and evidence
GET  /interventions                             runs awaiting a person
GET  /chat                                      the transcript, so a restart keeps it
POST /runs/{id}/done                            operator has finished; check the page
POST /chat                                      plain English → an invocation
```

```bash
curl -s localhost:8900/capabilities | python3 -m json.tool
curl -sX POST localhost:8900/capabilities/member_share_balance/1.0.0/invoke \
     -H 'content-type: application/json' \
     -d '{"inputs":{"member_ref":"100234"}}'
```

HTTP status carries the result contract: `success` and `business_outcome` are
both **200** — the caller asked a question and got an answer — `202` is
accepted-and-awaiting-a-human, `400` a caller error, `502` a system or app
failure. Every invocation runs headed and parks at an irreversible step, so
`202` means the session is open and waiting rather than already gone.

---

## Escalation from the CLI

`--escalate` is off by default so unattended replay stays unattended. It
implies `--headed`, since a human cannot drive a headless browser.

```bash
python -m replay.run --capability member_funds_transfer --version 1.0.0 \
  --input member_ref=100987 --input from_share=100987-MMKT-7 \
  --input to_share=100987-MMKT-8 --input amount=5.00 --input memo=demo \
  --escalate
```

The run pauses and prints the intervention request. Automation is locked out —
the executor asserts ownership before every action. Drive the **already-open**
window; it is the same session. Press `r` to resume or `a` to abort. On resume
the blocked step's checkpoint is re-evaluated before the run continues.

The dashboard's Interventions tab is a second operator surface over the same
mechanism — it signals resume; **it does not drive the browser**.

---

## Cross-tenant — one artifact, two tenants

```bash
TENANT=cascade uvicorn coreserv.main:app --port 8800

python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --tenant cascade --input member_ref=4471820019
```

Resolved through `capabilities/member_savings_balance/tenants/cascade.json`.
Cascade searches by ten-digit account number and relabels the field; the
overlay is two element chains, one input pattern and one version string.

An overlay may move a tenant to a different **host**: `allowed_origins` is
derived from `target.base_url` rather than stored beside it, so moving the URL
moves the allowlist with it.

---

## Tests

```bash
python -m pytest tests/ -q
```

**508 pass with nothing running**, 21 skip. With CoreServ up on 8800 the live
replay and escalation tests run too. No test needs an API key — model calls are exercised by real runs
in `evidence/`, and the loop's logic is tested against synthetic accessibility
trees.

Several tests are structural rather than behavioural, and they are the ones
worth knowing about:

- **`tests/test_redaction_chokepoint.py`** parses every first-party package
  and fails if anything writes a file or returns an HTTP body without going
  through `capability/sink.py`. Redaction has failed at every *new surface*
  this project added, so the number of places able to emit data is capped.
- **`tests/test_profile.py`** fails if an application name or app-shaped
  selector appears in engine code — the adapter seam, asserted.
- **`tests/scope.py`** derives which packages those guards cover from the
  repository, so a package added tomorrow is covered the moment it exists.
  Four guards previously carried hand-maintained lists and went stale.

---

## Layout

```
api/          capability API, dashboard, chatbot  ← wrapper only, no engine
perception/   accessibility-tree snapshot, filtering, label augmentation
capability/   artifact schema, loading, profiles, redaction sink
discovery/    LLM loop + recorder      ─┐ both use perception;
replay/       deterministic engine     ─┘ neither imports the other
escalation/   control transfer, operator surfaces
config/       app profiles + discovery policies   ← per-app knowledge
capabilities/ saved artifacts + tenant overlays
coreserv/     the offline target app
evidence/     runs, logs, findings  ← start at evidence/README.md
docs/         schema spec, decisions logs, the MERIDIAN diagnostic
```

## Evidence

[`evidence/README.md`](evidence/README.md) maps each directory to the
requirement it demonstrates. [`docs/phase2-diagnostic.md`](docs/phase2-diagnostic.md)
is the measurement pass taken against MERIDIAN *before* any adaptation, plus
an honest account of what adapting actually took.
