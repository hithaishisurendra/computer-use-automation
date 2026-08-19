# Design decisions log

Running log of decisions made before/alongside implementation, with the reasoning,
so REPORT.md doesn't have to be reconstructed from memory later. Newest at bottom.

## Directory layout: perception/ as a shared seam, not owned by either side

```
coreserv/          the target app (proxy surface CoreServ operates on)
capability/        artifact schema, loading, validation
discovery/         LLM-driven observe/decide/act loop, uses perception
replay/            deterministic replay engine, uses perception
perception/        accessibility-tree snapshot, filtering, locator resolution
evidence/          runtime output (logs, screenshots, saved artifacts)
scripts/
docs/
```

The load-bearing choice is that `perception/` is a peer of `discovery/` and
`replay/`, not nested inside either. Both import it; neither imports the
other. That's a direct answer to the section 3.7 question ("what's the seam
between how we perceive/act on a surface and the recorded flow?") made
visible in the file tree instead of only in prose: the same perception layer
that hands the LLM a filtered accessibility snapshot during discovery is what
resolves a saved locator back to a live element during replay. If a second
surface type (legacy web, desktop) gets added later, it's a new module behind
the same perception interface — discovery and replay don't change.

`capability/` is split out from both because the artifact schema is a
contract two different producers/consumers share (discovery writes it,
replay reads it, a human reviews it) — it shouldn't live inside whichever
module happens to run first.

Considered and rejected: nesting perception under `agent/` (e.g.
`agent/perception.py`) alongside the discovery loop. Rejected because it
implies replay either duplicates perception logic or reaches into `agent/`
to get it — the wrong dependency direction for the one piece of this system
meant to outlive any single surface.

## Perception strategy: accessibility tree over screenshot+coordinates

Competing options: (a) screenshot + vision-model coordinate grounding, (b)
raw DOM traversal, (c) accessibility tree (role + accessible name).

Screenshot+coordinates was the strongest competitor — it's surface-agnostic
in the sense that it doesn't care whether there's a DOM at all, which matters
for the eventual desktop-app case. It was cut for the *replay* path because
coordinates are the least stable locator available: pixel positions shift
with viewport size, zoom, font rendering, and any layout change, and CoreServ's
own hostility rules (rotating ids, meaningless classes, no test ids) are
explicitly designed to break naive selector strategies, not to break role/name
matching. Raw DOM traversal was cut because CoreServ's markup is deliberately
adversarial at that layer — nested tables, meaningless classes, rotating ids
are exactly the surface a DOM-position or CSS-selector locator would break on.

The accessibility tree wins for this environment specifically because the
spec's hostility rules were designed with that seam in mind: "Role and
accessible name survive terrible nesting." A login button is `role=button,
name="Login"` whether it's the second `<td>` in the third nested `<table>`
this render or the fifth next render. That's a property of enterprise
server-rendered apps generally (ASP.NET/JSP emit ugly markup with genuine
controls), not just of this proxy target, so it should generalize.

## Model-native computer use: considered, cut for the recorded artifact

Also considered: using a model-native computer-use tool (e.g. a CUA-style
tool that takes screenshots and emits coordinate actions natively) as the
*entire* mechanism, both for discovery and replay. Cut as the primary
mechanism because it collapses perception and action into an opaque
image-in/coordinates-out step that an artifact can't cleanly capture — a
recorded step becomes "click at (x, y) on a screenshot that no longer
exists" rather than "click the control with role=button, name=Submit."
That's undebuggable and non-portable across a resized window, let alone
across tenants.

Where it's kept: as a bounded, explicitly-scoped *escape hatch*, not the
default path. The designed extension point is a coordinate-to-semantic
resolver — if a model-native tool is used for a single stuck step (e.g. the
optional "assisted fallback" stretch goal), the resolver's job is to take
the coordinates the model clicked and, before anything is recorded,
translate them back to the accessibility-tree node at that location (role +
name + ancestry) so what gets persisted is still a semantic locator, never a
raw coordinate. If no node resolves at that point (canvas, custom-rendered
control), that's the signal to fail the step out to a human rather than
silently record a coordinate-based step that can't survive a resize.

## Custom hostile app over a public sandbox

A public demo/sandbox site was the alternative (spec explicitly allows it).
Rejected for three reasons specific to what this project needs to
demonstrate, not because public sandboxes are bad in general:

1. **Fault injection needs a control plane.** The interesting replay
   failures per the brief are runtime conditions (validation error, not-found,
   permission denial, session timeout, slow load, server error), not layout
   drift. A public site can't be told to deterministically return
   "restricted member" on demand — you'd be stuck waiting for or scripting
   around real error conditions, or faking them client-side in a way that
   doesn't actually exercise the replay engine's detection logic. CoreServ's
   `/_faults` endpoint makes those conditions reproducible on command, which
   is what lets a replay run be shown hitting a real, server-asserted error
   state rather than a scripted one.
2. **Cross-tenant reuse needs a second real variant.** Section 3.7 asks for a
   credible story on reusing one artifact across tenants running the same
   vendor product with different branding/config. A public site is exactly
   one tenant. The `northridge`/`cascade` split (same routes and flow,
   different labels, column order, and version string) is a direct stand-in
   for that, and it's only possible because the app is owned.
3. **Terms/rate-limit risk is eliminated.** No public site's ToS has to be
   checked or respected, and there's no risk of hammering a shared resource
   during iterative discovery/replay development.

## Hostile in structure, real in controls — why that split is faithful, not rigged

The spec draws a deliberate line: markup nesting/naming/ids are adversarial,
but every control is a genuine `<a>/<button>/<input>/<select>` with visible
text, never a clickable `<div>`/`<td>`. This could look like it's giving the
automation an easy out relative to a "real" hostile app. It isn't, for a
concrete reason: it matches how legacy enterprise apps actually get
generated. Server-rendered ASP.NET/JSP/Struts-era UIs are ugly at the
*layout* layer because that's what template-driven table layouts and
generated ids produce, but they're still built from real HTML form controls,
because the browser (and screen readers) need genuine controls to submit
forms and be operable at all. A framework doesn't generate a `<div
onclick>` masquerading as a button in that era — it generates an actual
`<button>` or `<input type=submit>`, just wrapped in three tables of
formatting cruft. So "hostile structure, real controls" isn't a concession
to make the automation's job easier — it's what makes the accessibility-tree
perception strategy the *correct* choice rather than a lucky one: the thing
being bet on (role + accessible name survive the nesting) is true of the
real environment this is standing in for, not just of this proxy.

## Two spec-silent calls (flagged during CoreServ build)

The target-app spec didn't fully specify these; both were exercised and
verified in the running app before being logged here.

- **`branch` field on each member.** The spec's "Each member has" list
  doesn't include it, but both tenants' results-table column sets include a
  Branch column ("Member ID, Name, Status, Branch" / "Name, Account Number,
  Branch, Status"), so a value has to exist somewhere to render. Added as a
  plain string field (`Downtown`/`Eastside`/`Westgate`) alongside the listed
  fields.
- **Search form wiring.** The spec fixes the *routes* (`/search`,
  `/search/results`) but not the search form's own field names or how a
  last-name search relates to an id-based lookup. Implemented as two
  optional fields on a self-posting form (`last_name`, `identifier`) —
  `identifier` matches exactly against `member_id` on northridge or against
  any account's `account_number` on cascade (per the tenant's id_label);
  `last_name` does a case-insensitive substring match. If `identifier` is
  present it takes priority over `last_name`. The nav frame's quick-lookup
  box reuses the same `identifier` param directly against
  `/search/results`, skipping the intermediate form page.

## Artifact schema: three decisions that changed the spec

Full reasoning lives in docs/artifact-schema-spec.md; recorded here because
each one overrode what the initial plan assumed.

- **`member_ref`, not `member_id`.** Cascade searches by a ten-digit
  account number where northridge searches by a five-digit member ID, and
  cascade's results grid has no Member ID column at all. Naming the
  parameter after northridge's concept would have forced either a
  misleading name or a forked capability on the second tenant. Renaming it
  also fixed the row-scoping locator for free: `contains: "{{member_ref}}"`
  resolves in both tenants, because each tenant's grid displays whichever
  identifier that tenant searches by, so `results_view_link` needs no
  per-tenant override.
- **Overlays may override inputs, but only the non-contractual parts.**
  The initial plan had element-only overlays. That is not sufficient for a
  tenant whose identifier *means* something different. Overlays may now
  specialise an input's pattern/description/example, any element chain, and
  base_url/app_version; they may not touch steps, outputs, an input's
  name/type/required/sensitivity, or widen the policy allowlist. Reaching
  past that boundary fails with an error pointing at forking, and
  `capability.derived_from` exists to record the ancestry when it happens.
  The rule is: overlays express configuration drift, forks express
  behavioural divergence.
- **Auth is a target block, not recorded steps.** CoreServ bounces
  unauthenticated requests, so the flow could not actually start at
  `entry_path` as specced. Login lives in `target.auth` and runs before
  step 1; `credentials_ref` holds environment variable *names* so the
  artifact stays safe to commit publicly, and the schema rejects anything
  in that field that doesn't look like a variable name. `auth_failure` is
  a distinct result classification from business outcomes, recoverable
  conditions and hard failures: it means our own configuration is wrong,
  which is neither the caller's problem nor retryable.

## Replay engine: four things the live app forced

All four were found by running against CoreServ, not by reasoning about it.

- **Policy must be enforced on every observation, not once per action.**
  The first implementation checked the URL immediately after each action.
  That check races the app: clicking a link returns before the navigation
  lands, so the frame still reports its *previous* URL and an
  off-allowlist destination sails straight through. A test that narrowed
  `allowed_paths` to exclude `/member/*` passed the run as `success` --
  the allowlist was describing intent, not behaviour. Enforcement now also
  runs inside `_capture`, so a page cannot be observed without validating
  where it is. The pre-navigation check stays too: refusing before the
  request is issued is still better than catching it after.
- **Engine universals must be checked before artifact outcomes, including
  during auth.** With the `server_error` fault on, the auth assertion fails
  and the run was reporting `auth_failure` -- pointing an operator at
  credentials that were perfectly fine when the real answer was that the
  app was returning 500s. The auth path now runs the same universal
  detection the step loop does. The same ordering protects the step loop
  from reporting "no such member" when the session simply expired.
- **Recovery must wait for the recovery to land.** Dismissing the
  maintenance interstitial triggers a POST-redirect that is still in flight
  when the step retries, so the retry's navigation raced it and the browser
  reported an aborted navigation -- surfacing as a driver error on a run
  that was recovering correctly. Recovery now polls until the dismissal
  control is gone, which is the observable signal that the re-render
  landed. Enumerating browser error strings was the alternative and is
  fragile; it is kept only as a secondary guard.
- **Any unexpected driver error still leaves through the result contract.**
  A raw Playwright exception escaping as a traceback breaks the promise
  that a caller always gets one typed result. Unknown exceptions become a
  `hard_failure` with the step id and the message; `PolicyViolation` is
  deliberately re-raised rather than softened, because it must abort the
  run rather than become one step's outcome.

Also settled here: `slow_response` is reported as a hard failure rather than
being made to pass. The fault injects 6s per request, so the search step's
redirect chain exceeds the 8000ms checkpoint budget the artifact itself
declares. Widening the timeout to make the row green would be tuning the
evidence rather than the system; the honest reading is that the artifact's
declared budget is being enforced.
