# Phase 2 design decisions

Same format as `docs/decisions.md`: what was decided, why, and what was
considered and rejected. The rejected line is the one that matters — it is
where the "why not X" answers live. Newest at bottom. Entries are written in
the session the decision is made, not batched.

Phase 2 pointed the phase-1 core at MERIDIAN CORE, a live legacy
credit-union console.

---

## Diagnostic before adaptation

Measured the coupling against the live target before changing anything —
perception coverage, locator behaviour, error markers, the hidden token, the
frame model — and wrote it down as `docs/phase2-diagnostic.md`.

The question the phase-2 brief actually asks is "how cleanly did your core
adapt". That is only answerable if the *before* state is on record. Patching
until it works and then describing the result reconstructs the answer from
memory, and the reconstruction is always flattering: every fix looks
inevitable in hindsight and the things that were nearly wrong disappear.

Considered and rejected: adapt first and write the report from the diff.
Rejected because a diff shows what changed, not what was hard, and not what
was *nearly* changed and shouldn't have been. It also loses the measurements
— that MERIDIAN has zero `columnheader` nodes, that the token is DOM-only —
which are the evidence behind the design choices, not decoration on them.

**A sibling diagnostic script, not the existing one repointed.**
`scripts/a11y_diagnostic.py` could not be pointed at a new URL: it drives
CoreServ's frameset with CoreServ selectors and scrubs through
`seed_data_scrubber()`, which imports `coreserv.data`. Repointing meant
rewriting its walk and its redaction. Considered and rejected: rewriting it in
place. Rejected because that fact — that the "just change the URL" script
could not have its URL changed — was itself the first coupling finding, and
editing it away would have destroyed the evidence for it.

---

## RiskBlocked is a separate type from PolicyViolation

Two exception types where there was one. `PolicyViolation` means the run tried
to do something it is not permitted to do. `RiskBlocked` means the step is
permitted but irreversible and policy says a person decides.

They are different events and they need different handlers. A policy violation
aborts the run — nobody can authorise it after the fact. A risk block is the
guardrail working, and it has to be able to reach a human. Sharing one type
meant the engine's abort handler caught both, so `_may_escalate` was never
consulted and a step marked "requires confirmation" could only hard-fail. The
comment claiming it "routed to the human-escalation path" was false.

Considered and rejected: keeping one type and branching on `kind` at the
catch site. Rejected because that is what the bug already was — the
distinction existed as data inside an exception whose *type* said "abort", and
the handler that mattered read the type. Same class as the phase-1 bug where
`PolicyViolation` was caught one level too high: a distinction that only
exists in a field is a distinction the control flow ignores.

---

## Risky steps escalate by having the human perform them

Under `require_confirmation`, automation never performs the irreversible
action. The run stops, hands the live session to the operator, and the
operator does it themselves; on resume the step's checkpoint verifies it
landed. The system confirms the outcome rather than trusting a report of it.

Considered and rejected: `Decision.APPROVE`, where the operator approves and
automation executes. Rejected because a terminal keypress is not an
authorisation. There is no operator identity behind it and nothing tying the
approval to someone with the authority to give it — in a regulated context
that is worse than no gate, because it looks like oversight while providing
none. Under the chosen model the human acts with their own credentials and
their own accountability.

Named as a deliberate cut: approve-and-execute becomes reasonable once
approvals carry an authenticated operator identity, which the mock terminal
operator surface does not have.

---

## Risky steps are never automatically retried

A step with `risk: "risky"` is not retried by either recovery path. It fails
after one attempt, toward escalation, with the page captured.

Retrying means re-running the step, and re-running a step means clicking the
control again. On an irreversible action whose result could not be observed, a
second click is how one transfer becomes two — the first may well have posted
and only its confirmation been slow. Whether the side effect landed is a
question about the account, and only a person looking at it can answer it.

Considered and rejected: retrying with an idempotency key. Rejected because
the app does not offer one — there is nothing to key on, and inventing a
client-side one would not stop the server from posting twice.

---

## Risk verb list, and what it is not

`Post, Confirm, Transfer, Save, Delete`, per app profile, matched whole-word
and case-insensitively against a clicked control's resolved accessible name.

`Submit` is deliberately excluded. It is the generic verb for sending any
form, including a read-only search — CoreServ's member lookup submits with a
button named `Submit`, and marking that risky would block a working read-only
capability under `require_confirmation`. It sits in `near_miss_verbs` instead,
so a reviewer can see it was considered and rejected rather than overlooked.

Considered and rejected: a "no extract step follows" heuristic for detecting
commits. Rejected because it is wrong in the obvious case — the transfer flow
has no extract after `Post Transfer` either, so it separates terminal steps
from non-terminal ones rather than committing ones from navigating ones. A
rule whose behaviour is hard to predict from reading it is worse than a
vocabulary list.

**This is a first guess, not a determination.** Verb matching cannot detect
irreversibility in a legacy UI: the same word commits on one screen and
navigates on another, and an app is free to label its post button `OK`.
Confirmed in the first live transfer recording, which marked a *navigation
link* named "Funds Transfer" as risky. The actual control is
`capability.status: "draft"` plus human approval; the verb list only decides
what gets flagged for that review.

---

## A risky step with no checkpoint is rejected at load and refused at runtime

Artifact validation rejects a risky step that declares no checkpoint, and
`_escalate` refuses to return success for one if it reaches the engine another
way.

An unverifiable irreversible action is not an acceptable artifact. The
checkpoint is what makes the escalation model work at all — without one, "the
human did it and we resumed" is an assumption, and assuming a transfer landed
is the exact failure this design exists to prevent.

Considered and rejected: runtime refusal only. Rejected because load-time
catches it before a browser opens, rather than mid-run after a person has
already acted; both layers are kept so the invariant survives an artifact that
reaches the engine by some path the loader did not see.

Consequence accepted: this makes the recorder's risk heuristic load-bearing —
any step it marks risky must also get a checkpoint or the artifact will not
load. That forced the terminal-post checkpoint fallback.

---

## App profile as the adapter seam

Everything the engine knows about a specific application moved into
`config/app_profiles/{name}.json`: error markers, recovery actions, version
pattern, frame model, risk vocabulary, chrome values to scrub, redaction
sources, auth element defaults. Pointing at a new application should be
writing a profile, not editing `replay/` or `discovery/`.

**Recovery is an action kind, not a marker string.** CoreServ's maintenance
interstitial is a `<button>Continue</button>` that re-renders the page
underneath it, so dismissing it in place keeps the flow's position. MERIDIAN's
is `<a href="/menu">Continue</a>`, which navigates to the main menu and
abandons the step being retried. Same visible affordance, same label,
opposite semantics. So the profile names a kind — `dismiss_control` vs
`reload_step_url` vs `backoff` — and the engine implements each.

Considered and rejected: profiles carrying only strings (marker text, control
names). Rejected on the evidence above: a profile that said
`interstitial_dismiss: "Continue"` for both apps would be correct about the
vocabulary and wrong about the behaviour, and "recovery" would have meant
walking away from the flow on one of them. Recovery strategy is app-specific,
not just app vocabulary.

Also rejected: collapsing the discovery allowlist into the profile. A profile
describes what an app *is*; a policy declares what the agent may *do* to it.
Merging them buries a safety decision inside a description.

Limit accepted: the action kinds are engine code. A third app needing a
genuinely new recovery mechanism needs an engine change. That is the right
boundary — an executable action is not JSON — but the vocabulary is three
items wide.

---

## Scrubber sources are declared in the profile

`profile_scrubber(profile)` builds redaction from declared literals and
patterns, with an optional named `fixture_module` as an escape hatch for an
app whose dataset this repo owns.

`seed_data_scrubber()` imported `coreserv.data.MEMBERS` from library code, so
on any other target it degraded to pattern-only matching — names and street
addresses passed straight through, with nothing saying so.

Considered and rejected: (a) the profile naming a fixture module only —
rejected because a real deployment has no fixture module and cannot enumerate
its members, so patterns have to be the primary mechanism; (c) the caller
constructing a scrubber with literals — rejected because it pushes the
decision to five call sites, and a helper you have to remember to call is one
that eventually is not called, which is exactly how phase 1 got here.

**Degradation must be loud.** A scrubber with no literal source reports itself
degraded into the evidence log (`redaction_configured`) and into the run
result's warnings. The phase-1 lesson was not "redaction was wrong", it was
"redaction got weaker and nothing said so".

---

## Frame is optional with a null default

`Element.frame` defaults to `None`, meaning the document. A frameset is the
exception, not the default.

Considered and rejected: swapping the hardcoded `"content"` for a hardcoded
`""` (Playwright's name for the main frame). Rejected because that moves the
coupling rather than removing it — the next surface would need a third string,
and `""` is an implementation detail of the snapshot that no artifact author
should have to know. The resolver normalises `None` to the snapshot key at the
one place that reads it.

Consequence caught late: the recorder did *not* normalise, so
`frames_before.get(None)` missed and the first MERIDIAN recording emitted one
step and zero elements while the executor had resolved every control fine.
Optionality has to be handled everywhere the value is read, not just where it
is declared.

---

## cell_in_row falls back to a grid's first row as headers

When a table contains no `columnheader` node at all, `_find_column_index`
treats the first row of that table as the header row, scoped to that table and
refused when the name is ambiguous or when the table has real headers and
simply lacks this one.

MERIDIAN builds every grid header from `<td>`, so it has zero `columnheader`
nodes and the strategy resolved nothing there.

Considered and rejected: a per-app profile flag saying "this app uses td
headers". Rejected because it would encode the workaround as configuration
and leave the strategy broken for every other legacy app with the same markup
— which is most of them. Styling a header row instead of marking it up is a
general property of table-era HTML, so the fix belongs in the resolver.

---

## Both provider clients kept; default switched to Anthropic

`AnthropicClient` and `GeminiClient` both remain wired. Default moved to
Anthropic / `claude-sonnet-5`.

A single-implementation interface is an untested guess about where the
boundary goes. The neutral transcript (`Observation` / `AssistantAction` /
`ActionResult`) only proves it is neutral by surviving two genuinely different
wire formats.

The switch was forced by something real rather than chosen on preference:
Gemini's free tier is 20 requests per day per model and a discovery run costs
8–12, so at seven capabilities the cap became binding — three models were
exhausted in one afternoon. That is the seam doing visible work: changing
provider was a default and a flag, and the Gemini-recorded evidence stays
valid because an artifact does not record which model produced it.

Considered and rejected: deleting the Gemini client once Anthropic became the
default. Rejected because the second implementation is the only evidence the
abstraction holds, and because a provider outage or quota wall is exactly when
you want the other one still working.

---

## Semantic correctness of an output is not machine-checkable

On the goal "look up member 100234 and read their share balance", the model
read the **Shares** column of the search results — a *count* of 20 — called it
a balance, and reported success without opening the member record.

Every mechanism passed. The locator resolved uniquely. The checkpoint matched.
The value coerced cleanly to `money`. The artifact was valid and would have
replayed successfully forever, returning a share count typed as currency.

Nothing in the system can catch this from a single run, because there is
nothing to compare against: "is 20 the balance?" requires knowing the answer
independently. This is the concrete reason artifacts are emitted as
`status: "draft"` and a human approves them — draft status is not
bureaucracy, it is the only control that covers this class of error.

Considered and rejected: a discovery-time verification pass asking the model
to check its own output. Rejected because it re-asks the model that just got
it wrong, using the same page it misread. Not fixed, and not fixable in this
sprint. The mitigation available now is that the goal is the specification —
a sharper goal ("read the balance of share X from the shares table") made the
error impossible to make.

---

## Redaction leaks by paraphrase, and grep found it

The MERIDIAN profile declares `"Lovelace, Ada"` — the surname-first form the
app renders. A discovery run's own summary read
`member 100234 (Ada Lovelace) is 20`, and literal scrubbing is exact, so the
flipped form went into committed evidence.

`register_pii` now also registers the comma flip. That catches the common case
and is explicitly not a claim to catch every paraphrase.

The general shape is the finding: **the model's prose is a redaction channel.**
It restates observed values in forms nobody enumerated, and every literal rule
is exact by construction. This is the fifth distinct redaction failure across
the project — enough that the pattern is the point, not the individual bugs.

Considered and rejected: masking the model's summary text wholesale. Rejected
because the summary is the most human-useful line in the evidence and
destroying it to protect fake demo data trades away real debuggability for
theatre.

Worth recording plainly: this was caught by grepping the staging area before
committing, not by a test. That is a weak control to be relying on for the
thing that has now failed five times.

---

## Recording the transfer before closing the discovery risk gate

Chose to record the funds-transfer capability first — which posted a real
$5.00 transfer on the demo app — and close the discovery risk gate
immediately afterwards.

The app is explicitly for hammering on: public demo operators, in-memory
state, resets on redeploy, a fake member, $5 between two of their own shares.
Recording first was the only order that produced a complete artifact,
because closing the gate first would have blocked the post step and left the
capability unrecordable until partial-artifact emission existed.

Considered and rejected: closing the gate first and accepting a partial
artifact. Rejected as sequencing — the partial-artifact path had not been
built yet, so the run would have emitted nothing at all. Also rejected:
stopping at the review screen, which avoids the post but leaves the
irreversible step, its checkpoint, and the token carry-through untested.

**The finding this exposed.** `discovery/loop.py` claimed "discovery cannot be
more permissive than replay because there is no second implementation for it
to be permissive in". That was false. The executor's risk gate reads
`Step.risk`; discovery constructed every `Step` without one, so it defaulted
to `"safe"` and `check_risk` never fired. Risk was assigned afterwards, by the
recorder. **Shared code is not shared enforcement** — a guard never handed the
input that trips it is not a guard. Same class as the phase-1 bug where
`PolicyViolation` was caught one level too high: in both, the mechanism was
present, correct, and unreachable.

Confirmed live rather than asserted from the code: the run clicked
`Post Transfer`, the transfer posted (`$40.00 → $35.00`), and no block,
escalation or intervention appeared anywhere in the evidence.

Fixed by classifying risk at act time in the discovery loop, using the same
app-profile verb rules the recorder uses, so the step discovery is willing to
perform and the step the artifact calls risky are the same judgement.

---

## A risk-blocked discovery run still emits an artifact

`DiscoveryOutcome.recordable` covers `goal_reached` **and** `risk_blocked`.
The blocked step is recorded with its locator chain, marked `risky`, and never
executed. Provenance says in capitals that the flow was not completed, that
nothing after the blocked step was observed, and that its checkpoint is a
guess rather than something the run verified. The CLI exits 4 — non-zero,
distinct from failure — so a caller cannot read "artifact written" as "flow
proven".

Considered and rejected: emitting nothing on a risk block, as before.
Rejected because the gate would then mean "irreversible capabilities cannot
be recorded at all", and the safe-looking choice would be the one that makes
the system useless for the half of the brief's function list that ends in a
post.

Considered and rejected: recording the blocked step as `safe` so the artifact
replays end-to-end. Rejected for the obvious reason, but worth writing down:
it is the shape the pressure pushes toward, because a fully-replayable
artifact looks more finished.

---

## Select steps record the option value, never the display label

A `select` records the option's `value` attribute, read back from the browser
after the selection, rather than whatever string the model passed.

The first funds-transfer recording stored
`"100234-S0001-6 - Regular Shares ($40.00)"` — the option's visible label,
which embeds the balance at record time. The same run debited that share, so
the artifact's own locator was stale before it was committed. Recording the
capability broke it. Playwright matches an option by value *or* label, so what
the caller passed does not reveal which one it was; reading the element back
is the only way to observe the stable identifier.

Same class as the phase-1 circular locator, where the model targeted the
balance cell by its displayed value. The recorder already suppresses
name-based rungs for extraction targets for exactly this reason; a value
derived from what the page currently shows resolves during discovery
*because* the page still shows what discovery saw.

Considered and rejected: parsing the label to its leading token before " - ".
Rejected as guessing at another app's formatting — it happens to work on
MERIDIAN and would silently mangle any console that formats options
differently.

---

## Every select becomes a declared input

Not only the ones whose options vary per record.

MERIDIAN's selects split into member-scoped (From Share, To Share, the Hold
share picker) and fixed-vocabulary (Share Type, Reason Code, Search by).
Parameterising only the first group needs a heuristic — "does the option value
contain the member ref?" — that works for share ids and nothing else, and it
is the same kind of guess as the verb list, which has already misfired in the
wild. The fixed-vocabulary selects are also exactly what a caller varies:
"open a Money Market" versus "a Certificate" is one capability with a
different argument.

The asymmetry decides it. Over-parameterising costs a required input the
caller must supply: annoying, visible, safe. Under-parameterising bakes in a
value that silently does the wrong thing.

Considered and rejected: templating share ids against `member_ref`
(`{{member_ref}}-S0001-12`). Rejected because share suffixes differ per
member, so it would fabricate a share id that may not exist. `from_share` is a
plain input whose description says it must belong to `member_ref`.

Also decided: select parameters take the plain label slug (`from_share`), not
the `_ref` suffix identifier text fields get. That convention exists because
tenants relabel identifier *fields* while meaning the same entity; a select's
label names its role in the flow, which is stable.

---

## A recorded value containing a currency amount is flagged

`suspect_value()` refuses to let a currency amount inside a `select` value
pass unremarked: it is logged as evidence and written into
`provenance.notes` in capitals.

Scoped to selects deliberately. A `fill` value containing currency is normal —
an amount of `5.00` is precisely what the caller typed. A select value
containing currency cannot be caller intent, because the caller did not
compose it: the page did.

Considered and rejected: refusing to record the step at all, by analogy with
name-based extraction rungs. Rejected because dropping a select breaks the
flow, whereas dropping a circular *rung* leaves other rungs. With the value
read-back in place this guard should never fire; it exists for when the
read-back is unavailable.

---

## The risk heuristic only considers submit-type controls

A click is a risk candidate only when the resolved control's role is `button`
— `<button>` and `<input type=submit>`. Links matching a commit verb are
logged as near-misses, not marked risky.

The first live transfer recording marked a *navigation link* named "Funds
Transfer" as risky, because the verb list contains `Transfer`. Navigation
links share the commit vocabulary with commit actions; only a submit-type
control can commit.

Considered and rejected: removing `Transfer` from the verb list. Rejected
because it is the right verb for `Post Transfer` and for the transfer button
on other consoles — the problem was never the word, it was applying it to
controls that cannot commit anything.

---

## An incomplete recording loads, but cannot replay

`provenance.flow_completed` is false when the risk gate blocked discovery
mid-flow. Such an artifact is exempt from the "a risky step must declare a
checkpoint" rule, and replay refuses it outright before validating inputs.

This closed a hole the gate itself opened. A blocked step was never performed,
so nothing after it was observed and there is no post-action URL to derive a
checkpoint from — the artifact failed validation and the partial recording was
unusable. Inventing a checkpoint would have been worse: it claims a
verification nobody did.

Considered and rejected: dropping the checkpoint requirement for risky steps
generally. Rejected because it is the rule that makes escalation meaningful
for *completed* capabilities. The exemption is scoped to artifacts that
declare themselves unfinished, and those are refused at replay rather than
run.

---

## Recording posture and replay posture are separate

The emitted artifact always carries `risky_action_handling:
"require_confirmation"`, regardless of what the discovery run used.
`config/discovery_policies/meridian-recording.json` relaxes the gate to `flag`
for recording only.

You cannot record a review→post flow without posting once, so a gate that
blocks discovery makes every capability ending in a post unrecordable — the
safe-looking choice would make the system useless for half the brief's
function list. So the gate is relaxed for recording, by an engineer, on the
command line, in a named file that explains itself, with the policy path in
the run's evidence.

That is the opposite of the bug it replaces. Until this phase discovery never
evaluated step risk at all, and nobody knew. The difference between "the gate
is off because of a bug" and "the gate is off because this file says so" is
the entire distinction.

Considered and rejected: letting the artifact inherit the recording policy.
Rejected because a relaxed recording session would then emit a capability that
posts unattended in production — one engineer's convenience becoming everyone
else's default.

---

## Positional rungs are scoped to a container, and never appended

`role_ordinal` may carry a `scope`. The recorder records scoped ordinals and
does **not** append a document-wide one to a chain that already has a
non-positional rung.

The recorded transfer chains ended in document-wide ordinals — `link index 5`,
`textbox index 0`. They resolved uniquely, which is what made them dangerous:
after a layout change they still resolve, to a stranger, and the run reports
success. A trailing ordinal only ever fires when the rungs above it failed —
exactly when position is least trustworthy — so it contributes nothing on a
healthy page and misfires on a changed one.

It also contradicted the resolver's own rule. Ambiguity is a miss rather than
pick-the-first because "picking [0] would be a guess dressed up as a
resolution"; a document-wide ordinal is that same guess, pre-registered at
record time.

The failure modes are asymmetric in the direction that matters. An exhausted
chain gives `element_unresolvable` → hard failure → escalation-eligible → a
human looks. A wrongly-resolved chain gives a successful run with wrong side
effects, and on a risky step that is the wrong transaction posted. A stalled
job is bounded; a wrong post is not.

Considered and rejected: dropping document-wide ordinals entirely. Rejected
because when nothing else resolves, the choice is not "safe artifact" versus
"dangerous artifact" — it is "flawed recording" versus "no recording", and the
safe-looking option just moves the failure somewhere nobody sees it. So a
document-wide ordinal survives as a *sole* rung, with a shouting note on the
element and a `POSITIONALLY IDENTIFIED ELEMENTS` line in provenance.

Also rejected: scoping to the enclosing table rather than the row. Tables here
carry no accessible name, so the scope would have to be keyed on the table's
whole text — broader than the document ordinal it replaces, and no more
stable.

---

## A locator scope is never keyed on personal data

Scope texts are filtered through the app profile's own redaction declaration,
and a parameterised scope suppresses literal alternatives from the same row.

The first tightened recording scoped the results-row locator on
`"Lovelace, Ada"` — a member's name, in an artifact bound for a public repo.
The cells that identify a record are the cells that carry its personal data,
so "prefer the parameterised scope" and "keep names out of locators" turn out
to be the same rule. Reusing the scrubber's declaration means there is one
idea of what counts as personal, not a second one quietly diverging inside the
recorder.

Considered and rejected: a dedicated PII regex in the recorder. Rejected
because it is a sixth place to get redaction wrong, and the profile already
declares this per app.

Considered and rejected: recording every candidate scope and letting replay
pick. Rejected because a literal scope is strictly worse than a parameterised
one that resolves — recording both means shipping a locator that only works
for the record it was discovered on, ready to fire the moment the good one
misses.
