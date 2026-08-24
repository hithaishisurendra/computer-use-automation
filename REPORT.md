# Design write-up

An LLM works out how to operate a legacy back-office UI once. That run becomes
a typed capability artifact, which then replays with no model involved.

I built it against CoreServ, a proxy target in this repo written to be
unfriendly on purpose: a real `<frameset>`, tables nested three deep, element
ids that change on every render, no test ids and seven injectable faults.

---

## 1. Architecture

```
coreserv/     the target app
perception/   accessibility-tree snapshot, filtering, label augmentation
capability/   artifact schema, loading, validation, redaction
discovery/    LLM loop + recorder          ─┐ both import perception
replay/       deterministic engine         ─┘ neither imports the other
escalation/   control transfer, operator surface
```

`perception/` sits alongside `discovery/` and `replay/` rather than inside
either. The same layer that hands the model a filtered snapshot during
discovery resolves a saved locator during replay. A desktop surface would be a
new module behind that interface, and neither consumer changes.

Discovery imports `replay/executor.py` rather than copying it, so the
allowlist, risky-action rule and ownership gate are the same code on both
paths. A test asserts `discovery/loop.py` defines no policy check of its own.

I chose the accessibility tree over the DOM. Screenshots plus coordinates
generalises further, but coordinates are the least stable way to identify a
control and CoreServ is built to punish position-based strategies. The bet was
that role and accessible name survive bad nesting.

I checked it, and half failed. Links and buttons all came back correctly
named. Every text input, select, radio and checkbox came back nameless,
because CoreServ puts label text in an adjacent cell and never binds it. The
select was worse than empty: it inherited its selected option as a name, which
looks like a label and is actually the value.

I left CoreServ unpatched. Adding `<label for>` would have fixed every case in
one commit, but that is shaping the app to fit my strategy, and the premise is
that we don't control these applications. `perception/labeling.py` infers
names from surrounding DOM through a seven-rule chain and records which rule
fired, so an inferred name is never mistaken for a real one. Ten of eleven
resolved; the eleventh stays reported.

Provider is configuration. `discovery/model.py` has one method with Anthropic
and Gemini clients behind it. Runs used `gemini-3.5-flash`.

---

## 2. Artifact schema

Full spec in `docs/artifact-schema-spec.md`.

Elements live in a registry and steps reference them by key, so a tenant
override is a diff against the registry rather than a patch to steps by index.

Each element carries a chain of locator rungs ordered most to least robust,
each with a confidence and a brittle flag. Replay records which rung fired, so
falling through to a brittle one is a drift signal even on a run that
succeeded.

Three version fields, deliberately separate: `schema_version` is the format,
`capability.version` is this flow, and `target.app_version` is what the flow
was recorded against. The last is never hand-edited. It exists to be compared
against what replay observes.

Outcomes are declared rather than inferred, so replay never has to guess
whether "no such member" is a result or a crash.

Two fields the live app forced: `steps[].frame`, because top-level navigation
destroys the frameset every element lives in, and `column_index` alongside
`column_header` on `cell_in_row`, because the member detail screen is a
label/value table with no headers.

**Not asserting what wasn't observed.** Three places, one decision. A
discovered artifact comes out with `outcomes: []`, because a happy-path run
never saw "no records match". `status` is always `draft`; discovery doesn't
approve its own output. And a discovered capability gets no tenant overlay,
because an overlay claims a flow generalises across tenants and one run
against one tenant isn't evidence of that. The lifecycle this implies is
designed for and not built.

---

## 3. Determinism & error handling

Replay imports no model client, and a test parses every import in `replay/` to
prove it.

An ambiguous rung is a miss, not a first match. Grabbing `[0]` is exactly how
replay clicks the wrong row and reports success, so ambiguity falls through to
the next rung and gets recorded. Not hypothetical on the results grid: three
rows means three identical "View" links, so the recorder skipped `role_name`
in favour of a row-scoped rung.

Circular locators are the interesting case, because a uniqueness check can't
catch them. In the real Gemini run the model targeted the balance cell as
`{"role":"cell","name":"8320.10"}`
(`evidence/discovery/disc_60829c31/cycles.jsonl`). That is unique on the page
it was recorded from and useless: it finds the balance only while the balance
is still 8320.10. The recorder discards name-based rungs for extraction
targets and uses column-based ones instead.

Detection runs in two layers, engine first. Session expiry, 500s, the
maintenance interstitial and timeouts belong to the product. `member_not_found`
and `permission_denied` belong to this flow. The order matters: when the
session expires CoreServ bounces to a login page containing none of the
member's data, so a flow-first classifier would report "no such member", a
wrong answer the caller acts on.

All seven faults are exercised in `evidence/replay/MATRIX.txt`. Business
outcomes exit 0, the interstitial is dismissed and the step retried, session
expiry and 500s are escalation-eligible hard failures, and malformed input is
`caller_error` with no browser opened. `slow_response` still reports a hard
failure: six seconds per request exceeds the artifact's own 8000ms budget, and
widening it would be tuning the evidence.

**Drift catches what the result contract can't.** Running the cascade overlay
against an instance on 8801 gave me a pass that was wrong:
`business_outcome / member_not_found`, exit 0, `tenant: cascade`, while
actually driving northridge on 8800. Every field in that result was true. What
exposed it were the drift signals: version 4.2.3 expected against 4.2.1
observed, and `member_ref_field` falling to a brittle positional rung because
cascade's "Account Number" label wasn't on the page. The contract tells you
what happened; drift tells you whether you were looking at the right system.

---

## 4. Heterogeneity & multi-tenant

`northridge` and `cascade` are two tenants of one vendor product. Same routes
and flow, different labels, column order and version. Cascade also differs
semantically, searching by ten-digit account number where northridge uses a
five-digit member ID.

Overlay size turned out to be a diagnostic. I first named the parameter
`member_id`, after northridge's field label, which forced an override of the
results-row locator, since scoping on "the member id column" is meaningless on
a grid without one. Renaming it to `member_ref` removed that override
entirely. A large overlay usually means the base encoded one tenant's
assumptions. The recorder now derives `{entity}_ref` rather than the
tenant-specific label.

| Tier          | Example                           | Mechanism                          |
| ------------- | --------------------------------- | ---------------------------------- |
| Cosmetic      | Label text, column order          | Element chain override             |
| Configuration | Identifier semantics, host, build | Input pattern + `target` override  |
| Structural    | Different steps or outputs        | Fork via `capability.derived_from` |

The loader enforces that line. Overlays may override element chains, an
input's pattern and description, and `base_url`/`app_version`. They may not
touch steps, outputs, an input's name or type, or widen the allowlist;
narrowing only. Anything past that errors and points at forking.

One real limitation: an overlay can't redirect replay to another tenant's
host. It can override `base_url` but not `policy.allowed_origins`, so the new
host fails its own origin check, and the replay CLI has no host override. The
fix is small and unbuilt, deriving `allowed_origins` from `base_url` at
resolution time. `evidence/replay/run_8dee5028` is the same untouched artifact
and overlay against a genuine cascade instance: success, no drift warnings,
every element at rung 0.

The artifact mentions no Playwright, CSS, XPath or pixels, so a UIA or AX
resolver could execute it unchanged, with `target.surface` picking the
resolver. What is CoreServ-specific: engine detection is string matching on
phrases like `"Your session has ended."` in `replay/classify.py`, which belong
in a per-app profile; `form_login` is the only auth mode and targets its form
in engine code rather than the element registry; frameset handling is
web-specific by construction.

---

## 5. Escalation & handoff

Ownership is explicit state on the session: `AUTOMATION`, `HUMAN` or
`RELEASED`. Explicit rather than implied by which code is running, because
those two come apart in a retry loop that didn't notice the pause.

The ownership check sits in the executor next to the policy check. A pause the
caller is trusted to respect isn't a pause.

The operator drives the same Playwright context. `ControlledSession` captures
context identity at construction and re-asserts it on every transfer, so a
handoff that quietly opened a new browser fails loudly. Tested both ways:
identity preserved, and a swapped context rejected.

An `InterventionRequest` carries the capability, the step, expected against
observed, the URL, a screenshot and the accessibility snapshot. On resume the
failed step's checkpoint is re-evaluated and the run continues from there.
Restarting would repeat side effects and could undo whatever the operator just
fixed.

`evidence/escalation/run_61d94a77` hit the harder case by accident. The
operator's fix was partial, since clearing the fault flag didn't repaint an
already-rendered page, so a second intervention fired. The trail reads
automation, human, automation, human, automation, released, and every step
executed exactly once.

Mocked: the operator surface is a terminal prompt assuming the human is at the
same machine. A real console replaces `ConsoleOperator` and nothing else, over
CDP screencast. Capture is effect-level, not keystrokes.

---

## 6. Safety

Policy is enforced at the executor boundary immediately before an action runs,
never in the prompt. The allowlist covers origins, path globs and action
types, and excludes `/_faults` so the agent can't touch the app's fault state.

The allowlist was escapable by timing, and my own policy test caught it.
Enforcement checked the URL after each action, but clicking a link returns
before the navigation lands, so the frame still reported its previous URL. I
narrowed `allowed_paths` to exclude `/member/*`, expected a block and got
`success`. Enforcement moved into `_capture`, which takes the invariant from
"check after actions", only as good as your enumeration of code paths, to "no
page is observed without validating where it is".

Risky steps are blocked under `require_confirmation`, since unattended replay
has no channel to confirm through. That's what escalation is for.

Redaction is the weakest part of this system. It failed in both directions,
four times, and every fix was a retrofit. Three were under-redaction: the a11y
diagnostic wrote a seed member's full SSN, DOB, phone, email and address into
committed evidence, and discovery's writer used a pattern scrubber catching
SSNs by shape but not names or addresses. Both capture whole pages, so
sensitive values arrive as content rather than declared fields, leaving
sensitivity-driven masking nothing to act on. The fourth was the opposite: my
email pattern matched `member_savings_balance@1.0.0` and masked it, stripping
the capability reference out of an intervention request.

It's clean now, enforced by `tests/test_evidence_redaction.py`, but every fix
landed after the leak reached disk. The structural fix, unbuilt: make writing
evidence impossible except through the scrubbing path, driven by the schema's
`sensitivity` taxonomy rather than a pattern list. Credentials already work
that way, since `credentials_ref` stores env var names and a pasted secret
fails validation.

---

## 7. Cuts

Stubbed at seams I think are real: the operator console is a terminal prompt,
though the control-transfer model underneath is not; the desktop surface
doesn't exist, since `target.surface` selects a resolver and only `web` is
implemented; and storage is files rather than a database.

Known weaknesses. Discovered artifacts come out with `outcomes: []`, so
declaring error handling on a discovered capability is review work. One
control, the nav frame's Quick Lookup box, is still nameless because its label
sits in a different table row than my seven rules reach. And before the
recorder emitted an explicit opening `navigate`, replay worked only because
CoreServ's frameset default happened to match `entry_path`.

What I'd build next, in order: evidence writing that can't bypass redaction;
per-app error-detection profiles, since the layering is right but the strings
in `replay/classify.py` aren't portable; `allowed_origins` derived from
`base_url`; re-auth on session expiry, which the already-declared auth block
makes cheap; and coordinate-to-semantic resolution if model-native computer
use were added, translating a clicked coordinate back to the accessibility
node before anything is recorded.

I left action-level capture of what the human did out partly on scope, and
partly because recording someone operating a bank's back office needs a
retention and consent story before it needs an implementation.
