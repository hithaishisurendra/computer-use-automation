# Evidence

Runs against a live CoreServ, produced by the CLIs in this repo. Every file
here is real output, not illustration. Sensitive values are masked on write
(see **Redaction** below).

Section numbers refer to the assignment brief.

---

## Where to look first

| If you want to see... | Open |
|---|---|
| An LLM actually driving the app | `discovery/disc_ba9d736d/cycles.jsonl` |
| What discovery produced | `discovery/disc_ba9d736d/artifact.json` |
| That artifact replaying deterministically | `replay/run_32707e07/result.json` |
| Every replay outcome at a glance | `replay/MATRIX.txt` |
| A human taking over a stuck run | `escalation/run_61d94a77/request.json` |
| One artifact working on a second tenant | `replay/run_8dee5028/result.json` |

---

## `discovery/` — §3.1 goal-driven agent loop, §3.2 artifact

Two genuine Gemini (`gemini-3.5-flash`) runs on the goal *"Look up member
10001 and read their current savings balance"*. Each directory has
`cycles.jsonl` (every observe → decide → act cycle, including the model's
reasoning and the accessibility snapshot it saw), `artifact.json` (what the
recorder emitted), `summary.json`, and per-step screenshots.

- **`disc_ba9d736d`** — the current one. 5 attempted / 5 recorded.
- **`disc_60829c31`** — kept deliberately as a contrast. 7 attempted /
  **4 recorded**: the model clicked Submit and View a second time after the
  page had already moved past them. Both re-clicks failed and were dropped.
  This is the transcript-versus-artifact split doing its job — the artifact
  is *cleaner than the run that produced it*, which a macro recording would
  not be.

## `replay/` — §3.3 deterministic replay, error handling

`MATRIX.txt` summarises every replay outcome including all seven injected
faults. Individual runs, one per result class:

| Run | Tenant | Shows |
|---|---|---|
| `run_71e357b9` | northridge | Hand-written artifact, success |
| `run_32707e07` | northridge | **Discovery-emitted artifact replaying**, member 10001 |
| `run_8813eb03` | northridge | Same emitted artifact, **member 10003** — proof it generalised rather than hardcoding the member it was discovered on |
| `run_4666bfd5` | northridge | `business_outcome` / `member_not_found` (via the `member_not_found` fault) |
| `run_5ab2bf17` | northridge | `business_outcome` / `permission_denied`, with failure screenshot + snapshot |
| `run_b3bb22fd` | northridge | `hard_failure`, with failure screenshot + snapshot |
| `run_5ad3fbd2` | northridge | `caller_error` — malformed input, **no browser was ever opened** |
| `run_61d94a77` | northridge | The escalation run (pairs with `escalation/run_61d94a77`) |
| `run_8dee5028` | **cascade** | Same artifact + tenant overlay against a `TENANT=cascade` instance |

Both business-outcome runs exit **0**. "No such member" is a legitimate
answer the caller needs, not a crash — conflating the two is the mistake the
result contract exists to prevent.

*Pruned:* eight earlier replay runs were removed — five referenced a
pre-fix capability id whose artifact no longer exists in the repo, and three
were duplicate successes from verification loops. Nothing distinct was lost.

## `escalation/` — §3.6 human-in-the-loop

`run_61d94a77` is one handoff: `request.json` (what the operator was handed —
capability, step, expected vs observed, URL, screenshot, snapshot),
`handoff.json` (the control-transfer log and what changed while the human
held control), plus `stuck.png` and `stuck_snapshot.txt`.

Read `handoff.json`'s `control.transitions` for the ownership trail:
`automation → human → automation → released`, each with a timestamp and
reason. The corresponding `replay/run_61d94a77/result.json` shows the run
resuming and completing — every step appears exactly once, so it continued
from where it stopped rather than restarting.

## `a11y_diagnostic/` — perception strategy

`REPORT.txt` is the honest assessment of the accessibility-tree bet: it holds
for links and buttons, and **fails for every text input, select, radio and
checkbox** on CoreServ's markup. Section 2 covers the label-augmentation
layer built in response. The numbered files are the raw per-page dumps it
draws on.

---

## Finding: the discovered capability has no cascade overlay

`replay/run_8dee5028` runs the **hand-written** capability through
`capabilities/member_savings_balance/tenants/cascade.json`. There is no
equivalent for the discovery-emitted capability, and that is correct rather
than a gap to fill.

An overlay binds to a base capability id (`extends:
member_savings_balance@1.0.0`), and discovery mints a new id. Authoring an
overlay for a freshly discovered capability would assert that its flow
generalises across tenants — and a single-tenant discovery run establishes no
such thing. It observed one app, one configuration, one tenant. The same
reasoning produces `outcomes: []` (a happy-path run sees no business
outcomes) and `status: "draft"` (discovery does not approve its own output).
All three are the artifact declining to claim more than the run demonstrated.

The lifecycle this implies, which is designed for but **not built**: discover
against one tenant → a human reviews the draft and promotes it to `approved`
→ overlays are authored as other tenants adopt the capability, each one a
deliberate assertion that the flow carries over, with its own evidence.
Discovery produces a candidate; promotion and generalisation are review
decisions.

## Finding: a tenant overlay cannot redirect replay to another tenant's host

Running the cascade overlay against a cascade instance on port **8801**
produced a *misleading pass*: `business_outcome / member_not_found`, exit 0,
`tenant: "cascade"` — while actually driving the **northridge** instance on
8800. The overlay sets `app_version` but not `base_url`, so the resolved
artifact still addressed the base tenant's host, and the replay CLI has no
`--base-url` flag. Worse, an overlay *cannot* fully fix this: it may override
`base_url` but not `policy.allowed_origins`, so an overridden host would then
fail its own origin check.

Two things are worth noting about how it surfaced. First, the classification
alone would have hidden it — `member_not_found` looks like a normal answer.
What exposed it was the **drift signals**: `app_version 4.2.3 expected vs
4.2.1 observed`, and `member_ref_field` falling through to a brittle
positional rung because cascade's "Account Number" label was not on the page.
Drift caught a misconfiguration that the result contract did not.

Second, `run_8dee5028` is what the same artifact and the same untouched
overlay do when they genuinely reach a cascade instance: **success, 8320.10,
no drift warnings, every element resolved at rung 0** — including
`results_view_link`, which needs no override at all because its row scope
matches on `{{member_ref}}` and each tenant's grid displays whichever
identifier that tenant searches by.

The overlay was not adjusted to make either run pass.

---

## Redaction

Everything here is written through `capability/redaction.py`
(`seed_data_scrubber`), not a pattern-only scrubber. `pii` values (SSN, date
of birth, phone, email, address, member name) become `<redacted:pii>`;
`identifier` values (member id, account number) are partially masked as
`****NN`, deliberately — distinct records must stay distinguishable or the
evidence stops being checkable. Credential values never appear at all, only
the environment variable names they came from.

`tests/test_evidence_redaction.py` enforces this: it scans every committed
file under `evidence/` for all seven classes of seed value and fails if any
appears unmasked.
