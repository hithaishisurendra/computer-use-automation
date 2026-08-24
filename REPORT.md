# Design write-up

An LLM works out how to operate a legacy back-office UI once. That run gets
recorded as a typed capability artifact, and the artifact replays afterwards
with no model involved.

I built it against CoreServ, a proxy target in this repo written to be
unfriendly on purpose: a real `<frameset>`, tables nested three deep, element
ids that change on every render, no test ids and seven faults I can inject on
demand.

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

The structural decision I'd defend hardest is that `perception/` sits
alongside `discovery/` and `replay/` rather than inside either one. The same
layer that hands the model a filtered snapshot during discovery is what
resolves a saved locator during replay. Adding a desktop surface means adding
a module behind that interface. Neither consumer changes.

Discovery imports `replay/executor.py` instead of having its own copy. That
means the allowlist, the risky-action rule and the ownership gate are
literally the same code on both paths, so discovery can't drift into being
more permissive than replay. There's a test asserting `discovery/loop.py`
defines no policy check of its own.

For perception I went with the accessibility tree over the DOM. Screenshots
plus coordinates was the real competitor and it generalises further, but
coordinates are about the least stable way to identify a control, and
CoreServ's hostility is aimed squarely at position-based strategies. The bet
was that role and accessible name would survive bad nesting.

I checked that bet rather than assuming it, and half of it failed. Every link
and button came back with a correct name. Every text input, select, radio and
checkbox came back nameless, because CoreServ puts label text in an adjacent
table cell and never binds it. The select was worse than empty: it inherited
its selected option as a name, which looks like a working label and is
actually the field's value. Full findings in
`evidence/a11y_diagnostic/REPORT.txt`.

I left CoreServ alone. Adding `<label for>` would have fixed every case in one
commit, but that's shaping the app to suit my strategy, and the whole premise
here is that we don't control these applications. So `perception/labeling.py`
infers the missing names from surrounding DOM through a seven-rule chain and
records which rule fired, so an inferred name is never mistaken for one the
browser supplied. Ten of eleven controls resolved. The eleventh didn't, and
I've left it reported rather than patched.

Model provider is configuration. `discovery/model.py` exposes one method with
`AnthropicClient` and `GeminiClient` behind it. I kept both, because a
one-implementation interface is really just a guess about where the boundary
goes. The runs in evidence used `gemini-3.5-flash`.

---

## 2. Artifact schema

Full spec is in `docs/artifact-schema-spec.md`. The blocks are `capability`,
`target`, `inputs`, `outputs`, `elements`, `steps`, `outcomes`, `policy` and
`provenance`.

Elements live in a registry and steps just reference them by key. That's what
makes a tenant override a small diff against the registry instead of a patch
to steps by index, and it's why section 4 is cheap.

Each element carries a chain of locator rungs ordered most to least robust,
each with a confidence and a brittle flag. Replay records which rung actually
fired, so falling through to a brittle one is a signal worth surfacing even
when the run succeeded.

There are three version fields and they're deliberately separate.
`schema_version` is the format, `capability.version` is this flow and
`target.app_version` is what the flow was recorded against. The last one is
never edited by hand. It exists to be compared against what replay observes.

Outcomes are declared rather than inferred. "No such member" is a legitimate
answer, and because the artifact says so, replay never has to guess whether
something is a result or a crash.

Two fields got added because the live app forced them. `steps[].frame`,
because navigating at the top level destroys the frameset every element lives
in. And `column_index` alongside `column_header` on `cell_in_row`, because the
member detail screen is a label/value table with no headers at all.

### Not asserting what wasn't observed

This shows up in three places and it's really one decision.

A discovered artifact comes out with `outcomes: []`. A happy-path run never
saw "no records match", so declaring that outcome would be the artifact
claiming something the run didn't establish.

`status` is always `"draft"`. Discovery doesn't get to approve its own output.

And a discovered capability has no tenant overlay. An overlay is a claim that
a flow generalises across tenants, and one run against one tenant isn't
evidence of that.

The lifecycle this points at is discover on one tenant, human review promotes
draft to approved, overlays get authored as other tenants adopt it. I designed
for that and didn't build it. What's there is the refusal to skip past it.

---

## 3. Determinism & error handling

Replay imports no model client. There's a test that parses every import in
`replay/` to prove it.

An ambiguous rung is treated as a miss, not as "take the first match". A rung
that matches three nodes has identified nothing, and grabbing `[0]` is exactly
how replay clicks the wrong row and then reports success. So ambiguity falls
through to the next rung and gets recorded. This isn't hypothetical on the
results grid: three rows means three identical "View" links, so `role_name` is
genuinely ambiguous there and the recorder skipped it in favour of a
row-scoped rung.

The circular locator problem is more interesting, because a uniqueness check
can't catch it. In the actual Gemini run the model tried to target the balance
cell as `{"role":"cell","name":"8320.10"}`
(`evidence/discovery/disc_60829c31/cycles.jsonl`). That rung is perfectly
unique on the page it was recorded from. It's also useless: it finds the
balance only while the balance is still 8320.10, which is never true for the
next member the capability gets called with. So the recorder throws away
name-based rungs for extraction targets and uses column-based ones instead.
Being unique and being correct are different things, which is why the recorder
probes strategies against the captured tree rather than trusting whatever the
model said.

Error detection happens in two layers, engine first. Session expiry, 500
pages, the maintenance interstitial and timeouts belong to the product, so
they live in the engine. `member_not_found` and `permission_denied` belong to
this flow, so they live in the artifact.

The order carries weight. When the session expires, CoreServ bounces you to a
login page that contains none of the member's data. A classifier that checked
flow outcomes first would see no results grid and confidently report "no such
member", which is a wrong answer that a caller then acts on.

All seven faults are exercised in `evidence/replay/MATRIX.txt`. Business
outcomes exit 0. The interstitial gets dismissed and the step retried. Session
expiry and 500s are hard failures flagged as escalation-eligible. Malformed
input comes back as `caller_error` without a browser ever opening.

`slow_response` still reports a hard failure and I left it that way. The fault
injects six seconds per request, which pushes the search redirect past the
artifact's own 8000ms budget. Widening the timeout would be tuning the
evidence rather than fixing anything.

### Drift signals catch what the result contract can't

Running the cascade overlay against a cascade instance on 8801 gave me a pass
that was wrong. The result said `business_outcome / member_not_found`, exit 0,
`tenant: "cascade"`, while the run was actually driving northridge on 8800.
Every field in that result was true.

What gave it away were the drift signals: app version 4.2.3 expected against
4.2.1 observed, and `member_ref_field` falling through to a brittle positional
rung because cascade's "Account Number" label wasn't on the page it was
looking at.

The result contract tells you what happened. Drift tells you whether you were
looking at the right system. Something with only the first will report
correct-looking answers about the wrong thing.

---

## 4. Heterogeneity & multi-tenant

`northridge` and `cascade` are two tenants running the same vendor product.
Same routes, same flow, different labels, different column order, different
version string. Cascade also differs semantically: it searches by a ten-digit
account number where northridge uses a five-digit member ID.

Something I didn't expect: overlay size turns out to be a diagnostic. I first
named the parameter `member_id`, after northridge's field label. That forced
an override of the results-row locator, because scoping on "the member id
column" is meaningless on a grid that doesn't have one. Renaming it to
`member_ref` made that override disappear entirely, since each tenant's grid
shows whichever identifier that tenant searches by and one chain resolves in
both.

The general lesson is that a big overlay usually means the base artifact
encoded one tenant's assumptions. Overlay size tells you about the base, not
the tenant. The recorder now derives `{entity}_ref` rather than the
tenant-specific label, so "Member ID" and "Account Number" produce the same
parameter name.

| Tier          | Example                           | Mechanism                          |
| ------------- | --------------------------------- | ---------------------------------- |
| Cosmetic      | Label text, column order          | Element chain override             |
| Configuration | Identifier semantics, host, build | Input pattern + `target` override  |
| Structural    | Different steps or outputs        | Fork via `capability.derived_from` |

The loader enforces where that line sits. Overlays can override element
chains, an input's pattern and description and `base_url`/`app_version`. They
can't touch steps, outputs, an input's name or type, or widen the policy
allowlist. Narrowing is allowed, checked with the same predicate discovery's
`--allow-path` uses. Anything past that fails with an error naming the field
and pointing at forking instead.

There's a real limitation here. An overlay can't redirect replay to another
tenant's host. It can override `base_url` but not `policy.allowed_origins`, so
the overridden host fails its own origin check, and the replay CLI has no host
override flag. The fix is small and I didn't build it: derive
`allowed_origins` from `base_url` at resolution time.
`evidence/replay/run_8dee5028` shows the same untouched artifact and overlay
reaching a genuine cascade instance, with success, no drift warnings and every
element resolving at rung 0.

On portability, the artifact mentions no Playwright, CSS, XPath or pixels.
Targets are role plus accessible name plus containing scope, which a UIA or AX
resolver could execute as-is, with `target.surface` picking the resolver.

What's genuinely CoreServ-specific is the engine's universal detection, which
is string matching on things like `"Your session has ended."` in
`replay/classify.py`. Those are one product's phrasings hardcoded into the
engine. The layering generalises fine; the strings need to move into a per-app
profile. `form_login` is the only auth mode I implemented, and its login form
is targeted in engine code rather than through the element registry. Frameset
handling is web-specific by construction.

---

## 5. Escalation & handoff

Ownership is explicit state on the session: `AUTOMATION`, `HUMAN` or
`RELEASED`. I made it explicit rather than implied by which code is running,
because those two come apart exactly when it matters, in a retry loop that
didn't notice the pause.

The ownership check sits in the executor next to the policy check, for the
same reason. A pause the caller is trusted to respect isn't really a pause.
Every action asserts ownership before it touches the page.

The operator drives the same Playwright context throughout.
`ControlledSession` captures the context identity when it's created and
re-asserts it on every transfer, so a handoff that quietly opened a new
browser fails loudly instead of handing someone a different session from the
one that got stuck. I tested it both ways: identity preserved across a
handoff, and a swapped context rejected.

When something stops, an `InterventionRequest` goes out carrying the
capability, the step, expected against observed, the URL, a screenshot and the
accessibility snapshot. Enough for an operator who wasn't watching the run.

On resume, the failed step's checkpoint gets re-evaluated and the run
continues from there. Restarting would repeat completed steps and their side
effects, and could undo whatever the operator just fixed.

`evidence/escalation/run_61d94a77` ended up exercising the harder case by
accident. The operator's fix was partial: clearing the fault flag didn't
repaint a page that had already rendered, so the condition persisted and a
second intervention fired. The control trail reads automation, human,
automation, human, automation, released, and the run finished with every step
executed exactly once. Two interventions, nothing repeated. That's the
property the resume design exists to guarantee, and it got tested under a
partial fix rather than an idealised one.

What I mocked: the operator surface is a terminal prompt that assumes the
human is sitting at the same machine. A real console would replace
`ConsoleOperator` and nothing else, delivering the same session and the same
request over CDP screencast. Capture is effect-level, meaning URL and
accessibility-state diff rather than keystrokes.

---

## 6. Safety

Policy is enforced at the executor boundary, immediately before an action
runs, never in the prompt. A guardrail in a prompt is a suggestion. The
allowlist covers origins, path globs and action types, and it excludes
`/_faults` so the agent can't manipulate the app's own fault state.

The allowlist was escapable by timing, and my own policy test is what caught
it. Enforcement was checking the URL after each action, but that check races
the app: clicking a link returns before the navigation lands, so the frame
still reports its previous URL. I wrote a test narrowing `allowed_paths` to
exclude `/member/*`, expected a block, and got `success`. Enforcement moved
into `_capture`. The invariant went from "check after actions", which is only
as good as your enumeration of the code paths, to "no page is observed without
validating where it is", which is a statement about states.

Risky steps get blocked under `require_confirmation` rather than proceeding,
since unattended replay has no channel to confirm through. That's what
escalation is for.

Redaction is the weakest part of this system. It failed in both directions,
four times, and every fix was a retrofit. The pattern matters more than the
individual bugs.

Three of them were under-redaction. The a11y diagnostic wrote a seed member's
full SSN, DOB, phone, email and address into committed evidence. Discovery's
evidence writer used a pattern-only scrubber that caught SSNs by shape but
missed names and addresses. Both of those capture whole pages, so sensitive
values show up as page content and never as declared fields, which leaves
sensitivity-driven masking with nothing to act on.

The fourth was the opposite. My email pattern matched
`member_savings_balance@1.0.0` and masked it, which stripped the capability
reference out of an intervention request. That's the single field an operator
most needs. Over-redaction destroys evidence just as effectively as
under-redaction leaks it.

Where it stands now: a sweep of all seven classes across the repo finds zero
unmasked values under `evidence/`, and `tests/test_evidence_redaction.py`
keeps it that way. But every one of those fixes landed after the leak had
already reached disk.

The structural fix, which I didn't build: make writing evidence impossible
except through the scrubbing path, and drive it off the schema's
`sensitivity` taxonomy instead of a pattern list. Right now `pii`, `secret`
and `identifier` govern declared fields while page captures fall back to
regexes, and those should be one mechanism. Credentials are already handled
properly by construction, since `credentials_ref` stores env var names and a
pasted secret fails validation. That's the shape the rest should have.

---

## 7. Cuts

Stubbed at seams I think are real:

The operator console is a terminal prompt that assumes the human is at the
same machine. The control-transfer model underneath is real; the presentation
isn't.

The desktop surface doesn't exist. `target.surface` selects a resolver and
only `web` is implemented. The artifact mentions no web-specific concept, so I
believe the seam holds, but it's untested against a second surface.

Multi-tenant infrastructure is files rather than a database.

Known weaknesses, stated plainly:

Discovered artifacts come out with `outcomes: []`, so error handling on a
discovered capability is currently review work.

One control, the nav frame's Quick Lookup box, is still nameless. Its label
sits in a different table row than my seven-rule chain reaches.

And before the recorder started emitting an explicit opening `navigate`,
replay only worked because CoreServ's frameset default happened to match
`entry_path`. That's fixed now, but it was luck for a while.

What I'd build next, in order:

1. Evidence writing that can't bypass redaction, driven by the sensitivity
   taxonomy. Four retrofits is a pattern, not four bugs.
2. Per-app error-detection profiles. The layering is right, but the strings in
   `replay/classify.py` need to move into configuration before a second app
   exists.
3. `allowed_origins` derived from `base_url`, plus a replay-time host
   override.
4. Re-auth on session expiry. It's a hard failure today. The auth block is
   already declared in `target`, so the engine could re-run it and resume,
   which would turn a common condition from an escalation into a recoverable
   one.
5. Coordinate-to-semantic resolution, if model-native computer use were added.
   Translate a clicked coordinate back to the accessibility node at that
   location before anything gets recorded, so what persists is semantic. If
   nothing resolves, that's the signal to escalate rather than record a step
   that won't survive a window resize.
6. Action-level capture of what a human did during a handoff. CDP input events
   resolved through `perception` into role and name, which would let a manual
   fix be promoted into the artifact as steps instead of just logged. I left
   it out partly on scope, and partly because recording a human operating a
   bank's back office needs a retention and consent story before it needs an
   implementation.
