# Computer-use automation: discover once, replay deterministically

An LLM drives a legacy back-office UI to accomplish a goal, the successful run
is recorded as a typed capability artifact, and that artifact replays
deterministically with **no model in the loop**.

The target is **CoreServ**, a purpose-written credit-union servicing console
in this repo: real `<frameset>`, tables nested three deep, server-generated
element ids that rotate on every render, no test ids, and seven injectable
faults. It is deliberately hostile.

Design write-up: [`REPORT.md`](REPORT.md). Runs and logs:
[`evidence/README.md`](evidence/README.md).

---

## Setup

Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium          # ~300MB, one time
```

### Environment variables

| Variable | Needed for | Notes |
|---|---|---|
| `GEMINI_API_KEY` | Discovery only | Free tier is enough. [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `CORESERV_USERNAME` | Discovery, replay | Any non-empty string — CoreServ accepts any password |
| `CORESERV_PASSWORD` | Discovery, replay | Any non-empty string |
| `ANTHROPIC_API_KEY` | Optional | Only if running discovery with `--provider anthropic` |

Put them in `.env` (gitignored, loaded automatically by discovery):

```bash
cat > .env <<'EOF'
GEMINI_API_KEY=your-key-here
EOF
export CORESERV_USERNAME=operator CORESERV_PASSWORD=devpassword
```

Artifacts store the **names** of credential variables, never values; a pasted
secret fails schema validation.

---

## Run CoreServ

In a separate terminal, and leave it running:

```bash
source .venv/bin/activate
uvicorn coreserv.main:app --port 8800
```

Open <http://localhost:8800> — log in with any username and password.

Faults are server-side flags, so the same artifact with the same inputs
produces a different outcome because the world changed, not because the run
was edited:

```bash
curl -X POST localhost:8800/_faults -H 'Content-Type: application/json' \
     -d '{"fault":"member_not_found","enabled":true}'
curl -X POST localhost:8800/_faults/reset
```

Faults: `member_not_found`, `restricted_member`, `maintenance_interstitial`,
`slow_response`, `session_expired`, `validation_error`, `server_error`.

---

## Demo path

### 1. Discovery — an LLM drives the app and emits an artifact

```bash
python -m discovery.run \
  --goal "Look up member 10001 and read their current savings balance" \
  --target http://localhost:8800 --entry /search
```

Takes ~40s. Writes `evidence/discovery/{run_id}/` containing `cycles.jsonl`
(every observe → decide → act cycle with the model's reasoning),
`artifact.json`, `summary.json` and per-step screenshots. The emitted artifact
is validated by loading it back before the run reports success.

### 2. Replay — the same artifact, no model

An artifact emitted by a real discovery run is already committed at
`capabilities/member_savings_balance_discovered/`, so this needs no API key
and no prior step. Run it **for a different member than it was discovered
on** — discovery saw 10001; this asks for 10003:

```bash
python -m replay.run \
  --capability member_savings_balance_discovered --version 1.0.0 \
  --input member_ref=10003
```

Prints a structured result. Exit codes: `0` success **and** business outcome,
`1` hard failure, `2` caller error, `3` auth failure — "no such member" is an
answer, not a crash.

Members with a savings account: `10001` (8320.10), `10003` (15230.44),
`10006` (3305.90), `10010` (640.75).

**If you ran discovery yourself in step 1**, replay your own artifact by
copying it where the loader can address it by capability id:

```bash
mkdir -p capabilities/my_discovered_capability
cp evidence/discovery/<run_id>/artifact.json \
   capabilities/my_discovered_capability/1.0.0.json
```

The capability id inside the artifact must match the directory name — set it
with `--capability-id my_discovered_capability` when you run discovery, or
use the committed copy above.

### 3. Error and outcome handling

```bash
# business outcome, exit 0 — a legitimate answer
curl -sX POST localhost:8800/_faults -H 'Content-Type: application/json' \
     -d '{"fault":"member_not_found","enabled":true}'
python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --input member_ref=10001

# hard failure, exit 1, escalation-eligible
curl -sX POST localhost:8800/_faults/reset
curl -sX POST localhost:8800/_faults -H 'Content-Type: application/json' \
     -d '{"fault":"session_expired","enabled":true}'
python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --input member_ref=10001

# caller error, exit 2 — no browser is ever opened
curl -sX POST localhost:8800/_faults/reset
python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --input member_ref=not-an-id
```

### 4. Escalation — a human takes over the live session

`--escalate` is **off by default** so unattended replay stays unattended. It
implies `--headed`, since a human cannot drive a headless browser.

```bash
curl -sX POST localhost:8800/_faults -H 'Content-Type: application/json' \
     -d '{"fault":"session_expired","enabled":true}'

python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --input member_ref=10001 --escalate
```

The run pauses, prints an intervention request (capability, step, expected vs
observed, URL, screenshot path), and waits. Automation is locked out — the
executor asserts ownership before every action. Drive the **already-open**
browser window; it is the same session, not a fresh one. Then press `r` to
resume or `a` to abort.

On resume the failed step's checkpoint is re-evaluated and the run continues
from there — it does not restart. Writes `evidence/escalation/{run_id}/` with
the request, the control-transfer log, and a diff of what changed while the
human held control.

Discovery has the same flag; there it triggers when the model calls `stuck`.

### 5. Cross-tenant — one artifact, two tenants

```bash
TENANT=cascade uvicorn coreserv.main:app --port 8800     # in place of northridge

python -m replay.run --capability member_savings_balance --version 1.0.0 \
  --tenant cascade --input member_ref=4471820019
```

Same artifact resolved through `capabilities/member_savings_balance/tenants/cascade.json`.
Cascade searches by ten-digit account number, relabels the field and reorders
the results grid; the overlay is two element chains, one input pattern and one
version string.

An overlay may now move a tenant to a different **host**, not just a
different port. `policy.allowed_origins` is derived from `target.base_url`
rather than stored beside it, so an overlay that sets `base_url` moves the
origin allowlist with it. Previously the two were independent and
`allowed_origins` was a forbidden overlay key, so a repointed artifact failed
its own origin check — which is why the cascade demo runs on 8800 in place of
northridge rather than alongside it. `evidence/README.md` records both runs.

---

## App profiles

Everything the engine knows about a *specific application* lives in
`config/app_profiles/{name}.json`, resolved from `target.app_profile`
(defaulting to `target.app`). Two ship: `coreserv` and `meridian`.

A profile carries the error markers that identify a session bounce, a server
error and a maintenance interstitial on that app; what recovering from each
actually means (`dismiss_control` vs `reload_step_url` — CoreServ's
interstitial is a button that re-renders in place, MERIDIAN's is a link that
navigates away and loses your position); the regex that reads the app version
off the page; whether the app uses frames and which one holds the working
area; the verbs the recorder treats as irreversible; the values the app
prints into its own chrome that must be scrubbed from evidence; and where its
known-sensitive literals come from.

Pointing at a new application should be writing one of these, not editing
`replay/` or `discovery/`. `tests/test_profile.py` asserts that structurally:
it fails if any application name or app-shaped selector appears in executable
code under `replay/`, `perception/` or `escalation/`.

```bash
python -m discovery.run --app coreserv --goal "..." --target http://localhost:8800
```

---

## Running without live services

**Most of the suite needs nothing running.** Tests that require CoreServ skip
rather than fail:

```bash
python -m pytest tests/ -q          # 178 passed, 21 skipped with nothing running
```

With CoreServ up on 8800, all **199** pass. No test needs an API key — the
model call is exercised by real runs in `evidence/`, not by unit tests, and
`tests/test_discovery.py` verifies the loop's logic against synthetic
accessibility trees.

The perception diagnostic needs CoreServ but no model:

```bash
python -m scripts.a11y_diagnostic --base-url http://localhost:8800
```

---

## Layout

```
coreserv/     the target app (proxy for a legacy back office)
perception/   accessibility-tree snapshot, filtering, label augmentation
capability/   artifact schema, loading, validation, redaction
discovery/    LLM loop + recorder      ─┐ both use perception;
replay/       deterministic engine     ─┘ neither imports the other
escalation/   control transfer, operator surface
capabilities/ saved artifacts + tenant overlays
evidence/     runs, logs, findings  ← start at evidence/README.md
docs/         schema spec, decisions log
```

## Evidence

[`evidence/README.md`](evidence/README.md) maps each directory to the
requirement it demonstrates: a real Gemini discovery run with its transcript,
the emitted artifact replaying for a different member, business outcomes, a
hard failure, a caller error, the escalation handoff, and the cascade tenant
run — plus two findings the exercise produced.
