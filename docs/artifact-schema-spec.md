# Capability Artifact Schema

The artifact is the output of discovery and the input to replay. It is a
*capability contract*, not a macro recording: a calling agent should be able to
read it and know what it needs, what it does, and what it returns, without
reading any code.

Design principles, in priority order:

1. **Surface-agnostic.** Nothing in the artifact names Playwright, CSS, XPath or
   pixels. Targets are described the way a human operator would describe them.
   This is the seam that lets a desktop resolver execute the same flow.
2. **Elements are shared, steps are thin.** A registry of named elements, with
   steps referencing them. This is what makes cross-tenant overrides a small
   diff rather than a rewrite.
3. **Outcomes are declared, not inferred.** "No such member" is a legitimate
   answer. The artifact says so explicitly so replay never has to guess whether
   something is a result or a crash.
4. **Robustness is recorded, not assumed.** Every locator carries a confidence
   level and every resolution is logged, so drift is observable.

Storage: one JSON file per capability version, at
`capabilities/{id}/{version}.json`. Files, not a database. A single-tenant demo
does not need one, and the brief explicitly does not reward premature
infrastructure.

---

## Top-level shape

```json
{
  "schema_version": "1.0",
  "capability": { ... },
  "target": { ... },
  "inputs": [ ... ],
  "outputs": [ ... ],
  "elements": { ... },
  "steps": [ ... ],
  "outcomes": [ ... ],
  "policy": { ... },
  "provenance": { ... }
}
```

### Three versions, deliberately separate

Conflating these is a common mistake. They change for different reasons.

- `schema_version` — the format itself. Changes when the engine's parser changes.
- `capability.version` — this flow. Changes when the recorded steps change.
- `target.app_version` — the observed version of the app it was recorded against.
  Never changes on its own. It is a *drift signal*: replay compares it to what it
  sees live and warns on mismatch.

---

## capability

```json
"capability": {
  "id": "member_savings_balance",
  "version": "1.0.0",
  "name": "Look up member savings balance",
  "description": "Given a member reference, return the member's current savings account balance.",
  "status": "draft",
  "derived_from": null
}
```

`status` is `draft | approved | deprecated`. Discovery always writes `draft`.
Unattended replay can be gated on `approved`. This is one line of code and it is
the cheapest version of the "confidence and approval" stretch goal.

`derived_from` is optional and records ancestry (`capability_id@version`) when a
capability was **forked** because some tenant's divergence exceeded what an
overlay can express. The loader raises an error naming this field when it
rejects an over-reaching overlay. No forking logic is built on it yet; it exists
so the provenance is recordable the moment the first fork happens.

---

## target

```json
"target": {
  "surface": "web",
  "app": "coreserv",
  "app_version": "4.2.1",
  "tenant": "northridge",
  "base_url": "http://localhost:8800",
  "entry_path": "/search",
  "auth": {
    "mode": "form_login",
    "path": "/",
    "credentials_ref": {
      "username": "CORESERV_USERNAME",
      "password": "CORESERV_PASSWORD"
    },
    "success_check": { "type": "element_present", "element": "member_ref_field" }
  }
}
```

`surface` is what tells the engine which resolver to load. Today only `web`
exists. A `desktop` value would select a UIA/AX resolver against the same steps.

`base_url` is environment config, not flow logic. It is overridable at replay
time so the same artifact runs against a different tenant's instance.

### auth: session establishment is not a recorded step

CoreServ bounces any unauthenticated request to a login page, so `entry_path`
is understood as **post-authentication**. The engine runs the `auth` block
before step 1 in both discovery and replay, then asserts `success_check` before
proceeding.

Login is deliberately *not* recorded as flow steps. Doing so would push a
credential parameter into every capability's public contract, and the recorded
flow should be the business flow.

- `mode` is an enum. Only `form_login` is implemented; `sso`, `basic` and
  `preauthenticated_session` are declared so the shape does not have to change
  when one of them arrives. The engine rejects an unimplemented mode at runtime.
- `credentials_ref` maps a credential role to the **name of an environment
  variable**, never a value. Artifacts are committed to a public repo, so the
  schema validates these against an env-var naming pattern and rejects anything
  that looks like a pasted secret.
- Resolution happens before any browser action. Only variable names and a
  resolved/unresolved boolean are ever logged — never a value.

A failure here is classified `auth_failure`, distinct from a business outcome, a
recoverable condition and a hard failure: it means *our own* credentials or
session are bad, which is not the caller's problem and is not retryable.

---

## inputs

```json
"inputs": [
  {
    "name": "member_ref",
    "type": "string",
    "required": true,
    "pattern": "^[0-9]{5}$",
    "description": "The identifier used to locate the member. On northridge this is the five-digit member ID.",
    "sensitivity": "identifier",
    "example": "10001"
  }
]
```

The parameter is `member_ref`, not `member_id`, because *what identifies a
member differs per tenant*: northridge searches by a five-digit member ID,
cascade by a ten-digit account number. Naming the parameter after one tenant's
concept would have forced either a misleading name or a forked capability. The
name and type are fixed across tenants; only `pattern`, `description` and
`example` may be specialised by an overlay.

`sensitivity` is one of `public | identifier | pii | secret`. It drives
redaction: `pii` and `secret` are never written to logs or evidence in full,
`identifier` is partially masked. This is how the safety requirement is enforced
by the data model rather than by remembering to call a redact function.

`pattern` is validated *before* the browser opens. A malformed member ID is a
caller error, not a replay failure, and should be reported as such.

---

## outputs

```json
"outputs": [
  {
    "name": "savings_balance",
    "type": "money",
    "required": true,
    "description": "Current balance of the member's savings account"
  },
  {
    "name": "member_name",
    "type": "string",
    "required": false,
    "sensitivity": "pii",
    "description": "Member display name, returned for caller verification"
  }
]
```

Declaring outputs up front means replay can verify it actually produced what it
promised. A run that reaches the confirmation screen but extracts nothing is a
failure, not a success.

`type` drives a coercion step: `money` strips currency symbols and commas and
parses to a decimal. Returning the string "$1,240.55" to a calling agent is
passing the parsing problem downstream.

---

## elements

The registry. This is the load-bearing design decision.

```json
"elements": {
  "member_ref_field": {
    "description": "Member identifier input on the search form",
    "frame": "content",
    "chain": [
      {
        "strategy": "role_name",
        "role": "textbox",
        "name": "Member ID",
        "confidence": "high"
      },
      {
        "strategy": "role_ordinal",
        "role": "textbox",
        "index": 1,
        "confidence": "low",
        "brittle": true
      }
    ],
    "notes": "Accessible name is inferred by the perception layer from the preceding table cell; CoreServ provides no label binding. Label text is tenant-specific."
  },

  "results_view_link": {
    "description": "The View link for a specific member's row in the results table",
    "frame": "content",
    "chain": [
      {
        "strategy": "role_name_scoped",
        "role": "link",
        "name": "View",
        "scope": { "role": "row", "contains": "{{member_ref}}" },
        "confidence": "high"
      },
      {
        "strategy": "role_name",
        "role": "link",
        "name": "View",
        "confidence": "low",
        "brittle": true,
        "notes": "Ambiguous when the result set has more than one row"
      }
    ]
  },

  "savings_balance_cell": {
    "description": "Balance value in the row of the account table whose type is Savings",
    "frame": "content",
    "chain": [
      {
        "strategy": "cell_in_row",
        "scope": { "role": "row", "contains": "Savings" },
        "column_header": "Balance",
        "confidence": "high"
      }
    ]
  }
}
```

**Why a registry rather than per-step chains.** A tenant overlay becomes a small
override of registry keys, leaving the flow completely untouched. See the
[tenant overlays](#tenant-overlays) section below for the full rules and the
shipped cascade example.

**Strategies.** Keep the set small and semantic:

| Strategy | Meaning |
|---|---|
| `role_name` | Role plus accessible name, document-wide |
| `role_name_scoped` | The same, within a container (a row, a form, a region) |
| `cell_in_row` | Cell within a content-matched row, addressed by `column_header` **or** `column_index` (exactly one) |
| `role_ordinal` | Nth control of a role. Always `brittle: true`, last resort only |

`scope.contains` supports `{{param}}` substitution, which is what makes
"the View link in the row for member 12345" expressible rather than
"the third View link".

`cell_in_row` accepts `column_index` as an alternative to `column_header`
because CoreServ's member-detail screen is a label/value table with **no column
headers at all** — the only way to say "the value cell of the Name row" is by
position *within a content-matched row*. That is meaningfully different from
`role_ordinal`, which is position within the whole document, and is why it is
`medium` confidence rather than `brittle`.

**Innermost match wins.** When several nested containers satisfy a `scope`, the
resolver takes the innermost one. This is not a detail — CoreServ nests tables
three deep, so an outer wrapper row's subtree text transitively contains every
inner row's text. Matching outermost-first selects the page shell instead of the
data row. (Discovered the hard way: the a11y diagnostic's own row XPath matched
three elements before it was scoped this way.)

**confidence and brittle** are not decoration. Replay records which rung of the
chain actually resolved. Falling through to a `brittle` rung is a drift signal
worth surfacing even when the run succeeds.

---

## steps

```json
"steps": [
  {
    "id": "s1",
    "action": "navigate",
    "path": "/search",
    "risk": "safe",
    "checkpoint": {
      "type": "element_present",
      "element": "member_ref_field",
      "timeout_ms": 5000
    }
  },
  {
    "id": "s2",
    "action": "fill",
    "element": "member_ref_field",
    "value": "{{member_ref}}",
    "risk": "safe"
  },
  {
    "id": "s3",
    "action": "click",
    "element": "search_submit",
    "risk": "safe",
    "checkpoint": {
      "type": "any_of",
      "conditions": [
        { "type": "element_present", "element": "results_grid" },
        { "type": "text_present", "text": "No records match" }
      ],
      "timeout_ms": 8000
    },
    "outcomes": ["member_not_found"]
  },
  {
    "id": "s4",
    "action": "click",
    "element": "results_view_link",
    "risk": "safe",
    "checkpoint": {
      "type": "element_present",
      "element": "member_detail_heading",
      "timeout_ms": 5000
    },
    "outcomes": ["permission_denied"]
  },
  {
    "id": "s5",
    "action": "extract",
    "element": "savings_balance_cell",
    "into": "savings_balance",
    "risk": "safe"
  },
  {
    "id": "s6",
    "action": "extract",
    "element": "member_name_cell",
    "into": "member_name",
    "risk": "safe"
  }
]
```

**Actions.** Deliberately small: `navigate`, `click`, `fill`, `select`, `check`,
`extract`, `wait_for`. A small vocabulary is a constraint on the LLM during
discovery as well as a simpler replay engine. Anything not in this list cannot be
recorded, which is itself a safety property.

`wait_for` is declared but **unused in 1.0.0** (settling open question 3):
per-step checkpoints already carry `timeout_ms`, and the maintenance
interstitial is engine-owned recovery rather than a recorded step. It stays in
the vocabulary because it costs one enum value and keeps a flow that genuinely
needs a bare wait from forcing a schema bump.

**One extract per output** (settling open question 1). `extract` writes exactly
one element's value into exactly one declared output. Two outputs means two
steps, which keeps each extraction independently checkpointable and keeps the
step-to-output mapping legible to a reviewer.

**`risk` on every step.** `safe` (reversible, read-only) or `risky`
(writes, submits, irreversible). Replay policy decides what to do with `risky`.
For a read capability every step is `safe`, which is the honest answer and worth
stating.

**Checkpoints are per-step, not just terminal.** A click that silently does
nothing is the most common failure mode in UI automation, and a terminal-only
checkpoint tells you the flow failed but not where. Per-step checkpoints give you
"step s4 expected the member detail heading, observed the results table still
present", which is the debuggable error the brief asks for.

`any_of` matters at s3: search legitimately produces either a results table or a
not-found page. Both are successful *page loads*. Only one is a successful
*step*. That distinction is exactly the business-outcome-versus-failure line.

---

## outcomes

```json
"outcomes": [
  {
    "name": "member_not_found",
    "classification": "business_outcome",
    "detect": { "type": "text_present", "text": "No records match" },
    "terminal": true,
    "message": "No member exists with the supplied identifier."
  },
  {
    "name": "permission_denied",
    "classification": "business_outcome",
    "detect": { "type": "text_present", "text": "You do not have permission" },
    "terminal": true,
    "message": "The member record exists but is restricted."
  }
]
```

These are the *flow-specific* ones, declared here because only this flow knows
that a not-found search is a legitimate answer rather than a fault.

**Universal conditions live in the engine, not the artifact.** Session expiry, the
500 page, and the maintenance interstitial can occur on any step of any flow, so
duplicating them into every artifact would be noise. The engine owns:

| Condition | Classification | Engine behavior |
|---|---|---|
| Maintenance interstitial | `recoverable` | Dismiss via Continue, retry the step, max 2 |
| Slow load / timeout | `recoverable` | Backoff retry, max 2 |
| Session expired | `hard_failure` | Stop. Re-auth is out of scope and stated as such |
| Server error page | `hard_failure` | Stop, capture evidence |
| Element unresolvable | `hard_failure` | Stop, escalate to human |
| Credentials unset or login rejected | `auth_failure` | Stop before step 1. Not retryable |
| Inputs fail contract validation | `caller_error` | Reject before the browser opens |

That split is the answer to "where does error detection live": **universal in the
engine, flow-specific in the artifact.**

### Two classification vocabularies, deliberately different sizes

An artifact's `outcomes[].classification` may only be `business_outcome`,
`recoverable` or `hard_failure`. The engine's *result* classification is wider:
it adds `success`, `auth_failure` and `caller_error`.

The split is not cosmetic. A recorded flow can legitimately assert things about
the app it drives ("this screen means no such member"). It cannot meaningfully
assert anything about whether *our* credentials were configured or whether the
*caller* passed a well-formed argument — those are properties of the system and
the request, not of the flow, and they are both decided before any step runs.

---

## policy

```json
"policy": {
  "allowed_origins": ["http://localhost:8800"],
  "allowed_paths": ["/", "/search", "/search/results", "/member/*"],
  "allowed_actions": ["navigate", "click", "fill", "extract"],
  "risky_action_handling": "require_confirmation",
  "max_steps": 25,
  "timeout_ms": 120000
}
```

`allowed_actions` lists only what this capability's steps actually use, not the
whole vocabulary — the schema rejects an artifact whose steps use an action the
policy omits, so an over-broad allowlist is a silent widening with no benefit.
`/` is present because the `auth` block performs the login there and the auth
phase is policy-checked like any other navigation.

Enforced at the executor boundary, immediately before any action runs, not in the
prompt. A prompt-level guardrail is a suggestion. An executor-level one is a
control. This applies identically to discovery and replay, which is the point:
the same policy layer sits under both paths.

Note `allowed_paths` excludes `/_faults`. The agent must not be able to
manipulate the app's fault state, which is a small but real demonstration that
the allowlist constrains something meaningful.

---

## provenance

```json
"provenance": {
  "source": "discovery",
  "discovered_at": "2026-08-18T21:14:03Z",
  "goal": "Look up member 10001 and read their current savings balance",
  "model": "claude-sonnet-5",
  "discovery_run_id": "run_a3f2c1",
  "steps_attempted": 11,
  "steps_recorded": 5,
  "human_interventions": 0
}
```

`source` is `discovery | hand_written`. The shipped 1.0.0 artifact is
`hand_written` and says so: the target format was designed before the replay
engine, so pretending a model produced it would be the one dishonest field in a
block whose entire purpose is honesty. `model` and `discovery_run_id` are null
in that case.

`steps_attempted` versus `steps_recorded` is the honest record: the model
wandered, and the artifact captures the path that worked rather than the raw
transcript. That decoupling is explicitly asked for in section 3.2.

The goal string is retained; raw model reasoning is not. Transcripts go to
evidence, not into the capability contract.

---

## Tenant overlays

Separate files, at `capabilities/{id}/tenants/{tenant}.json` (settling open
question 2). A tenant override never requires editing the base artifact, so the
base stays reviewable on its own and a new tenant is an added file rather than a
diff to shared code.

```json
{
  "extends": "member_savings_balance@1.0.0",
  "tenant": "cascade",
  "input_overrides": {
    "member_ref": {
      "pattern": "^[0-9]{10}$",
      "description": "On cascade this is the ten-digit account number, not the member ID.",
      "example": "4471820019"
    }
  },
  "element_overrides": {
    "member_ref_field": {
      "chain": [
        { "strategy": "role_name", "role": "textbox", "name": "Account Number", "confidence": "high" }
      ]
    },
    "results_grid": {
      "chain": [
        { "strategy": "role_name", "role": "columnheader", "name": "Account Number", "confidence": "high" }
      ]
    }
  },
  "target_overrides": { "app_version": "4.2.3" }
}
```

### What an overlay may and may not change

| May override | May **not** override |
|---|---|
| Any element's `chain`, `description`, `frame`, `notes` | `steps` — a different flow is a different capability |
| An input's `pattern`, `description`, `example` | An input's `name`, `type`, `required`, `sensitivity` |
| `target.base_url`, `target.app_version` | `outputs` — callers depend on the returned contract |
| `policy.allowed_paths`, `policy.allowed_actions` — **narrowing only** | `outcomes`, `capability`, `provenance`, `schema_version` |
| | `target.surface`, `target.app`, `target.entry_path`, `target.auth` |
| | `policy.allowed_origins`, `risky_action_handling`, `max_steps`, `timeout_ms` |

The allowlist rule is one-directional on purpose: an overlay that could *widen*
`allowed_paths` would make the base artifact's guardrail unreviewable, since you
would have to read every tenant file to know what the capability is permitted to
do. The loader validates that each overridden path is covered by a base entry
(literally or by glob) and rejects anything else.

An overlay may only *specialise* an input or element that already exists —
introducing a new one is rejected, since a parameter absent from the base is a
parameter no caller reading the base contract knows to supply.

When an overlay reaches past these bounds the loader fails with an error naming
the specific field and pointing at the remedy: **fork the capability** and record
the ancestry in `capability.derived_from`. That message is the design statement —
overlays express configuration drift, forks express behavioural divergence.

### Why cascade is the honest test case

Cascade is not a relabelling exercise. Same routes, same flow, same vendor
product — but it searches by a *ten-digit account number* where northridge
searches by a *five-digit member ID*, and its results grid drops the Member ID
column entirely. That is why:

- the parameter is `member_ref` rather than `member_id` (see [inputs](#inputs)),
- the overlay must be able to override `pattern`, not just element chains,
- and `results_view_link` needs **no** override at all: its row scope matches on
  `{{member_ref}}`, and each tenant's grid displays whichever identifier that
  tenant searches by, so one chain resolves correctly in both.

A tenant model that only handled label changes would have looked cleaner in a
demo and failed on the first real institution.

---

## Settled open questions

1. **One extract per output.** See [steps](#steps).
2. **Overlays are separate files.** See above.
3. **`wait_for` stays in the vocabulary but is unused in 1.0.0.** See
   [steps](#steps).
