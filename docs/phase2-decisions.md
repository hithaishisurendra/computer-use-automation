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

---

## One write path, enforced by parsing the codebase

`capability/sink.py` owns every outbound byte: file writes, payloads returned
to callers, and the predicate that decides whether text may be embedded in an
artifact. `tests/test_redaction_chokepoint.py` parses every module under
`capability/`, `replay/`, `discovery/`, `escalation/`, `perception/` and
`scripts/` and fails if any of them calls `write_text`, `write_bytes`,
`json.dump`, `open(..., "w")` or `print(json.dumps(...))` outside the sink.

Redaction has never failed in the redaction code. It failed six times at a new
*surface* — a page dump, a credential rendered into page chrome, a target
without a fixture module, model prose, a locator scope — and every fix was a
retrofit at a new call site. Being careful at each call site is what was
tried, six times. The only thing that changes the trend is making the number
of places able to emit data stop growing, and that has to be checkable by a
machine, because "did anyone add a writer" is exactly the question code review
keeps answering wrong.

Two sources feed it and neither is a pattern list living in the sink: the app
profile's redaction declaration, and the artifact's own sensitivity taxonomy
applied by field name. The second is the half patterns can never cover — a
person's name has no recognisable shape, but the artifact says the output
holding it is `pii`.

Considered and rejected: a decorator or a lint rule on the write functions.
Rejected because both are opt-in at the call site, which is the failure mode
being fixed. Considered and rejected: making `Path.write_text` unavailable by
convention (a documented rule). Rejected for the same reason — the previous
six were all rule-following failures, not rule-ignorance failures.

The scan carries no exemption list, deliberately. When it fails, the fix is to
route the write through the sink, not to add an entry.

**It does not over-redact.** Incident 3 was a pattern broad enough to eat
`member_savings_balance@1.0.0`, which destroyed the context an operator needed
to act on an intervention. A sink that masked everything it could not classify
would be a worse bug than the six it replaces, because it fails in the
direction nobody checks. Unclassifiable values pass through untouched and the
sink reports what it was configured with.

Screenshots are declared, not exempted. A screenshot of a member record shows
everything the page showed and no text pass can mask it, so
`note_unscrubbable()` records the fact in the evidence manifest rather than
letting the file look scrubbed.

---

## One sink per run, not one per writer

The engine constructs a single `RedactionSink` and every writer downstream
shares it: evidence, intervention requests, handoff records, and the result
returned to the caller.

Four writers each built their own. Correct in isolation, and a real leak
together: `register_secrets` landed on the engine's instance alone, so
`write_request` had a sink that knew the app profile but not this run's
credentials. Demonstrated before fixing — an operator name the engine masked
everywhere else survived verbatim into `request.json`. That is the worst place
for it: an intervention request is a whole-page capture of a screen nobody
anticipated, which is exactly where an app that prints the signed-on operator
into its own chrome puts one.

Considered and rejected: registering secrets on a module-level singleton.
Rejected because a run's credentials are run-scoped, and a process-wide
registry would carry one run's secrets into another's evidence — trading a
leak for a worse one.

A writer given no sink falls back to `null_sink()`, which still applies shape
rules and reports itself degraded. A missing sink must never mean "write it
raw".

---

## The capability API is a wrapper, not a second engine

`POST /capabilities/{id}/{version}/invoke` runs `ReplayEngine`. `api/` imports
no Playwright, no perception, no resolver, no executor — asserted by a test,
because a surface that quietly grew its own way to drive a page would be the
easiest possible place for the guardrails to go missing.

Two things this surface adds rather than inherits.

**An API invocation is unattended by construction.** There is no operator
behind an HTTP request, so `escalate=False` and a risky step returns **202
escalation_required** with the step, what its checkpoint will verify, and how
to resume. 202 because the work was accepted and stopped, which is neither
success nor failure; offering escalation over HTTP would be a lie the audit
trail keeps.

**Status codes carry the result contract.** `success` and `business_outcome`
are both 200 — the caller asked a question and got an answer — and non-2xx is
reserved for cases where the system could not answer. `caller_error` is 400
before a browser opens; `auth_failure` and `hard_failure` are 502, because
neither is the caller's fault.

Considered and rejected: a tool/function-calling interface instead of HTTP.
Rejected because the chatbot and dashboard both need to reach it and an HTTP
surface serves both, where an in-process tool list would need a second
adapter. The catalogue is deliberately separate from the endpoints so a tool
list can be generated from the same description.

Run history is in-memory. A database here would be the scaling infrastructure
the brief explicitly does not reward.

---

## Some business outcomes belong to the app, not the flow

Profiles declare `business_outcomes`, checked after the artifact's own and
after engine universals.

Phase 1 put every business outcome in the artifact, reasoning that only the
flow knows a not-found search is an answer rather than a fault. Building the
API showed that is too strong. A capability recorded from a happy-path run has
`outcomes: []` — discovery never observes a not-found — so asking the API for
a member who does not exist returned **502 hard_failure** with "checkpoint not
met". That is precisely the mistake the brief calls the most common design
error: conflating a legitimate answer with a crash.

"No member records matched your search." can only mean the search found
nothing, whichever step is running when the page says it. That is a fact about
the application, and the application's profile is where facts about the
application live. The recorder cannot invent these from a successful run, and
requiring a human to hand-write them into every artifact is the duplication
profiles exist to remove.

Ordering is most-specific-first: engine universals, then the artifact's own
outcomes (still step-scoped), then the app's. A capability can always be more
precise about its own flow than the application is about itself.

Considered and rejected: treating these as engine universals alongside session
expiry. Rejected because a universal is a fault and these are answers — they
belong on the business side of the result contract, and collapsing the two
would put "no such member" in the same bucket as "the app returned a 500".

---

## A commit is identified by where the click lands, not by what it is called

Profiles declare `commit_paths` — globs for endpoints that commit — and a
click that lands on one is recorded `risky` whatever its label said. The verb
list stays as a second, independent signal.

The verb list missed. MERIDIAN labels its transfer commit `Post Transfer`
(caught by `Post`) and its share commit `Open Share` (matching nothing), so
`member_open_new_share` was recorded with its post step marked **safe** and
would have opened a share unattended on replay. A false negative here is far
worse than the `Funds Transfer` false positive: one blocks a step that did not
need blocking, the other performs an irreversible action nobody approved.

Where the click ended up is *observed*, not inferred from prose, and the
recorder already captures it for checkpoints. `/members/*/open-share/post` is
a fact about the application, so it lives with the application's other facts.

Considered and rejected: adding `Open` to the verb list. Rejected as
whack-a-mole — it fixes this button and not the next one, and the class of
error is "the label does not say what the button does", which no vocabulary
closes. Considered and rejected: replacing the verb list with commit paths.
Rejected because the two fire at different times — the path is only knowable
*after* the click, so the verb is what could ever gate a click before it runs.

---

## Discovered values must not reach the artifact's prose

An extraction target's accessible name **is** the value being read, and the
recorder used it for both the output description and the element description.
Observed: `"description": "cell CN480193"` and `"Value read from CN480192."` —
a confirmation number from one run, written into a capability's public
contract. Descriptions now come from the column header or row scope.

Same reasoning that suppresses name-based *rungs* for extraction targets,
applied to prose. The locator rule was in place and the prose was not, which
is the same shape as every redaction incident: the rule was right and the new
surface had not been covered.

Also fixed alongside: an output extracted twice was declared twice. The model
read `confirmation_number` from the label cell and again from the value cell,
and the recorder appended two identical output specs.

---

## Validation rejections are answers, not failures

MERIDIAN's `"could not be validated"` is declared as an app-level business
outcome.

Replaying `open_new_share` with a $250 certificate deposit reported
`hard_failure: Could not locate 'continue_button'`. Perfectly true and
completely useless: the button was absent because the page was a validation
error saying *"Certificates require a minimum opening deposit of $500.00."*
The caller needs the rule they broke, not a missing-element trace.

Considered and rejected: classifying it `caller_error`. Tempting — the caller
did supply a bad value — but the system cannot know that in advance. A $250
certificate deposit satisfies the artifact's declared contract; only the
application knows the minimum. `caller_error` is reserved for violations
detectable before a browser opens, and blurring it would make that promise
meaningless.

---

## The commit-path signal needed the same narrowing the verb signal had

A click is only a commit candidate when the resolved control's role is
`button`. This was applied to the verb signal when `Funds Transfer` (a link)
was wrongly marked risky, and **not** applied to `commit_paths` when that was
added a session later.

MERIDIAN serves its update form from `/members/<id>/update` and posts it to
the same path. So the `Select` **link** that opens the form matched a declared
commit path and was recorded risky — replay would have blocked at step 5,
before a single field was filled in. A link click is a GET and commits
nothing.

The lesson is about the fix, not the target: a rule established for one signal
did not travel to the second signal added later for the same purpose. Two
mechanisms answering one question need the same qualifications, and nothing
made that true by construction.

---

## A checkpoint that was already true is refused

`_url_checkpoint` returns None when the URL did not change across the step, and
falls back to a heading that appeared only afterwards.

The derived checkpoint for `Save Changes` asserted `/members/[^/]+/update$` —
the path the page already had, because the form posts to itself. It would pass
whether or not the click did anything. That is worse than no checkpoint: the
schema accepts it, a reviewer reads it as verification, and it certifies
nothing.

Considered and rejected: any post-action text as the fallback. Rejected
because a server-rendered console puts a live clock and a session id in its
status bar, so most observed text differs between two loads of the same page.
Headings are used instead, and any heading containing a digit is refused as
well, since one carrying a confirmation number would pin the checkpoint to a
single run.

---

## An input's sensitivity is decided by its value, not its label

`_infer_input` runs the candidate through the sink's `is_sensitive` predicate.
A sensitive value makes the input `pii` and its example is **withheld**.

Everything non-numeric was declared `public`, so an email typed into an update
form was a public input. It did not reach disk in the clear only because a
shape rule happened to catch it — luck, not classification, and luck that does
not hold for a value no pattern recognises.

Considered and rejected: keeping the masked example (`"example":
"<redacted:email>"`, which is what the first recording produced). Rejected
because a masked placeholder reads like data, tells a caller nothing about the
expected shape, and invites someone to paste it back in. Saying nothing is
more honest than saying something masked.

This forced a separation worth naming: the recorder keeps the discovered value
internally to substitute `{{param}}` into step values and scope texts, while
the artifact declares no example. Conflating "what the recorder knows" with
"what the artifact publishes" broke parameterisation the first time the two
diverged.

---

## Provenance names the step it is actually about

Risk notes are keyed by element and given their step id after renumbering.

They were built with the step id in hand, and then
`_ensure_opening_navigate` prepends a step and `_renumber` shifts everything
after it — so the first update recording blamed `s4` for a decision about
`s5`. Provenance that points a reviewer at the wrong step is worse than
provenance that says nothing, because it will be believed.

---

## A paused run outlives the request that started it

An attended invocation runs on its own thread with its own event loop, and
`PendingOperator` blocks that thread on an Event a later HTTP request sets.

The escalation model depends on the human working on *the session that got
stuck* -- same browser, same cookies, same half-completed flow. An HTTP
request cannot hold that: the response has to return so the dashboard can
render the intervention. So the run keeps its thread and the operator surface
blocks it, exactly as `ConsoleOperator` blocks on stdin.

That symmetry is the design. Both satisfy `OperatorSurface`, so the engine
cannot tell them apart, which is what makes the dashboard a second operator
*surface* rather than a second escalation *mechanism*. `ConsoleOperator`
stays: unattended CLI replay still escalates to a terminal.

Considered and rejected: serialising the paused run and rehydrating it on
resume. Rejected because a browser session is not serialisable in any useful
sense -- rehydrating means a new session, which is precisely what the
handoff model forbids.

A pause has a deadline (30 minutes) and expires into an abort. It holds a
browser, a thread and a live application session; waiting forever leaks all
three. Expiry aborts rather than resumes, because resuming would continue a
run whose blocked step nobody performed.

---

## The dashboard reads the API and reaches nothing else

`api/dashboard.py` serves static files. The page's only capability is
`fetch`, and a test asserts every path it requests is one the API serves.
When the dashboard needed the stuck screenshot, the API grew
`/runs/{id}/evidence` rather than the page learning where evidence lives.

Considered and rejected: server-rendering the pages from FastAPI with Jinja.
Rejected because a template with the artifact in scope can read anything, and
the constraint worth enforcing is that the dashboard has no more access than
any other API client. A static page cannot cheat: there is nothing in scope to
cheat with.

---

## The chokepoint scan now covers HTTP response bodies

`test_redaction_chokepoint.py` scans `api/` and flags any
`JSONResponse`/`PlainTextResponse` whose content did not come from a sink.

An HTTP body is an output surface, and this project has leaked at every new
surface it added. The scan caught two real gaps the moment it was widened:
`_live_run_body` scrubbed only its escalation block, and the evidence endpoint
returned file contents unscrubbed.

The fix in `_live_run_body` is worth naming: it now sinks the *assembled*
body rather than each part. Piece-by-piece scrubbing is how the previous six
incidents happened -- whoever adds the seventh field forgets, and nothing says
so.

Considered and rejected: matching sink calls by receiver name
(`sink.payload(`). Rejected because it missed `null_sink().payload(...)` and
`record._sink.payload(...)`, both correct. The guarantee comes from which
method ran, not from what the variable is called, so the scan matches
`.payload(` / `.emit(` / `.text(`.

---

## `kv()` escapes its own values

The definition-list helper interpolated its value raw and trusted every call
site to have escaped first. That works until someone adds a row and forgets,
and the page then renders app-controlled text as markup -- a legacy console
that echoes a member's input into an error message is exactly the source that
would exploit it. Callers needing markup pass `html(...)`, which is explicit
and greppable.

Considered and rejected: leaving it to the call sites and testing that they
comply. Rejected as the same shape as the redaction problem: a rule every
caller must remember is a rule that eventually is not followed. Safe by
construction beats enforced by review.

---

## Request models forbid unknown fields

`StrictRequest` sets `extra="forbid"`, so `{"policy": {...}}` on an invoke is
a 422 rather than a silently ignored key.

Silently ignoring it gives a caller a 200 and no way to tell the override was
not honoured. The value was never read -- but a reader cannot distinguish
"ignored" from "applied", and the next person to add a field may wire it up.
A 422 says plainly that this surface does not take policy.

---

## One sensitivity classifier for inputs and outputs

`classify_sensitivity()` decides how sensitive data read from or typed into a
field is, and both `_infer_input` and the output declaration call it. Three
signals, most sensitive answer wins: the field's **label** against a new
profile `sensitive_labels`, the observed **value** through the sink's
predicate, and what the artifact has **already declared** about that field.

The leak it closes shipped in a committed artifact: `member_update_info`
declared `e_mail` as `pii` input and the output reading *the same field* as
`public`. Same field, same data, opposite classification — the input fix from
the previous session simply never travelled to outputs.

**This derives at the boundary.** Sensitivity is computed once, from the field,
at the single point where any declaration is made. There is no second decision
to drift out of agreement with the first. That is the property Finding 11
identified: `risky_action_handling` and `catalog.risky_steps` never broke
because they derive; the input/output split broke because it copied.

Three signals rather than one, because each covers the others' blind spot. The
label is the durable fact — a member with no e-mail recorded yields an
innocuous sample, and value-only classification would declare the e-mail output
public forever. The value catches a sensitive field under a label nobody
enumerated. The declared-field ledger makes the original leak structurally
impossible rather than merely fixed.

Considered and rejected: defaulting every output to `pii`. Safest by
construction, and it would mask `savings_balance` and `confirmation_number` —
the answers those capabilities exist to return. A caller who gets `<redacted>`
where they expected the answer learns to ignore the sensitivity field, which
is worse than the problem.

Considered and rejected: classifying outputs from the observed value alone,
mirroring `_infer_input` exactly. Smaller, and it catches the live leak — but
an output is a value that *varies per invocation*, and one benign sample would
lock in `public`.

Which labels name personal data is per-app knowledge, so `sensitive_labels`
lives in the profile beside the app's other facts.

---

## Replay re-derives risk and refuses when it disagrees

`check_risk_agreement()` runs in `load_resolved`. It re-derives each click
step's risk from the app profile and refuses the artifact when the profile
considers a step irreversible and the artifact does not.

Replay read `step.risk` and believed it. A hand-edited artifact flipping
`risky` to `safe` would have posted unattended — while the profile that would
have caught it was in hand the whole time. The recorder's judgement is a
*recording-time* fact and execution was treating it as gospel.

**Refused, not corrected, and only in one direction.** Overriding toward
`risky` would let a stale profile break a reviewed capability; overriding
toward `safe` would let a tamper through. Either way the override hides the
disagreement, and the disagreement is the information: something is wrong with
the artifact, the profile, or both, and which one decides whether to re-record
or to review the profile. The reverse direction — an artifact marking a step
risky the profile does not — loads fine, because a human may mark a step risky
for reasons no vocabulary encodes, and refusing that would punish exactly the
review `draft -> approved` asks for.

**At load, not at execution.** An element's chain carries the control's role
and accessible name, which is what the recorder classified on, so the verb
signal reproduces statically with no browser. That means the refusal happens
before Chromium starts rather than three steps in with a session open — and
every surface that loads an artifact inherits it.

**This derives at the boundary**, and it is the one fix here that turns a
copy into a derivation: the recorded label is still stored, but it is no
longer *believed* — it is checked against a fresh derivation on every load.

**A limit, stated rather than papered over.** The static derivation reproduces
the verb signal only. `commit_paths` matches a landing URL, which is not
observable before the click, so a commit whose label says nothing —
MERIDIAN's `Open Share` — derives `safe` here. The artifact still marks it
risky from the recording, and that is the tolerated direction. Finding 1's
pre-click href check would narrow this gap.

Fixing this exposed the same defect one layer up: `api/catalog.py` called
`load_artifact` while invoke called `load_resolved`, so a tampered artifact
listed as `invocable: true` and then failed on invocation. A catalogue that
advertises what the invoke path refuses is worse than one that says why — an
agent reads the flag and calls it. Both entry points now run the check.

---

## Every checkpoint rung is tested for discrimination, not just the fallbacks

`_add_checkpoints` is one cascade — element, then URL, then heading — and each
rung must establish that its condition was **false before the step and true
after**. A rung that cannot establish it returns None and the next is tried.

Two rungs already enforced this. The first — `element_present` on the next
step's control, which fires for almost every step — did not. The rule had been
applied where a bug was found rather than everywhere the property is needed,
which is this audit's whole subject.

`_element_checkpoint` resolves the candidate through the **resolver against
the pre-step tree**, not by searching observation text. `element_present` is a
resolver question at replay time, and answering it any other way here would
vet a different condition than the one that will actually run.

**Unproven is not proven.** No pre-step tree, or a chain that fails to resolve
for unrelated reasons, both return None rather than emitting. A checkpoint
nobody verified is indistinguishable from one that verifies nothing.

**And that makes some risky steps unrecordable, which is correct.** A risky
step with no checkpoint is rejected at load, so a risky step whose every
candidate is vacuous cannot become a capability. An irreversible action nobody
can confirm should not be one — the same reasoning as the load-time rule. The
cascade is what keeps that from being over-strict: it refuses only when all
three rungs fail, not when the first does.

One exemption: the synthetic opening navigate `_ensure_opening_navigate`
prepends has no observed cycle and therefore no pre-step tree. It is also the
one step whose discrimination cannot matter — it is first, nothing preceded
it, and asserting the flow's first control is what makes the artifact state its
own starting precondition. Risky steps never take that path; the recorder only
ever synthesises a navigate.

**This derives at the boundary.** The property is computed from observed
before/after state at the single point a checkpoint is produced. Nothing
copies a checkpoint decision from one place to another.

### The audit was wrong about `open_new_share`

The audit reported all three recorded capabilities clean. Re-recording under
the cascade showed `member_open_new_share` s9 falling through from
`element_present` to `url_matches` — which means the confirmation cell **did**
resolve against the pre-click tree. MERIDIAN's review page and its posted page
both carry a `Confirmation` row, so that checkpoint was true before the click
and would have passed whether or not the share was opened.

The audit missed it because it string-matched the scope text `'Confirmation'`
against the observation, and the observation is a filtered rendering rather
than the tree the resolver walks. **A weaker check than the one being audited
will clear things the real check catches.** All three capabilities were
re-recorded; the corrected artifacts are committed.

---

## The commit signal is available before the click, and perception provides it

`perception/labeling.py` records `props.action` for submit-type controls, and
both risk decision sites — discovery's act-time gate and the loader's static
derivation — now use the destination alongside the verb.

The accessibility tree exposes a link's href as a `/url` property and a submit
button's target **not at all**: a button's destination lives on the enclosing
`<form>`. So the control that can commit is exactly the control whose
destination is invisible, and that gap is not incidental — it is why the
commit signal had to be post-hoc.

**Perception reads one non-accessibility attribute, and that needs saying
plainly.** The project's bet is that role + accessible name is the whole
perception surface. This is a deliberate, narrow exception: the safety model
turns on knowing whether a click commits, and the accessibility tree cannot
answer it. The script reads the form's `action` and nothing else — not its
method, not its fields, not their values — and a test asserts that by
inspecting what the script accesses rather than trusting its comment.

Considered and rejected: reading the DOM in the executor just before clicking.
It has a Playwright locator in hand, so it could. Rejected because it puts
page inspection in the layer that is supposed to *act on what perception
reports*, and the recorder would need its own copy — two mechanisms answering
one question, which is this audit's entire subject.

**Narrower than the landing URL, deliberately.** A form's action is where the
request is *addressed*; the landing URL is where the app *ends up*. A server
that redirects after posting, or a form with an empty action that posts to the
current path, both defeat it. It is kept alongside the post-hoc landing-URL
match in the recorder rather than replacing it: the pre-click signal is the
only one available before the click, and the post-hoc one is the more accurate.

**This closes Finding 10's stated limit.** The recorded `Element.destination`
lets the load-time derivation reproduce the commit signal without a browser, so
a downgrade of a step the verb list cannot see — MERIDIAN's `Open Share` — is
now refused. Verified: flipping that step to `safe` in a copied artifact is
rejected, where before it loaded cleanly. All four shipped capabilities now
agree between recorded and derived risk.

An artifact recorded before destinations existed carries none and falls back to
the verb signal, which is why the check refuses downgrades rather than
asserting equality — an artifact must not be refused for missing a field it
predates.

**This derives at the boundary.** The destination is observed once, by
perception, and every consumer reads it from there. The recorded value in the
artifact is a cache of an observation, and the loader re-derives the *decision*
from it on every load rather than trusting a recorded decision.

---

## Selects, suspect fills, and stale descriptions

Three smaller findings from the same audit, and one of them was not what it
looked like.

**Selects already went through the shared classifier**, as a side effect of
the Finding 4 fix: `_infer_select_input` takes a computed sensitivity rather
than testing digit-shape. Nothing to do.

**The extraction label had a hole the classifier could not see past.**
`_extraction_label` read `column_header` and `row_contains` only. A model that
names the control directly -- `{"role": "textbox", "name": "E-mail"}` with no
scope at all -- produced an empty label, so the label signal never fired and
an extracted e-mail was declared `public` on a capability recorded **after**
the classifier was meant to prevent exactly that. The control's own name is
the field's label when no scope is given, and it is now read.

Worth naming, because it is this audit's pattern once more: Finding 4 built
the right classifier and fed it from an incomplete source. A correct decision
procedure with a lossy input is indistinguishable from a wrong one.

**The suspect guard now covers fills**, with a different signature than
selects. Currency in a select value means the page composed it; currency in a
fill is normal, since "5.00" is what a caller types. So a fill is suspect when
its value appears in the observation the model was looking at, does not appear
in the goal, and is long enough that co-occurrence is not chance. All three
conditions, because any two of them fire on ordinary flows.

**This derives at the boundary** in both cases: one classifier, one label
extractor, called at the single point a declaration is made.

### A blocked re-record, and why the artifact stays stale

`member_savings_balance_discovered` still carries `Value read from 8320.10.`
in an output description. It could not be re-recorded, and the reason is a
real interaction rather than an oversight.

CoreServ's profile declares `fixture_module: coreserv.data`, so every seed
member id is registered as a scrubber literal. The goal text goes through the
model-facing scrubber, so `"Look up member 10001"` reaches the model as
`"Look up member ***01"` -- and the model dutifully types `***01`, which
matches no member. Every CoreServ lookup goal is unexpressible for the same
reason. Measured: MERIDIAN is unaffected (its ids are not registered
literals), which is why every MERIDIAN re-record in this pass succeeded, and
e-mail masking affects both.

This is the second sighting of the same interaction -- the first was a model
targeting a control by the literal string `&lt;redacted:email&gt;`. **Redaction
of model-facing text collides with the model's need to reference values.**
The scrubber is right that a member id is an identifier; it is wrong that the
model can work without it.

The artifact is left as-is, with the staleness recorded in the test that
checks for it: the test names it as a known exemption rather than passing
blind over the whole directory. Fixing the interaction is a separate decision
about what the model may see, and making it silently here -- by loosening
redaction to unblock a re-record -- would be exactly the trade this project
has refused five times.

---

## Guard scope is derived from the repository, not listed in each guard

`tests/scope.py` computes which packages a structural guard covers. Four
guards each carried their own hardcoded list; `api/` was added, none were
updated, and four guards went on reporting clean over code they never read.

That is this audit's defect wearing a test's clothes. A guard with stale scope
is worse than no guard, because it answers the question it was asked without
covering the ground the asker meant — and it does so silently, in green.

Excluding a package now requires naming it with a reason in `_NOT_RUNTIME`. An
omission is invisible; a named exclusion is reviewable. A meta-test fails if
any guard reintroduces a hand-maintained package list, and distinguishes a
list of packages to *scan* (a scope, must be derived) from a list of packages a
module may not *import* (an assertion, deriving it would assert nothing).

**This derives at the boundary.** One computation of scope, read by every
guard.

### Widening a guard is not the same as fixing it

Two things surfaced only because the widened guard actually ran.

**The application guard's exemption was wrong in both directions.** It stripped
docstrings alone, so pointing it at `capability/` flagged every
`Field(description=...)` that explains why a rule exists — prose the docstring
already promised was exempt. Exempting *all* string literals fixed that and
broke the guard: a planted `TENANT = "northridge"` passed. The exemption is now
shaped to prose specifically — bare string expressions, `description=`
keywords, multi-line strings — and a permanent test plants both a docstring and
an assigned literal to prove the line falls between them.

Worth stating plainly: the guard had been green for the whole project while
being unable to catch an assigned literal in the packages it did read. Nobody
had planted one.

**A scope list hid inside an assertion.** `test_the_check_runs_for_every_surface`
iterated `("replay", "api")` — written when those were the only two places risk
could be re-implemented. Now derived.

### The dashboard's source is scanned

`app.js` was ~330 lines of rendering logic no guard read. It now gets the same
treatment as Python engine code: no application names in running code, no
locator vocabulary, no route out of the page except `fetch`, and every API path
it builds must be one the API serves. Comments and string literals are stripped
first, on the same reasoning.

---

## Load time rejects self-defeating checkpoint shapes

A risky step whose checkpoint its own definition already guarantees is
refused: an `element_present` naming the element the step acts on, or a
`navigate` whose `url_matches` its own path. Both are checked through nested
`any_of`/`all_of`, since one vacuous branch of an `any_of` makes the whole
condition vacuous.

Load time has no before-state, so it can only catch contradictions between the
checkpoint and the **step beside it** — not between the checkpoint and any
observed page. That is the honest limit: this is a backstop for a hand-written
or hand-edited artifact, not a replacement for the record-time check, which
has real before/after state and does the general job.

**Two shapes considered and rejected as rules.** An element an earlier step's
checkpoint already asserted, and text that appeared in an earlier checkpoint.
Both look like vacuity and both have a legitimate counter-case: a flow that
returns to a screen — search, open, go back, search again — asserts the same
element twice entirely correctly. An artifact carries *steps*, not page
states, so the two are indistinguishable at load. Rejecting a real flow to
catch a hypothetical one is a bad trade, and the record-time check already
catches the real case with the state to tell them apart.

**Scoped to risky steps.** A safe step with a weak checkpoint is a quality
problem; nothing irreversible rests on it, and refusing to load the artifact
would be disproportionate.

**This derives at the boundary** in the sense available to it: the judgement is
computed from the step and its checkpoint together, at load, every time. It
does not copy a record-time conclusion forward — which is why it catches an
artifact edited after recording, the case the recorder cannot see.
