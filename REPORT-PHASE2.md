# Adaptation write-up: pointing the core at MERIDIAN CORE

Phase 1 built a discover-once, replay-deterministically engine against CoreServ,
a hostile proxy app I wrote for that project. This is what happened when I
pointed it at MERIDIAN CORE. Branch `phase-2-cua`. 508 tests pass with nothing
running.

## 1. What adapting took

I measured the coupling before touching anything, and wrote it up as
`docs/phase2-diagnostic.md`. Writing that report from the diff afterwards would
only have shown what changed, not what was actually hard.

Most of the system did not move. The artifact schema, the resolver, the result
contract and the escalation model are all untouched. So is discovery: the same
loop, the same prompts, the same recorder, pointed at a different app.

Perception did not move either, and that was the part I was least sure about.
The `preceding_td` labelling rule I wrote because CoreServ never bound a
`<label for>` to anything turned out to name every interactive control on
MERIDIAN too: 100% accessible-name coverage across seven pages, no changes to
`perception/`. Both apps put label text in the cell next to the input and leave
it unbound, which is apparently just what server-rendered enterprise HTML looks
like. After that the engine drove MERIDIAN end to end, signing on through the
element registry including the Branch select and reading a share balance, before
I had recorded a single capability and with no MERIDIAN-specific code anywhere.

Most of what did change is `config/app_profiles/meridian.json`: error markers,
version regex, frame model, risk vocabulary, commit paths, chrome values to
scrub, sign-on elements. Two things were not configuration, and both cost me
real time.

Recovery needed action _kinds_, not just different marker strings. CoreServ's
maintenance interstitial is a `<button>` that re-renders the page underneath it.
MERIDIAN's is `<a href="/menu">Continue</a>`, which navigates away and abandons
the step being retried. Same label, opposite semantics. So the profile names
`dismiss_control` or `reload_step_url` and the engine implements each. I lost an
afternoon to this because "recover from the interstitial" looked like a string
problem right up until the retry landed on the main menu.

Perception now reads one non-accessibility attribute: a submit button's
enclosing form `action`. The tree gives you a link's destination but nothing at
all for a button, so the control that can commit is exactly the one whose
destination is invisible, and the whole safety model turns on knowing whether a
click commits. This is a deliberate exception to the bet that role plus
accessible name is the entire perception surface, and I would rather name it
than bury it. A test asserts the script reads the action and nothing else.

`tests/test_profile.py` parses the engine packages and fails if an application
name or an app-shaped selector shows up in executable code. The seam is
enforced, not claimed.

## 2. The capability API and its contract

`GET /capabilities` is the catalogue, `POST /capabilities/{id}/{version}/invoke`
runs one, `GET /runs/{id}` returns the result and its evidence. Invocation is a
POST because it is not idempotent and may move money.

Status codes carry the result contract. `success` and `business_outcome` are
both 200, because the caller asked a question and got an answer. So are
`not_performed` and `expired`, where an operator was offered an irreversible
step and it was not taken. `202` means the work was accepted and stopped at an
irreversible step with the session held open. `400` is a caller error, caught
before a browser opens. A `policy` override in the request body gets a 422
rather than being quietly dropped, because a 200 gives the caller no way to tell
it was not honoured.

`auth_failure` and `hard_failure` are both 502, and they are not the same thing.
`auth_failure` means my own credentials for the application are wrong: a
configuration problem, not retryable, nothing the caller can do about it.
`hard_failure` means the application or the flow broke. Neither is the caller's
fault, which is why both are 5xx, but 502 really says an upstream returned
something invalid, and that fits a broken app better than it fits my own
misconfiguration. The classification in the body carries the distinction that
matters. The shared code is a rough edge I left alone.

Two structural tests hold the boundary. `api/` imports no Playwright, resolver
or executor. And the catalogue mentions no locator, frame, selector or chain,
because a calling agent has no business knowing there is a UI underneath. The
chatbot is a thin driver over the same endpoint: one model call, mapping a
request to a capability and its declared arguments.

## 3. Driving the legacy UI, and its runtime states

I target the accessibility tree rather than the DOM or coordinates. Role and
accessible name survive nested tables and ids that change on every render, and
the same abstraction exists as UI Automation on Windows and AX on macOS, which
makes `target.surface` selecting a resolver a real extension point rather than a
gesture at one. Playwright is the driver; the accessibility tree is the
strategy.

Each element carries locator rungs ordered most to least robust. An ambiguous
rung counts as a miss rather than a first match, because taking `[0]` is exactly
how replay clicks the wrong row and then reports success. Name-based rungs are
suppressed for extraction targets, and the reason is a real run: the model
targeted a balance cell as `{"role":"cell","name":"8320.10"}`
(`evidence/discovery/disc_60829c31/cycles.jsonl`). Unique on the page it was
recorded from, and useless the moment the balance changes.

Error detection runs in three layers: engine universals from the profile, then
the application's own answers also from the profile, then the flow's declared
outcomes from the artifact. The middle layer only exists because I hit a real
problem with it missing. A capability recorded from a happy path has
`outcomes: []`, so asking the API for a member who does not exist came back 502
with "checkpoint not met" — a legitimate answer reported as a crash, which is
the mistake the brief calls out by name. "No member records matched your search"
means one thing whichever step is running, so it belongs to the application, not
the flow. Ordering matters too: when a session expires MERIDIAN bounces you to
sign-on, and a flow-first classifier would look at that page and confidently
report "no such member".

The natural errors are what a production caller actually sees, and all of them
come back as business outcomes with exit 0: not found, insufficient funds, share
on hold, supervisor override required, validation rejection.
`evidence/replay/MATRIX.txt` is the equivalent sweep on CoreServ, all seven
injectable faults against one artifact. On MERIDIAN the injected fault pages
word two conditions differently from the natural ones, so the profile's markers,
which I wrote from the natural pages, miss them. That is a real gap, and it is
in the profile rather than the engine.

Forcing a fault globally turned up something worse than a missed marker.
MERIDIAN's System Settings switch injects into every request, including the main
menu, so a forced 403 arrives on a page where nothing was being authorised. The
profile's `supervisor_override_required` matched it, and a read-only balance
lookup came back saying a supervisor must perform it. Clean 200, exit 0, and
wrong. The cause is that artifact-declared outcomes are step-scoped, checked
only on the steps that declare them, and profile-level outcomes are not. That
asymmetry is still there. A confidently wrong answer is worse than an honest
failure, and this is the one result in here I would not want a caller acting on.

Session re-authentication is a per-step permission rather than engine-wide
behaviour. A mid-flow 440 during `member_share_balance` was detected,
re-authenticated, the prefix replayed, and the correct balance returned. A 440
on the POST during `member_funds_transfer` failed after one attempt with both
balances unchanged. The schema refuses `retry_after_reauth` on a risky step
outright: a post that may already have landed must never be repeated.

## 4. Safety, evidence and escalation through the new surfaces

Automation never performs an irreversible action. The run stops, hands the live
session to a person, and the operator performs the step themselves. The
checkpoint then verifies it landed. I rejected approve-and-execute, where the
operator presses a button and automation clicks, because a keypress is not an
authorisation. There is no operator identity behind it, and in a regulated
context that looks like oversight while providing none.

The operator's control means "look now", not "here is what happened". I found
that while rehearsing the demo. Abort used to return immediately without
evaluating anything, so an operator who performed the transfer and then aborted
got a run recording that the step was not performed, while the money had
actually moved. In a bank that is a reconciliation problem, and someone has to
find that money by hand. Both paths now evaluate the blocked step's checkpoint,
including the timeout path, and a test parses the method to assert that no
branch on the operator's choice can return early.

The operator drives the same session, not a fresh one. `context_identity` shows
up in the result and in the handoff record, and a test plants a swapped browser
context and asserts the handoff is refused.

Replay re-derives each click step's risk at load and refuses an artifact whose
recorded label disagrees with what it works out. A downgrade is refused; an
upgrade loads, because a reviewer may mark a step risky for reasons no
vocabulary encodes.

Redaction has one chokepoint. `capability/sink.py` owns every outbound byte, and
`tests/test_redaction_chokepoint.py` parses every first-party module and fails
on any write outside it, with no exemption list. I built it after redaction had
failed six times, never in the redaction code itself and always at a new
surface: a page dump, a credential in the page chrome, a target with no fixture
module, the model's own prose, a locator scope, an HTTP body.

## 5. Evals, and what they are here

The tests that matter most assert architectural properties rather than unit
behaviour. No application name in engine code. No model client anywhere in
`replay/`. No write outside the sink. `api/` imports no engine module. Guard
scope is computed from the repository rather than listed, because four guards
carried hand-written package lists, `api/` got added, none of them were updated,
and all four went on reporting clean over code they had never read.

Every guard was verified by planting a violation and checking it got caught.
That is not ceremony. The application guard had been green the entire project
while being unable to catch an assigned literal. Nobody had ever planted one.

A check verified against itself passes every time. I audited three capabilities
for vacuous checkpoints and cleared all three, and the audit was wrong. It
string-matched observation text where the real check is a resolver question
against the pre-step tree, and a weaker check clears exactly what the real one
catches.

What does not exist is a harness. `evidence/replay/MATRIX.txt` is a real sweep
of one artifact against every CoreServ fault, but its header says plainly that I
generated it by hand. The version worth building runs every capability against
every declared condition on a schedule, scores replay stability across repeats,
and fails when a classification changes without an artifact changing. That last
check is the one that would have caught the step-scoping problem in section 3.

## 6. Cuts, and what I would build next

Member inquiry and member record are not separate capabilities. Both are
prefixes of `member_share_balance`, which searches, opens the record and reads a
share. Recording them standalone would have been repetition, and that time went
to a consolidation pass instead.

Mid-flow privilege elevation is cut. A capability declares `required_role` and
the engine signs on with that credential set for the whole run, which is what a
supervisor does at a branch anyway. Automation that can escalate its own
privileges past a permission gate defeats the gate. The cost is real though: the
operator acts inside a session authenticated as the service account, so
MERIDIAN's own records name the service account rather than the person who
approved the transfer. Closing that is the first thing I would build.

The operator console is a dashboard page. In production the browser runs
server-side and an operator picks up a parked session over CDP screencast.
`ConsoleOperator` and the dashboard already satisfy one interface, so that is a
new surface rather than a new escalation mechanism.

Two known gaps, which I would rather state than have found.

Discovery can produce a semantically wrong capability that every mechanism
passes. Given "read their share balance", the model read the Shares column of
the search results — a count of 20 — called it a balance, and reported success
without ever opening the member record. The locator resolved uniquely, the
checkpoint matched, the value coerced cleanly to money, and it would have
replayed forever returning a share count typed as currency. Nothing catches that
from a single run, because there is nothing to compare it against. This is the
concrete reason artifacts come out as `draft`.

Redacting model-facing text can make a value unreferenceable as a locator
target. CoreServ's profile registers every seed member id as a scrubber literal,
so the goal "look up member 10001" reaches the model as "member \*\*\*01", and it
types that. No CoreServ lookup goal is currently expressible. MERIDIAN is
unaffected because its ids are not registered literals. The scrubber is right
that a member id is an identifier, and wrong that the model can work without it.
