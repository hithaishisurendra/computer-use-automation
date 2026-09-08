# Dashboard seed state

`runs.json` and `chat.json` are what the **Runs** and **Chat** tabs show when
the API starts. They are committed so a fresh clone opens with something to
look at rather than two empty tables.

**This is seed state, not the evidence of record.** It is a filtered view of
real runs: the successes, business outcomes and not-performed results from a
working session, with debugging noise removed. The evidence of record is
untouched and lives elsewhere:

- `evidence/discovery/` every LLM run, its cycles, screenshots and cost
- `evidence/replay/` every replay's steps and failure captures
- `evidence/escalation/` intervention requests and handoff records

Every value here went through `capability/sink.py` before it was written, which
is why identifiers appear masked.

To start from empty, delete this directory and restart the API. It is rebuilt
as you use the system.
