# Phase 2 measurement pass — MERIDIAN CORE

Branch `phase-2-meridian`. Nothing in the core was changed. One new file exists,
`scripts/a11y_diagnostic_meridian.py`, which is instrumentation, not core.

Raw evidence: `evidence/a11y_diagnostic_meridian/` (seven per-page dumps plus
`findings.json`, which is the machine-readable source for every number below).

Target: `https://web-sample.interface-hiring.com`, signed on as `teller1`
(branch MAIN-001). The walk was sign on → main menu → member inquiry → search
results → member record with balances → funds transfer form → transfer
**review**. Nothing was posted. `/transfer/review` renders the confirmation
screen and moves no money; the `/transfer/post` step was never issued.

---

## Why this is a second diagnostic script, not the existing one repointed

`scripts/a11y_diagnostic.py` could not be pointed at MERIDIAN by changing a
URL. It drives CoreServ's frameset with CoreServ CSS selectors
(`input[name="username"]`, `button:has-text("Login")`, `page.frame(name=...)`,
the Mary Nguyen XPath), and it scrubs through `seed_data_scrubber()`, which
imports `coreserv.data` at call time. Repointing it means rewriting its walk
and its redaction. That is itself the first coupling finding, so the walk was
written fresh alongside it rather than in place of it.

---

# TASK 1 — perception diagnostic

## 1.1 Accessible-name coverage

**Every interactive control on every page has a non-empty accessible name.
Nothing was nameless anywhere in the walk.**

| Page | raw nodes | raw tokens | filtered nodes | filtered tokens | controls | named | name_source |
|---|---:|---:|---:|---:|---:|---:|---|
| 01 sign on | 37 | 4,701 | 33 | 312 | 7 | 7 | preceding_td ×3, platform ×4 |
| 02 main menu | 57 | 7,562 | 55 | 446 | 11 | 11 | platform ×11 |
| 03 member inquiry | 39 | 4,790 | 37 | 311 | 9 | 9 | platform ×7, preceding_td ×2 |
| 04 search results | 52 | 6,866 | 49 | 398 | 10 | 10 | platform ×8, preceding_td ×2 |
| 05 member record | 151 | 20,915 | 149 | 1,164 | 8 | 8 | platform ×8 |
| 06 transfer form | 88 | 13,304 | 85 | 1,069 | 48 | 48 | platform ×44, preceding_td ×4 |
| 07 transfer review | 46 | 5,923 | 44 | 348 | 6 | 6 | platform ×6 |

Token estimates are `estimate_tokens` (chars/4) over the full JSON snapshot
versus the compact rendering. Filtering removes 93–95% of the payload; the
worst page for an LLM is the member record at ~1.2k tokens, which is
comfortable. The 48 "controls" on the transfer form are 10 real controls plus
38 `option` nodes from the two share dropdowns — `INTERACTIVE_ROLES` counts
options, which inflates the number without inflating the risk.

**The preceding-sibling-`td` rule carries, exactly as expected.** MERIDIAN uses
the same `<td class="lbl">Label:</td><td><input></td>` pattern as CoreServ and
binds no `<label for>` anywhere, so every text input and select is nameless at
the platform layer and `perception/labeling.py` rule 4 (`preceding_td`) names
all of them:

```
textbox  'Operator ID'   preceding_td      combobox 'From Share'  preceding_td
textbox  'Password'      preceding_td      combobox 'To Share'    preceding_td
combobox 'Branch'        preceding_td      textbox  'Amount'      preceding_td
combobox 'Search by'     preceding_td      textbox  'Memo'        preceding_td
textbox  'Value'         preceding_td
```

Two details worth keeping:

* `normalize_name` strips the trailing colon, so `"Amount:"` → `"Amount"`. The
  required-field markers are CSS `::before` content on `.req`, and the rule
  reads `textContent`, so the `"* "` never enters the name. If the chain were
  ever switched to Playwright's own computed name, these would become
  `"* Amount:"` and every recorded chain would break. The current rule is the
  safer one here by accident, and it should stay.
* Submit controls are `<input type="submit" value="...">`, not `<button>`. They
  still come back as role `button` with the `value` as the platform name
  (`'Sign On'`, `'Search'`, `'Continue'`, `'Post Transfer'`, `'Save Changes'`),
  so `role_name` targets them fine — but anything matching on the `<button>`
  *tag* does not. The engine's login click and interstitial dismissal both do
  (see Task 2).

## 1.2 Nothing nameless — but two ambiguities

No control resisted the rule chain. The exposure is the opposite kind: two
controls that are named identically.

* Main menu has **two links named `Sign Off`** (nav bar and menu item 9). A bare
  `role_name` rung is ambiguous there, and the resolver's ambiguity-is-a-miss
  rule turns that into a fall-through, not a wrong click. Correct behaviour,
  but it means the menu needs scoped rungs.
* Search results by last name render **N identical `Select` links**. Measured:
  `role_name(link,"Select")` → 3 matches; `role_name_scoped` with
  `scope{row, contains:"{{member_ref}}"}` → **1 match**. The CoreServ
  row-scoping pattern transfers intact.

## 1.3 The per-transaction hidden token

**It is DOM-only. It is not in the accessibility tree, in any form, on either
page.**

```
transfer form   hidden inputs: [{name:"_token", value:"59e8a5df-3a6",
                                 form_action:"/members/100234/transfer/review"}]
                token_found_in_a11y_tree: []      in compact rendering: false

review page     hidden inputs: _token, from, to, amount, memo
                                 → form_action:"/members/100234/transfer/post"
                token_found_in_a11y_tree: []
                other_hidden_values_echoed_as_visible_nodes:
                  ["100234-S0001-6","100234-S0001-12","1.00","perception diagnostic"]
                (the four field values are echoed as visible confirmation cells; the token is not)
```

Hidden inputs have no accessibility node at all, so `extract` — which resolves
role+name and reads the node's name — cannot reach it. There is no perception
path to that value today.

Three further facts that change what this costs:

1. **It is per *session*, not per transaction.** Measured: three consecutive
   loads of the same transfer form, plus the open-share form, the hold form,
   the update form and the settings form, all returned `6e2ef143-1ef` in one
   session. A second session returned `8fb0d584-f80`. The brief calls it
   per-transaction; on this deployment it is a session constant.
2. **The review page re-emits it** as a hidden input inside the post form,
   along with `from`, `to`, `amount` and `memo`. So a browser that *clicks
   through* form → Continue → Post Transfer carries the token automatically.
   The token only needs extracting if something ever POSTs directly.
3. **`/transfer/review` does not validate it.** Posting the review with
   `_token=BOGUS`, and with the field omitted entirely, both returned HTTP 200
   and the normal confirmation screen. Whether `/transfer/post` enforces it was
   deliberately not tested — that is the irreversible step.

The honest read: the token is a real gap in the *schema* (see Task 3) but not
a blocker for the click-through replay path.

## 1.4 The live clock

**It survives into the filtered tree and it does change between loads.**

```
status_bar_in_filtered_tree : true
first  : cell "OPR TELLER1 | BR MAIN-001 | 09/03/2026 04:42:17 | SID A0F11594"
second : cell "OPR TELLER1 | BR MAIN-001 | 09/03/2026 04:42:19 | SID A0F11594"
changed_between_loads       : true
whole_filtered_tree_identical: false
```

It is a single `cell` node, rendered server-side per request to the second, and
it appears on **every** page including the sign-on and every injected error
page. Consequences:

* Any checkpoint or classifier that hashes or equality-compares whole page text
  is poisoned. Nothing in the core does that today — `text_present` is a
  substring test and `classify` is substring matching — so this is a latent
  hazard, not a current break. It matters for anything added later that
  compares snapshots (`escalation/capture.py` already diffs *control lines*
  only, which sidesteps it).
* The same cell carries `SID A0F11594`, a truncated session identifier. It is
  stable within a session and lands in every evidence dump and every
  intervention request. It should be registered as a secret literal on the
  evidence scrubber the way credentials are, or it will be written to disk on
  every run.
* `OPR TELLER1` puts the operator id in the same cell — the CoreServ
  `register_secrets` reasoning ("the app renders the username into its chrome")
  applies here unchanged, and on more pages.

## 1.5 Function-key navigation

**They are inert text. They do not resolve to controls, and they have no
accessible names of their own.**

```
F3 / F5 / F7 / F12   tag: B   clickable_ancestor: null
a11y node:  cell "F3=Sign Off F5=Main Menu F7=Member Inquiry F12=Cancel"
anchors on transfer page: Main Menu, Member Inquiry, System Settings, Sign Off, Cancel
```

The markup is `<b>F3</b>=Sign Off …` inside a `<font>` in a footer `<td>`, with
no `<a>` or `<button>` ancestor anywhere. The whole row collapses into one
`cell` node whose name is the concatenated legend. This contradicts the brief's
"function-key navigation rendered as clickable links" — either the deployment
differs from the description, or the F-keys are decoration in the terminal-look
sense and the *real* navigation is the persistent nav bar, which is four
genuine links (`/menu`, `/members`, `/settings`, `/signoff`) that map one-to-one
onto F5, F7, and F3. That nav bar is the thing to target; it works.

## 1.6 Things measured that were not asked for but change the plan

**There is no frameset. The document has exactly one frame, and its name is
`""`.** Every artifact element declares `frame: "content"` and the discovery
prompt teaches the model about `content` and `navFrame`. On MERIDIAN the only
valid frame value is the empty string. `_frame_by_name("")` and the resolver's
frames dict both handle `""` correctly, so this is expressible — it just reads
badly, and every recorded chain and the whole discovery prompt need the change.

**There are zero `columnheader` nodes anywhere on this app**, which breaks a
locator strategy outright:

```
role histogram, member record:  cell 95, row 28, table 3, link 8, heading 1,
                                columnheader 0
grid header row cells:          ["Share ID","Type","Balance","Status"]  — all role "cell"
cell_in_row + column_header="Balance"   → 0 matches   (FAILS)
cell_in_row + column_index=2            → 1 match, "$40.00"   (works)
```

MERIDIAN builds grid headers from `<td>`, not `<th>`, so Playwright reports
role `cell` and `resolver._find_column_index` — which searches for a
`columnheader` — never matches. **`cell_in_row` with `column_header` is
non-functional on this target.** The committed `member_savings_balance`
artifact uses exactly that rung for `savings_balance_cell`. `column_index`
works, but it is `confidence: "medium"` and it hardcodes column order.

**Scope `contains` is a substring test, and share IDs are prefixes of each
other.** `scope{row, contains:"100234-S0001-1"}` matched **5** innermost rows
(`-12, -13, -14, -18, -19`). Scoping on a full exact share id is unambiguous;
scoping on a base share id (`100234-S0001`) is not. Any share-selection locator
needs the full id.

**MERIDIAN has real `<h1>` headings** (role `heading`, one per screen:
`MEMBER RECORD`, `FUNDS TRANSFER`, `CONFIRM FUNDS TRANSFER`, …). CoreServ had
none, which is why the existing artifact asserts `cell "Member Detail"`. Here
`role_name(heading, "CONFIRM FUNDS TRANSFER")` is available and is a much
better checkpoint. This is the one place MERIDIAN is *friendlier* than CoreServ.

---

# TASK 2 — coupling audit

Working checklist. Cost is rough effort to decouple, not to make MERIDIAN work
by hacking constants.

## Confirmed, with detail

| # | File:line | Assumes | Cost |
|---|---|---|---|
| 1 | `replay/classify.py:74-77` | CoreServ's exact error strings: `"Your session has ended."`, `"An unexpected error occurred."`, `"System Maintenance"`, dismiss control literally named `"Continue"` | **M** |
| 2 | `replay/engine.py:154-158`, `discovery/loop.py:351-354` | login form is `input[name=username]` / `input[name=password]` / `button:has-text("Login")`, targeted in engine code rather than through the element registry | **M** |
| 3 | `replay/executor.py:166-172, 234-247`, `replay/engine.py:107` | a frameset exists; `_settle` walks every frame; `_capture` looks for a frame literally named `content` | **S** |
| 4 | `capability/redaction.py:113-134` | `seed_data_scrubber()` imports `coreserv.data.MEMBERS`; on a target with no seed module there is nothing to match | **M** |
| 5 | `discovery/policy.json:17`, every artifact `policy.allowed_origins` | localhost | **S–M**, see below |

Detail where it changes the fix:

**(1)** Measured against the live pages, MERIDIAN's strings are:
`"YOUR SESSION HAS TIMED OUT"` (440), `"APPLICATION ERROR"` /
`"An unexpected error occurred while processing your request."` (500),
`"SCHEDULED MAINTENANCE IN PROGRESS"` (503). Note that
`SERVER_ERROR_MARKER = "An unexpected error occurred."` **does** substring-match
MERIDIAN's 500 page by luck. The other two do not match at all — session expiry
and the maintenance interstitial are currently invisible to the classifier.

Worse, the interstitial dismissal is structurally different. CoreServ's was a
`<button>Continue</button>` that re-rendered the same page via `return_to`.
MERIDIAN's is `<a href="/menu">Continue</a>` — a **link**, and it navigates to
the main menu, discarding your position in the flow. So
`engine._recover` fails twice over: `button:has-text("Continue")` finds nothing,
and even a fixed selector would land the run on `/menu` mid-flow. Recovery here
has to be "re-request the URL the step was on", not "click the dismiss control".

**(2)** MERIDIAN's fields are `operator` / `password` / `branch` (a select), and
the submit is `input[type=submit][value="Sign On"]`, not a `<button>`. Neither
selector matches. There is also a third credential — branch — which
`credentials_ref` has no place for, and `AuthSpec` has no notion of a
non-secret auth parameter.

**(5)** Sharper than stated. Discovery is *fine*: `discovery/run.py:52` rebuilds
`allowed_origins` from `--target`, so `--target https://web-sample…` sets the
origin correctly. The gap is **replay only**: `TargetOverride` may set
`base_url` but `_FORBIDDEN_SECTION_KEYS["policy_overrides"]["allowed_origins"]`
forbids the matching origin change, and `replay/run.py` has no host flag. An
artifact repointed at MERIDIAN fails its own origin check. This is already
documented as a known limitation in `README.md` §5 and `REPORT.md` §4.

## Not on your list

| # | File:line | Assumes | Cost |
|---|---|---|---|
| 6 | `replay/resolver.py:207-222` (`_find_column_index`) | grid headers are `<th>` → role `columnheader`. **Zero on MERIDIAN**, so `cell_in_row`+`column_header` never resolves. Not a config change — a strategy gap | **M** |
| 7 | `replay/engine.py:46-47, 122-136` | `APP_VERSION_RE = r"CoreServ\s+(\d+\.\d+\.\d+)"`. MERIDIAN prints `MERIDIAN CORE … v4.2.1`. Never matches, so drift detection silently becomes a no-op — a warning system that reports nothing looks identical to one with nothing to report | **S** |
| 8 | `discovery/prompts.py:41-45, 92, 145` | teaches the model that the app is framed, that `content` and `navFrame` exist, and that identically-named `Submit` buttons live in each. All false here. A frameless target needs the frame vocabulary made optional, not just reworded | **M** |
| 9 | `discovery/loop.py:408-410` | computes `entry` from `target.entry_path`, policy-checks it, then navigates to a hardcoded `base_url + "/home"`. MERIDIAN's post-signon landing is `/menu`. The entry path is validated and then ignored | **S** |
| 10 | `discovery/run.py:67-84` (`build_target`) | `app="coreserv"`, auth path `"/"`, `CORESERV_USERNAME`/`CORESERV_PASSWORD`, `success_check` = url matching `/home\|/search`, `--app-version 4.2.1`. Five CoreServ facts in one constructor with no CLI escape | **S–M** |
| 11 | `discovery/loop.py:162, 295`; `discovery/recorder.py:341, 516` | `frame` defaults to `"content"` in four places when the model omits it. On a frameless target the correct default is `""` | **S** |
| 12 | `escalation/request.py:114,132`, `escalation/capture.py:146`, `discovery/loop.py:236`, `scripts/a11y_diagnostic.py:51` | five call sites of `seed_data_scrubber()`. Each will silently degrade to pattern-only redaction on MERIDIAN — names, street addresses and the short `555-0142` phone form pass straight through, and nothing reports that redaction got weaker | **M**, and it is the highest-risk item on this list because it fails *quietly* |
| 13 | `escalation/request.py:139` | `next((f.url for f in page.frames if f.name == "content"), page.url)` — same content-frame assumption, falls back correctly, so harmless but wrong-headed | **S** |
| 14 | `replay/executor.py:45-54` (`_path_allowed`) | `fnmatch`, where `*` crosses `/`. `"/members/*"` therefore also permits `/members/100234/transfer/post`. On CoreServ nothing irreversible lived under the glob; on MERIDIAN it does. The allowlist can still be written safely (`/members/*/transfer` does **not** match `…/transfer/post`), but the trap is now live | **S** to fix, important to notice |
| 15 | `tests/test_replay.py:35,397-406`; `tests/test_escalation.py:31,314-323` | `CORESERV_URL` defaulting to `localhost:8800`; `coreserv_up()` gates the live tests, which **skip** rather than fail — so a MERIDIAN-only environment silently loses live coverage | **S** |
| 16 | `tests/test_evidence_redaction.py:24`, `tests/test_escalation.py:202,271` | module-level `from coreserv.data import MEMBERS`. These are hard imports, not skips: they break outright if `coreserv/` is ever dropped | **S** |
| 17 | `tests/test_replay.py:118-186, 280-294` | fixtures shaped as CoreServ trees: nested wrapper rows, `columnheader` cells, a `navFrame`/`content` pair. They test real invariants (innermost-scope, no cross-frame resolution) but encode CoreServ's DOM as the ground truth — the `columnheader` fixtures in particular assert a shape MERIDIAN never produces | **M** |
| 18 | `capabilities/member_savings_balance/*` | every element declares `frame:"content"`, the balance rung uses `column_header:"Balance"`, the heading checkpoint asserts `cell "Member Detail"`, `base_url` is localhost, `app_version` `4.2.1`, `credentials_ref` `CORESERV_*`. The tenant-overlay mechanism cannot express any of it — origin and frame are both forbidden overlay keys — so MERIDIAN is a new capability, not a tenant of this one | **M** |

**Resolver strategies specifically** (you asked): `role_name`,
`role_name_scoped` and `role_ordinal` are surface-agnostic and were verified
working against live MERIDIAN trees. `cell_in_row` is half-broken — the
`column_header` arm is dead on this target (item 6), the `column_index` arm
works. `find_scopes`' innermost rule is still correct and still necessary
(MERIDIAN nests page-shell tables the same way). `substitute` is clean.

**Evidence writing**: `EvidenceWriter` itself is target-agnostic; the coupling
is entirely in which scrubber it is handed (item 12) and in the fact that
nothing registers MERIDIAN's `SID`/`OPR` chrome values as literals (§1.4).

---

# TASK 3 — gap list

## 3.1 Mid-flow data extraction — **not expressible at all**

This is the real schema gap, and it is broader than the token.

`extract` writes into `step.into`, which the schema requires to name a declared
**output** (`schema.py:518`: *"extracts into undeclared output"*). In the
engine, extracted values land in a local `extracted: dict[str,str]`
(`engine.py:500`) that is read exactly once, at the end, to build
`result.outputs`. Meanwhile `resolver.substitute` interpolates `{{param}}` only
from `params`, which is `validated.values` — the caller's inputs, fixed before
the browser opens and never updated. **There is no write path from a step's
observation into a later step's input.** A value read on page 3 cannot be typed
on page 5.

Three MERIDIAN flows want it independently of the token:

* Read the from-share's current balance, then use it to decide or to report.
* Read the confirmation number off the post-result page — that one happens to
  fit today, because it is a terminal output rather than a later input.
* Any "select the share whose status is OPEN" logic.

What it needs: a distinction between a *declared output* (returned to the
caller) and a *flow variable* (visible to later steps' `{{...}}`), with the
substitution namespace being `params + variables`. That is a small change to
`substitute`'s call sites and a schema field; the validation rule at
`schema.py:518` has to learn about the second namespace.

For the **token specifically**, the extra problem is that hidden inputs have no
accessibility node, so even a flow-variable mechanism could not read it —
`extract` resolves through the a11y tree by design, and that design is the
project's core bet. Three honest options, in order of preference:

1. **Don't extract it.** Click through form → Continue → Post Transfer. The
   review page re-emits the token and all four field values as hidden inputs,
   so the browser carries them. This is also what a human operator does, which
   is the standard this system holds itself to. It costs nothing and needs no
   schema change.
2. Add a narrow, declared `read_hidden_field` capability to the perception layer
   — an explicit, auditable hole in the "role + accessible name only" rule,
   rather than a general DOM escape hatch. Only worth it if a flow must POST
   directly.
3. Nothing, and document that a direct-POST flow is out of scope.

Recommendation: (1), with the reasoning written down, because the bet that the
a11y tree is sufficient survives it intact — and the measurement above is what
makes that claim defensible rather than convenient.

## 3.2 Session re-auth on timeout (440) — hard failure by design, and it is now wrong

`classify.py:97-107` classifies session expiry as `hard_failure` with the
message *"Re-authentication mid-run is out of scope, so the run stops here."*
That was a defensible cut on CoreServ. On MERIDIAN it is a poor fit:

* The marker string does not match, so today a 440 is not even *detected* — the
  run sees the sign-on page and fails on a checkpoint with a misleading reason.
  That is strictly worse than the documented cut.
* Measured: `?inject=timeout` **destroys the server-side session**. Every
  subsequent request in that browser context 302s to `/signon`. So this is not
  a page to dismiss; the session is genuinely gone.
* The engine already has the two pieces re-auth needs: `_authenticate()` is a
  method, and credentials are resolved and held on `self._credentials` for the
  whole run. Re-auth is mechanically a call plus a retry.

The design question is not "can we" but **"is it safe to re-auth and retry a
step whose side effect may already have happened."** For a read step,
trivially yes. For a post step, no — see 3.4. So re-auth wants to be a
per-step property (`retry_after_reauth: bool`, default false, false on any
risky step), not an engine-wide behaviour. Detection must come first regardless:
a 440 must be recognised before it can be handled.

## 3.3 Role-gated actions — **no mechanism exists**

Place Hold needs `super1`. Measured: `teller1` can load `/members/100234/hold`
and see the whole form — the gate fires at `/hold/review`, returning HTTP 403
with `"SUPERVISOR OVERRIDE REQUIRED"` and `"Operator profile teller1 is not
authorized to perform this function."`

What is missing, in order of size:

* **One credential set per artifact.** `AuthSpec.credentials_ref` is a flat
  `{role: ENV_VAR}` dict resolved once at `engine.py:405`, before step 1.
  There is no way to say "this capability runs as a supervisor" or "this step
  needs an elevated session."
* **No operator-role concept anywhere** — not in `Target`, not in `Policy`, not
  in `Step`. A calling agent cannot ask "what privilege does this capability
  need?" and the catalogue cannot answer it. For an agent-facing capability API
  that is a contract hole, not just an implementation one.
* **No third auth field.** MERIDIAN's sign-on takes `branch`, which is neither a
  secret nor a caller input. `credentials_ref` validates every value as an env
  var name (`ENV_VAR_RE`), so branch has nowhere to live except as a fake
  credential.

Two shapes to choose between: declare the required role on the capability and
resolve a different credential set (simple, one session, and it matches how a
supervisor actually signs on at a branch), or support mid-flow elevation
(closer to a real override prompt, much more machinery). The first is right for
this sprint; the second should be named as the cut.

Separately: `Place Account Hold` is `risk: "risky"`, and there is a live bug in
that path. `check_risk` raises `PolicyViolation` under the default
`require_confirmation`; `PolicyViolation` is re-raised out of `_run_step`
(`engine.py:202-204`) and caught in `run()` (`engine.py:434`), which sets the
result and returns — **`_may_escalate`/`_escalate` are never reached.** So a
risky step marked "requires confirmation" can never actually reach a human for
confirmation; it only hard-fails. The comment at `executor.py:100-108` says it
is "routed to the human-escalation path," and it is not. That needs fixing
before Place Hold is demoable as an escalation.

## 3.4 Review → post, and the retry budget — **unsafe as it stands**

`MAX_RECOVERY_ATTEMPTS = 2` and the retry in `_run_step` re-enters the loop at
`engine.py:238` (`continue`), which re-runs `executor.execute(step, params)` —
the click itself. There are two independent paths into that retry:

* a `recoverable` detection (the maintenance interstitial), and
* a checkpoint timeout (`engine.py:288-291`), which sleeps and retries.

**Neither is idempotency-aware.** A `click` on `Post Transfer` whose checkpoint
does not settle within `timeout_ms` gets clicked again. If the first post
actually succeeded and only the confirmation was slow, that is a **double
transfer**. This is the single most dangerous thing in the core when pointed at
a target with a real post step, and it is not hypothetical: MERIDIAN's error
rate setting and the `maintenance` inject exist specifically to make slow and
interstitial responses happen.

Compounding it, the maintenance recovery is wrong here in a way that makes the
double-post more likely: the dismiss control is a link to `/menu`, so "recover
and retry" navigates away and then re-executes the step from the wrong page.

The schema has the vocabulary to fix this — `Step.risk` already exists — but the
engine does not use it in the retry decision. Minimum viable rule: **a step whose
`risk` is `risky`, or any step that is a post, is never automatically retried.**
It fails once, and it fails toward escalation with the state captured, so a
human can look at the account and decide whether it went through. Anything else
requires the app to give us an idempotency key, which it does not.

Related and smaller: `check_risk` is evaluated on `Step.risk`, which the
recorder always emits as `"safe"` (`recorder.py:352, 383`). Discovery can never
produce a step marked risky. Every irreversible step in the MERIDIAN set will
be marked risky by hand, or by a rule the recorder does not yet have.

---

---

# Core fixes applied before adaptation

Both bugs from §3.3 and §3.4 are fixed on this branch. No adaptation work was
started. Full suite: 227 passing with a live CoreServ, no regressions.

## Fix 1 — risky steps can reach a human

`check_risk` now raises `RiskBlocked`, a separate exception from
`PolicyViolation`. The two were conflated, and that is precisely what made
the escalation route unreachable: the engine aborts the whole run on a policy
violation, by design, so a risky step never got as far as `_may_escalate`.
They are different things and now have different types — a policy violation
means the run tried to do something it is not permitted to do, and no human
can authorise that after the fact; a risk block means the step is permitted
but irreversible and a person decides.

`require_confirmation` means **the operator performs the step themselves in
the live session, and the step's checkpoint verifies it landed.** Automation
never performs the action, before or after approval.

The rationale, since it is a real cut: a terminal keypress is not an
authorisation. There is no operator identity behind it and nothing tying the
approval to someone with the authority to give it, which in a regulated
financial context is worse than no gate — it looks like oversight while
providing none. Under this model the human acts with their own credentials
and their own accountability, and the system confirms the outcome rather than
trusting the report of it.

**Deliberately cut: `Decision.APPROVE` (approve, and automation executes).**
That becomes reasonable once approvals carry an authenticated operator
identity, which the mock terminal operator surface does not have.

`block` stays terminal and is not escalation-eligible — offering a human the
chance to say yes would make `block` a synonym for `require_confirmation`.
`flag` is unchanged.

The intervention request now states which step is blocked, why it is
classified risky, and what the checkpoint will verify on resume, because the
operator needs to know what "done" looks like before they act. Its
`classification` is `risk_blocked`, not `hard_failure` — nothing failed.

## Fix 2 — risky steps are never retried

A step with `risk: "risky"` is not retried by either recovery path. Both
previously re-entered at `engine.py:238` and re-ran `executor.execute`, which
means re-clicking. On an irreversible action whose result could not be
observed, a second click is how one transfer becomes two — the first may well
have posted and only its confirmation been slow.

Such a step now fails after one attempt, toward escalation, with the page
captured and a message saying the action may or may not have taken effect.
That is the honest report: whether the side effect landed is a question about
the account, and only a person looking at it can answer it.

Note the interaction with fix 1: under `require_confirmation` and `block` a
risky step is never executed at all, so this guard actually bites under
`flag`. It is built anyway rather than left to depend on policy configuration.

## Fix 3 — the recorder's risk heuristic, and what it made load-bearing

`recorder.py` always emitted `risk: "safe"`, so discovery could never produce
a risky step. A click whose resolved control name matches a post-like verb is
now recorded as risky. The vocabulary lives in `discovery/app_profiles.json`,
beside `policy.json`, because which words mean "commit" is knowledge about a
target, not about recording — a second app gets a second entry, not a code
change.

Default set: `Post`, `Confirm`, `Transfer`, `Save`, `Delete`. **`Submit` is
deliberately not in it** — it is the generic verb for sending any form,
including a search, so treating it as a commit would mark CoreServ's read-only
member lookup risky and block a capability that works today. It sits in
`near_miss_verbs` instead and is addable per-app where it does mean commit.

Matching is whole-word and case-insensitive, so `Post` does not fire on
`Postal Address`.

**This is a first guess, not a determination.** Verb matching cannot reliably
detect irreversibility in a legacy UI: the same word commits on one screen and
navigates on another, and an app is free to label its post button `OK`. The
recorder proposes; the draft → approved review disposes. That is what
`capability.status: "draft"` is for. Every match **and every near-miss** is
written into the artifact — the marked step's `notes`, and a risk summary in
`provenance.notes` — and emitted to the discovery evidence log, so a reviewer
can see what the heuristic weighed, not only what it concluded.

## The verification rule, and the third fix it forced

A step marked `risky` must declare a checkpoint. Enforced in two places:
artifact validation rejects the shape at load time, and `_escalate` refuses to
return success for such a step if one reaches the engine another way.

An unverifiable irreversible action is not an acceptable artifact. The
checkpoint is what makes the escalation model work at all — without one, "the
human did it and we resumed" is an assumption, and assuming a transfer landed
is the exact failure this design exists to prevent. Catching it at load means
it fails before a browser opens, rather than mid-run after a person has
already acted.

**This makes the risk heuristic load-bearing in a new way**, and the concern
was real: `_add_checkpoints` only attached a checkpoint when a *later* step
referenced a control, and a terminal `Post Transfer` click by definition has
no later step. The recorder would have emitted artifacts that could not load.
So a risky step with no following control now falls back to a checkpoint on
the URL the action was observed to produce, parameterised segment-wise so it
travels to the next member the capability is invoked for rather than pinning
the one it was discovered on. URL rather than page text on purpose: MERIDIAN
puts a live clock and a session id in its status bar (§1.4), so text observed
after an action is not the same text on the next run. The path is.

If no checkpoint can be derived at all, the recorder records that in
`unrecordable` and the artifact fails validation loudly — which is the correct
outcome, not a regression.

---

# The adapter seam

Six items, all landed. 273 tests pass against a live CoreServ (227 before,
46 new); the two committed artifacts load and replay unchanged.

## What moved into config

`config/app_profiles/{coreserv,meridian}.json`, resolved from
`target.app_profile` and defaulting to `target.app` — so artifacts written
before profiles existed name theirs correctly without being edited. A missing
profile is a hard error, not a default: an engine with no markers detects no
session bounce and would do it quietly, which is the failure this replaces.

Now data rather than engine constants: error markers per condition; recovery
as a typed **action** (`dismiss_control` / `reload_step_url` / `backoff`)
rather than a control name; the version regex; the frame model; the recorder's
risk vocabulary (moved out of `discovery/app_profiles.json`, which was the
right idea in the wrong file); chrome values to scrub; redaction sources; and
default auth element definitions.

## Proof the seam holds

Two independent checks.

**Structural.** `tests/test_profile.py` parses `replay/`, `perception/` and
`escalation/`, strips docstrings and comments, and fails if any application
name or app-shaped selector (`input[name=`, `button:has-text(`, a literal
`'content'`) appears in executable code. Prose may name an app — explaining
why a rule exists usually requires it. Running code may not.

**Live.** The engine drove MERIDIAN end to end with no MERIDIAN-specific code:
signed on through the element registry including the Branch select, navigated
to a member record, and extracted `share_balance = 40.00` via `cell_in_row` +
`column_header` — the rung that resolved *nothing* on this target before item
6. Origins derived to `https://web-sample.interface-hiring.com`; the session
id and operator were scrubbed from evidence by the profile's chrome rules
(verified: zero raw `SID`/`OPR` occurrences, both replacement markers
present). This used a throwaway in-memory artifact, not a recorded
capability.

## What I could not push into a profile, and why

Honest list, since "it was pure config" would be flattering and false.

**Recovery action kinds are engine code.** The profile *chooses* between
`dismiss_control`, `reload_step_url` and `backoff`; the engine implements
them. A third app needing a genuinely new mechanism — acknowledging a native
JS dialog, say — needs an engine change. That is the right boundary (an
executable action is not JSON) but it is a boundary, and the vocabulary is
currently three items wide.

**Auth control-verb dispatch is engine code.** `_fill_auth_control` picks
fill / select / check from the control's resolved accessibility role. A
sign-on needing a verb outside that set — a file upload, a multi-step SSO
redirect, a challenge-response — is engine work, not a profile entry.

**The `cell_in_row` header fallback is engine code**, deliberately, per the
brief. Teaching `_find_column_index` to read a table's first row as headers
when it has no `columnheader` is a general strategy improvement; encoding
"this app uses `<td>` headers" as a per-app flag would have been the
workaround version of the same fix.

**Signature changes rippled.** `classify()` and `detect_engine_universals()`
take a profile; `EvidenceWriter`, `capture_state`, `write_request`,
`write_activity` and `HumanActionCapture` take a profile or a content frame;
`Executor.locate` and `Executor.goto` became public so authentication and
recovery can use the resolver without being recorded steps. Unavoidable
plumbing — the values are config, but something has to carry them.

**`_run_step` now captures the URL before each attempt.** `reload_step_url`
needs somewhere to go back to, and that had to be observed before the action
rather than reconstructed after it. New engine capability, not config.

**Schema changes.** `Element.frame` became optional (null = the document,
rather than every surface having to name a frame); `AuthSpec` gained
`elements`, `submit` and `parameters`; `Policy.allowed_origins` became
derived from `base_url` and rejects a declared value that disagrees.

**`loader.apply_profile_defaults` is a compatibility path.** Artifacts
recorded before auth used the registry declare no login elements, and the
constraint was that they keep replaying unchanged. The profile supplies them
and the loader injects them under reserved keys. Applied in the engine too,
so any route in gets them. This is a bridge for existing artifacts, not a
mechanism new ones should lean on.

**Discovery's CLI defaults are still CoreServ's** — `--entry /search`, and
`build_target`'s `success_pattern` of `/home|/search`. They are arguments now
rather than literals, but their defaults name one app. `success_pattern` in
particular is per-app knowledge that arguably belongs in the profile; I left
it as a parameter because auth success is a property of the *target* an
artifact declares, and moving it would split that declaration across two
files.

## One thing that broke, found by the live check

**`{{param}}` is not substituted in a navigate step's `path`.** The live seam
check first ran with `"path": "/members/{{member_ref}}"` and navigated to that
string literally. `Step.value` is templated and validated; `Step.path` is
neither — `check_template` never inspects it, so a parameterised path passes
validation and then fails at runtime as a confusing 404.

CoreServ never exposed this because its flow reaches a member by searching and
clicking, so no path ever needed a parameter. MERIDIAN puts the member record
at `/members/{id}`, so any capability against it needs one. Not fixed here —
it is adaptation work, not seam work — but it blocks the first MERIDIAN
capability and should be the next thing done.

---

# First MERIDIAN capability

`capabilities/member_share_balance/1.0.0.json`, recorded by the discovery loop
against the live target and committed **exactly as emitted** — no hand edits.
Run `disc_c4c2cab8`, `gemini-3.1-flash-lite`.

## Templated fields: the audit found five, not one

`Step.path` was named. Auditing every field that carries caller data found
four more in the same state — read but never substituted, and never validated
against declared inputs:

| field | substituted before | validated before | now |
|---|---|---|---|
| `Step.value` | yes | yes | unchanged |
| `Scope.contains` | yes | yes | unchanged |
| `Step.path` | **no** | **no** | both |
| `Condition.text` | **no** | **no** | both |
| `Condition.pattern` | **no** | **no** | both, value regex-escaped |
| `Scope.name` | **no** | **no** | both |
| `LocatorRung.name` | **no** | **no** | both |

A checkpoint asserting `text_present: "Member {{member_ref}}"` would have
silently never matched, which is the same defect wearing different clothes.
Values substituted into a regex are `re.escape`d — a caller-supplied value is
data, not pattern syntax.

## The run

Goal reached. 7 steps attempted, 6 recorded — the dropped one is a failed
extract the model retried, which is exactly what `steps_attempted` vs
`steps_recorded` exists to keep honest.

**Did `member_ref` generalise?** Yes, and in two places. The parameter is
`member_ref` (not `value_ref` — see below), the fill value is
`{{member_ref}}`, and the balance row scope came out as
`cell_equals: "{{member_ref}}-S0001"` — parameterised *inside* a compound
identifier, which the previous whole-string comparison could not do and which
is what makes the capability portable.

**Which rungs for the balance**, in recorded order:

```
cell_in_row  scope{row cell_equals "{{member_ref}}-S0001"}  column_header Balance   high
cell_in_row  scope{row cell_equals "{{member_ref}}-S0001"}  column_index 2          medium
cell_in_row  scope{row contains "{{member_ref}}-S0001 Regular Shares"} header       medium
cell_in_row  scope{row contains "{{member_ref}}-S0001 Regular Shares"} index 2      low
role_ordinal cell index 19                                                    low, brittle
```

**Was a name-based rung suppressed?** Yes. No `role_name(cell, "$2,499.00")`
appears, though it resolved uniquely at record time. That rung is circular —
it finds the balance only while the balance is still what it was during
discovery — and `is_extraction` suppresses every name-based strategy for
exactly that reason.

**Did `frame` come out null?** Yes, on all five elements and the opening
navigate. Nothing declares `"content"`.

**Did the prompt's frame vocabulary behave?** Yes. No tool call in any of the
four MERIDIAN runs named a frame, because the frameless tool schema has no
`frame` property to name and the prompt's frames paragraph was omitted.

## Replayed on three different members, unedited

Discovered on 100234. Replayed for members it had never seen:

```
101555 -> success  balance 18015.00   (live: $18,015.00)
103001 -> success  balance   760.50   (live: $760.50)
102777 -> success  balance 41980.00   (live: $41,980.00)
```

Every step resolved on **rung 0**, confidence high — no fallback, no brittle
rung. Inputs logged as `****55`, masked by declared sensitivity.

## Four recorder bugs this exposed, all fixed

1. **Chains were built against a tree of `None`.** Snapshots are keyed by
   frame name, the main frame's name is `""`, and a frameless element's frame
   is `None` — so `frames_before.get(None)` missed and *nothing* resolved
   uniquely. The first run emitted one step and zero elements while the
   executor had resolved every control fine. The resolver already normalised
   this; the recorder did not.
2. **Extraction could be scoped on the value being read.** After an ambiguous
   scope failed, the model retried with `row_contains: "2,499.00"` — the
   balance itself. Recorded, that locator finds the balance only while it is
   unchanged. Name-based suppression did not catch it because the circularity
   was in the scope, not the name.
3. **No scope could identify a row whose id is a prefix of its siblings.**
   `contains: "100234-S0001"` matches nine rows on a member whose shares are
   suffix-numbered. Added `Scope.cell_equals` — a direct cell child matching
   exactly — and the recorder now proposes one scope per sibling cell and
   keeps whichever resolve uniquely.
4. **Outputs were named after the discovered record.** The model asked for
   `balance_100234_s0001`; an output name is the contract a calling agent
   binds to, and that one reads as a different output per member. Digit-
   bearing tokens are now stripped, matching the rule capability ids already
   follow.

Fix 4 landed after the committed artifact was recorded, and the free-tier
quota was exhausted before it could be re-recorded — so the committed artifact
still carries `balance_100234_s0001`. Left as emitted rather than hand-edited,
since editing it is exactly what "no manual editing" rules out. The next
recording will produce `balance`; `_output_name` is unit-tested.

## Two things fixed in discovery itself

**The wall clock counted provider backoff.** A run spent 300s, most of it
waiting out free-tier rate limits, and was reported as `timeout` — the
agent's behaviour blamed for the quota. Backoff is now excluded from the
budget and reported separately; each call's backoff is bounded by
`MAX_RETRIES`, so this cannot hang.

**Ambiguous and missing arrived as the same advice.** `ElementUnresolvable`
told the model to "target something that is actually present", which is right
for a missing control and actively wrong for one matched nine times — from
where the model sits, the target *is* present. Observed: a weaker model
re-sent the identical ambiguous scope three times and hit the consecutive-
failure limit with the answer on screen. It now gets told the count, that
narrowing is the fix, and that a prefix-of-siblings identifier is the usual
cause.

## A redaction hole the run itself found

Committing the evidence surfaced PII in it: the run summary read
`member 100234 (Ada Lovelace) is 20`. The profile declares
`"Lovelace, Ada"` — the surname-first form MERIDIAN renders — and literal
scrubbing is exact, so the flipped form passed straight through.

The general shape: **the model's own prose is a redaction channel.** It
restates observed values in forms nobody enumerated, and every literal-based
rule is exact by construction. `register_pii` now also registers the comma
flip, which catches the common case and is not a claim to catch every
paraphrase. Existing evidence was re-scrubbed rather than deleted.

Worth noting where this was caught: not by a test, by grepping the staging
area before committing. That is a weak control to be relying on.

## Two judgement calls worth challenging

**`parameter_aliases` in the profile.** MERIDIAN labels its member search
field `Value` — correct on screen beside a "Search by" selector, useless as a
parameter name, and the recorder derives names from labels. Without an alias
the contract reads `value_ref`. The profile now maps `Value -> member_ref`.
Which entity a generically-labelled field identifies is app knowledge, so it
sits with the app's other knowledge — but it is a per-app patch on a
heuristic, and a reviewer might prefer the recorder simply refuse to name a
parameter from a generic label.

**The goal had to be sharpened.** On "read their share balance", the lite
model read the **Shares column of the search results** — a *count* of 20 —
called it the balance, and reported success without ever opening the member
record. A wrong answer returned confidently. The goal was rewritten to name
the share and require the record. That is legitimate — a goal is the caller's
spec — but it is worth stating plainly that the first formulation produced a
confidently wrong capability, and that nothing in the system caught it. The
artifact would have replayed "successfully" forever, returning a share count
labelled as money. Discovery-time output verification is the gap.

---

## The three things that decide the shape of the sprint

1. **The adapter seam is real but it is not one seam.** Perception transfers
   essentially unchanged (100% naming, `preceding_td` carries, row scoping
   works) — the bet paid off. The coupling is concentrated in three specific
   places: the auth block, the classifier's marker strings and recovery
   actions, and the frame model. Those are worth extracting properly; the rest
   is configuration.
2. **`cell_in_row` + `column_header` is dead on this target.** That is a
   resolver change, not a config change, and it invalidates a rung in the
   committed artifact. Decide early whether to teach `_find_column_index` to
   treat a grid's first row as headers, or to lean on `column_index`.
3. **Retry on a non-idempotent post is a correctness bug waiting for a demo
   day.** Fix it before recording the transfer capability, not after.
