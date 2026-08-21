# Design write-up

A system that lets an LLM discover how to operate a legacy back-office UI
once, then replays that flow deterministically without the model. Built
against **CoreServ**, a purpose-written proxy target: real `<frameset>`,
tables nested three deep, server-generated element ids that rotate on every
render, no test ids, and seven server-side fault flags.

---

## 1. Architecture

```
coreserv/     the target app (proxy surface)
perception/   accessibility-tree snapshot, filtering, label augmentation
capability/   artifact schema, loading, validation, redaction
discovery/    LLM loop + recorder          ─┐ both import perception
replay/       deterministic engine          ─┘ neither imports the other
escalation/   control transfer, operator surface
evidence/     runtime output
```

The load-bearing choice is that **`perception/` is a peer of `discovery/` and
`replay/`**, not owned by either. The same layer that hands the model a
filtered snapshot during discovery resolves a saved locator during replay. A
second surface (desktop) is a new module behind that interface; neither
consumer changes. Making the seam visible in the directory tree is doing real
work — it is what stops replay quietly growing its own perception.

**Discovery imports `replay/executor.py` rather than duplicating it**, so the
policy allowlist, risky-action rule and ownership gate are literally the same
code on both paths. Discovery cannot be more permissive than replay because
there is no second implementation for it to be permissive in. Tests assert
`discovery/loop.py` defines no `check_destination`/`check_action`/`check_risk`
of its own.

**Perception is the accessibility tree, not the DOM.** Screenshot+coordinates
was the strongest competitor and is genuinely more surface-agnostic, but
coordinates are the least stable locator available and CoreServ's hostility is
aimed precisely at DOM-position strategies. The bet was that role and
accessible name survive terrible nesting.

That bet was checked, not assumed, and **half of it failed**
(`evidence/a11y_diagnostic/REPORT.txt`): every link and button got a correct
accessible name; **every text input, select, radio and checkbox came back
nameless**, because CoreServ labels fields with an adjacent table cell rather
than `<label for>`. Worse than uniformly empty — the Account Type `<select>`
inherited its *selected option* as a name, which reads like a working label
and is actually the field's value.

**CoreServ was left unpatched and the perception layer compensates** —
adding a `<label for>` to each input would have fixed every case in one
commit, but that is fitting the world to the strategy, and the whole premise
is that we do not control these applications. `perception/labeling.py`
instead infers names from the surrounding DOM through a seven-rule chain,
recording which rule fired (`name_source`) so an inferred name is never
mistaken for one the platform supplied. Ten of eleven nameless controls
resolved; one did not, and stays reported rather than patched.

**Provider choice is configuration.** `discovery/model.py` exposes one
abstract method (`complete`); `AnthropicClient` and `GeminiClient` sit behind
it. Keeping both is the point — a single-implementation interface is an
untested guess about where the boundary is. Tool definitions are plain JSON
Schema with no per-provider translation. Runs used `gemini-3.5-flash`.

---

## 2. Artifact schema

Full spec: `docs/artifact-schema-spec.md`. Shape: `capability`, `target`,
`inputs`, `outputs`, `elements`, `steps`, `outcomes`, `policy`, `provenance`.

**Elements are a registry; steps are thin.** Steps reference element keys, so
a tenant override is a small diff against the registry rather than a patch to
steps by index. This is what makes §4 cheap.

**Each element carries a chain of locator rungs**, ordered most to least
robust, each with a confidence and a `brittle` flag. Replay records which rung
actually fired — falling through to a brittle rung is a drift signal worth
surfacing *on a run that succeeded*.

**Three versions, deliberately separate.** `schema_version` (the format),
`capability.version` (this flow), `target.app_version` (what it was recorded
against — a drift signal, never changed on its own).

**Outcomes are declared, not inferred.** `member_not_found` is a legitimate
answer; the artifact says so, so replay never has to guess whether something
is a result or a crash.

Two additions the live system forced. `steps[].frame`, because a frameset app
navigates a content region — a top-level `goto` destroys the frameset every
element lives in. And `cell_in_row` accepts `column_index` as well as
`column_header`, because the member-detail screen is a label/value table with
no column headers at all.

### The system refuses to assert what it did not observe

This appears in three independent places, and they are the same decision:

- **`outcomes: []`** on a discovered artifact. A happy-path run never saw "no
  records match", and inventing that outcome would be the artifact claiming
  something the run did not establish.
- **`status: "draft"`, always.** Discovery does not approve its own output.
- **No tenant overlay for a discovered capability.** An overlay asserts a flow
  *generalises across tenants*; a single-tenant discovery run establishes no
  such thing. It observed one app, one configuration, one tenant.

The lifecycle this implies — discover on one tenant → human review promotes
draft to approved → overlays authored as other tenants adopt it, each a
deliberate assertion with its own evidence — is designed for and **not built**.
What exists is the refusal to shortcut it.

---

## 3. Determinism & error handling

Replay imports no model client, asserted structurally by a test that parses
the imports of every file in `replay/`.

**Ambiguity is a miss, not a pick-the-first.** A rung matching three nodes has
identified nothing; taking `[0]` is how replay clicks the wrong row and
reports success. An ambiguous rung falls through to the next and the ambiguity
is recorded. On the results grid this is not theoretical: three identically
named "View" links mean `role_name` is genuinely ambiguous, so the recorder
skipped it and recorded `role_name_scoped` on `{{member_ref}}` first.

**Circular locators are suppressed — and uniqueness alone cannot catch them.**
In the real Gemini run the model targeted the balance cell as
`{"role":"cell","name":"8320.10",...}`
(`evidence/discovery/disc_60829c31/cycles.jsonl`). Recording `role_name(cell,
"8320.10")` would have been *unique on that page* and completely wrong: it
finds the balance only while the balance is 8320.10, which is never true for
the next member. The recorder discards name-based rungs for extraction targets
and records column-based ones instead. That distinction — unique is not the
same as correct — is why `discovery/recorder.py` probes strategies against the
captured tree rather than trusting what the model said.

**Chains are measured, not assumed.** Every candidate strategy is tried
against the page as it was at the moment of the action, and only strategies
resolving to exactly one element are recorded.

**Error taxonomy is two-layer, engine universals first.** Session expiry, 500
pages, the maintenance interstitial and timeouts are properties of the product
and live in the engine; `member_not_found` and `permission_denied` are
flow-specific and live in the artifact. Order is load-bearing: when the
session expires CoreServ bounces to a login page containing none of the
member's data, and a flow-first classifier would confidently report "no such
member" — a wrong answer a caller would act on.

Verified against all seven faults (`evidence/replay/MATRIX.txt`): business
outcomes exit **0**, the interstitial is dismissed and the step retried,
session expiry and 500s are escalation-eligible hard failures, malformed input
is a `caller_error` **with no browser ever opened**.

`slow_response` is reported as a hard failure rather than made to pass. The
fault injects 6s per request, pushing the search redirect past the artifact's
own 8000ms checkpoint. Widening the timeout would be tuning the evidence.

### Drift says whether we were looking at the right thing

Running the cascade overlay while a cascade instance sat on 8801 produced a
**misleading pass**: `business_outcome / member_not_found`, exit 0, `tenant:
"cascade"` — while actually driving *northridge* on 8800. The result contract
reported it as a clean, legitimate business outcome, because that is exactly
what it was.

What exposed it were the drift signals: `app_version 4.2.3 expected vs 4.2.1
observed`, and `member_ref_field` falling through to a brittle positional rung
because cascade's "Account Number" label was not on that page.

**The principle: the result contract says what happened; drift says whether we
were looking at the right thing.** They answer different questions, and a
system with only the first will confidently report correct-looking answers
about the wrong system.

---

## 4. Heterogeneity & multi-tenant

Two tenants of the same vendor product: `northridge` and `cascade` — same
routes, same flow, different labels, different results-column order, different
version string. Cascade also differs *semantically*: it searches by a ten-digit
account number where northridge searches by a five-digit member ID.

**Overlay size is a diagnostic.** The first parameter name was `member_id`,
taken from northridge's field label. That forced an override of the
results-row locator, because a scope keyed on "the member id column" means
nothing on a grid that has no such column. Renaming it to **`member_ref`
eliminated that override entirely** — `results_view_link` scopes on
`{{member_ref}}`, and each tenant's grid displays whichever identifier that
tenant searches by, so one chain resolves in both. The cascade overlay is now
two element chains, one input pattern and one version string.

The general rule: **a large overlay usually means the base encoded one
tenant's assumptions.** Overlay size is a signal about the base artifact, not
about the tenant. The recorder now applies this automatically — an identifier
field derives `{entity}_ref` rather than the tenant-specific label, so
"Member ID" and "Account Number" both yield a stable parameter name.

**Three tiers of divergence:**

| Tier | Example | Mechanism |
|---|---|---|
| Cosmetic | Label text, column order | Element chain override |
| Configuration | Identifier means an account number; different host/build | Input pattern + `target` override |
| Structural | Different steps, different outputs | **Fork** — `capability.derived_from` |

The loader enforces the boundary. Overlays may override element chains, an
input's pattern/description/example, and `base_url`/`app_version`; they may
**not** touch steps, outputs, an input's name/type/required/sensitivity, or
widen the policy allowlist (narrowing only, checked with the same predicate
discovery's `--allow-path` uses). Reaching past it fails with an error naming
the field and pointing at forking. Overlays express configuration drift; forks
express behavioural divergence.

**A real limitation.** An overlay cannot redirect replay to another tenant's
host. It may override `base_url` but **not** `policy.allowed_origins`, so an
overridden host fails its own origin check — and the replay CLI has no
`--base-url` flag. The fix is small and not built: derive `allowed_origins`
from `base_url` at resolution time rather than storing it independently, plus
a replay-time host override. Demonstrated at
`evidence/replay/run_8dee5028`, which is the same untouched artifact and
overlay reaching a genuine cascade instance: **success, no drift warnings,
every element at rung 0.**

**What is portable, precisely.** The artifact names no Playwright, CSS, XPath
or pixels — targets are role + accessible name + containing scope, which a
UIA/AX resolver could execute unchanged; `target.surface` selects the
resolver. The schema, loader, resolver semantics and result contract are
surface-agnostic.

What is **CoreServ-specific and would need per-app configuration**: the
engine-universal error detection is *string matching* — `"Your session has
ended."`, `"An unexpected error occurred."`, `"System Maintenance"` in
`replay/classify.py`. Those are one product's phrasings hardcoded in the
engine. The layering (universals before flow outcomes) generalises; the
strings do not, and belong in a per-app profile. `form_login` is likewise the
only implemented auth mode, and the login form is targeted by field name in
engine code rather than through the element registry. The frameset handling in
`_settle` and `_goto` is web-specific by construction.

---

## 5. Escalation & handoff

**Ownership is explicit state** on the session — `AUTOMATION | HUMAN |
RELEASED` — not an implicit consequence of which code is running. The two
diverge exactly when it matters: a retry that did not notice the pause.

**The check lives in the executor, alongside the policy check**, and for the
same reason: a pause the caller is trusted to respect is not a pause. Every
action asserts ownership before touching the page, so a stray retry cannot
race the operator regardless of which caller triggered it.

**The operator drives the same Playwright context.** `ControlledSession`
captures context identity at construction and re-asserts it on every transfer;
a handoff that quietly opened a new browser fails loudly rather than handing
someone a different session from the one that got stuck. Tested both
directions — identity preserved across a handoff, and a swapped context
rejected.

Detection routes an `InterventionRequest` carrying capability, step, expected
vs observed, URL, screenshot and accessibility snapshot — enough for an
operator who did not watch the run.

**Resume re-evaluates the failed step's checkpoint and continues from there.**
Restarting would repeat completed steps, repeating side effects and
potentially undoing what the operator just fixed. A resume whose checkpoint
still fails is not treated as recovery.

`evidence/escalation/run_61d94a77` is a live handoff — and it exercised the
harder case by accident. The operator's fix was **partial**: clearing the
fault flag did not repaint an already-rendered page, so the condition
persisted and a **second** intervention fired. The control trail is
`automation → human → automation → human → automation → released`, and the run
finished `success` with every step executed **exactly once**. Two
interventions, no step repeated — which is the property the resume design
exists to guarantee, tested under a partial fix rather than an idealised one.

**Mocked, deliberately:** the operator surface is a terminal prompt, and it
assumes the human is sitting at the machine running the agent. A real console
replaces `ConsoleOperator` and nothing else — same session, same request, same
decision, delivered over CDP screencast. Capture is **effect-level**: URL and
accessibility-state diff across the handoff, not keystrokes.

---

## 6. Safety

**Policy is enforced at the executor boundary**, immediately before an action
runs — never in the prompt. A prompt-level guardrail is a suggestion. The
allowlist covers origins, path globs and action types; `/_faults` is excluded,
so the agent cannot manipulate the app's own fault state.

**The allowlist was escapable by timing, and my own policy test caught it.**
Enforcement checked the URL after each action — but that check races the app:
clicking a link returns *before* the navigation lands, so the frame still
reports its previous URL. A test narrowing `allowed_paths` to exclude
`/member/*` and expecting a block returned **`success`**. Enforcement moved
into `_capture`, changing the invariant from "check after actions" — a
statement about code paths, only as good as the enumeration of them — to **"no
page is observed without validating where it is"**, a statement about states.
The pre-navigation check stays as well.

Risky steps are blocked under `require_confirmation` rather than proceeding,
since unattended replay has no confirmation channel — that is what the
escalation path is for.

**Redaction failed in both directions, four times, and every fix was a
retrofit.** This is the weakest part of the system and the pattern matters
more than the individual bugs:

- **Under-redaction ×3.** The a11y diagnostic wrote a seed member's full SSN,
  DOB, phone, email and address into committed evidence. Discovery's evidence
  writer used a pattern-only scrubber that caught SSNs by shape but not names,
  addresses or account numbers. Both capture *whole pages*, so sensitive
  values arrive as page **content** — never as declared fields, so
  sensitivity-driven masking had nothing to act on.
- **Over-redaction ×1.** The email pattern matched
  `member_savings_balance@1.0.0` and masked it, stripping the capability
  reference out of an intervention request — the single field an operator most
  needs. Over-redaction destroys evidence as surely as under-redaction leaks
  it.

Current state is clean: a sweep of all seven classes across 102 committed
files finds zero unmasked values under `evidence/`, and
`tests/test_evidence_redaction.py` enforces it. But every one of those fixes
was applied *after* the leak reached disk.

**The structural fix, not built:** make writing evidence impossible except
through the scrubbing path — a single writer that no module can bypass — and
drive it from the schema's `sensitivity` taxonomy rather than a pattern list.
Today `pii`/`secret`/`identifier` govern *declared fields* while page captures
fall back to regexes; those should be the same mechanism. Credentials are
handled correctly by construction (`credentials_ref` stores env var names, and
a pasted secret fails schema validation), which is what the rest should look
like.

---

## 7. Cuts

**Stubbed deliberately, at real seams:**

- **Operator console** — terminal prompt, assumes the human is at the same
  machine. The control-transfer model and handoff are real; the presentation
  is not.
- **Desktop surface** — `target.surface` selects a resolver; only `web`
  exists. The artifact names no web-specific concept, so the seam is real, but
  it is untested against a second surface.
- **Multi-tenant infrastructure** — files, not a database. The brief
  explicitly does not reward premature scaling infrastructure.

**Known-weak, stated plainly:**

- Discovery's emitted artifact declares `outcomes: []`. Error handling on a
  discovered capability is currently review work.
- The emitted artifact replays partly by luck of configuration: the recorder
  now emits an explicit opening `navigate` so it declares its own precondition,
  but before that fix it worked only because CoreServ's frameset default
  happened to match `entry_path`.
- One control (the nav frame's Quick Lookup box) is still nameless; its label
  sits in a different table row than the 7-rule chain reaches.

**Next, in priority order:**

1. **Evidence writing that cannot bypass redaction**, driven by the
   sensitivity taxonomy — the §6 structural fix. Four retrofits is a pattern.
2. **Per-app error-detection profiles.** The universals layer is right; the
   strings in `replay/classify.py` are one product's phrasings hardcoded in
   the engine and must move to per-app configuration before a second app.
3. **`allowed_origins` derived from `base_url`**, plus a replay-time host
   override — the §4 limitation.
4. **Re-auth on session expiry.** Currently a hard failure with the
   out-of-scope stated in the message. With the auth block already declared in
   `target`, the engine could re-run it and resume the step, turning a common
   condition from an escalation into a recoverable one.
5. **Latency budgets as policy, not per-checkpoint.** `slow_response` fails
   because a per-step 8000ms timeout is a local decision with no global view
   of the run's budget.
6. **Coordinate-to-semantic resolver** for model-native computer use. Its job
   would be to take coordinates a vision model clicked and, *before anything is
   recorded*, translate them back to the accessibility node at that location —
   so what persists is a semantic locator, never a raw coordinate. If nothing
   resolves (canvas, custom-rendered control), that is the signal to escalate
   rather than record a step that cannot survive a resize.
7. **Action-level human capture.** CDP input events resolved through
   `perception` to role+name — the same translation the recorder already does —
   which would let a human's manual fix be *promoted into the artifact* as
   recorded steps rather than merely logged. Not built partly on scope and
   partly because recording a human operating a bank's back office needs a
   retention and consent story before it needs an implementation.
