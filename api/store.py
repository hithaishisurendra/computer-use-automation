"""File-backed run history, so a demo survives a server restart.

In-memory history is fine for a process and useless for a demonstration: the
runs from five minutes ago are exactly the evidence you want on screen, and
restarting the API to pick up a change wiped them. This is a JSON file rather
than a database on purpose -- the brief explicitly does not reward scaling
infrastructure, and a file is enough to outlive a process.

Two things it deliberately does NOT do.

**It does not persist a parked run.** A run waiting for an operator holds a
live browser, a thread and an authenticated session on the target
application. None of those survive the process. Writing the record without
the session would put an intervention in the queue that nobody can act on --
which is precisely the defect the old unattended `202` had, where the
response named a session that had already been torn down. A parked run whose
server dies is gone, and the honest thing is to say so rather than to leave a
ghost in the list.

**It does not re-scrub.** Bodies arrive here having already been through the
run's own sink, and they are written back out through a sink again on the way
to disk. Both are cheap and idempotent; neither is the place where a leak
would be caught, and pretending otherwise would invite someone to hand this
raw data on the assumption it gets cleaned here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from capability.sink import null_sink

DEFAULT_PATH = Path("evidence/dashboard/runs.json")
DEFAULT_CHAT_PATH = Path("evidence/dashboard/chat.json")

# A demo does not need unbounded history, and an ever-growing file eventually
# makes the Runs tab slow to render for no benefit. Oldest entries fall off.
MAX_RUNS = 200


def load(path: str | Path) -> dict[str, Any]:
    """Previously served runs, keyed by run id. Missing or corrupt is empty.

    A store that raises on a malformed file would take the whole API down at
    startup over a cosmetic feature. Losing the history is the correct
    failure here; refusing to serve the capability catalogue is not.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(path: str | Path, runs: dict[str, Any], sink=None) -> None:
    """Write the history, newest kept, through a sink.

    Failure is swallowed for the same reason `load` tolerates corruption: a
    read-only directory must not turn a completed run into an error the
    caller sees. The run already happened and its result has already been
    returned; the history file is a convenience.
    """
    trimmed = runs
    if len(runs) > MAX_RUNS:
        ordered = sorted(
            runs.items(), key=lambda kv: kv[1].get("started_at") or 0, reverse=True
        )
        trimmed = dict(ordered[:MAX_RUNS])
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        (sink or null_sink()).write_json(path, trimmed)
    except OSError:
        pass


def remember(
    runs: dict[str, Any],
    run_id: str,
    result: Optional[dict[str, Any]],
    capability: dict[str, str],
    started_at: Optional[float],
) -> dict[str, Any]:
    """Fold one finished run into the history and return the stored body."""
    body = dict(result or {})
    if not body.get("classification"):
        # The run's thread raised before producing a result. Store it as the
        # failure it was rather than as a shape with no classification: an
        # entry missing the field is one every reader has to defend against,
        # and the first one that did not took the Runs tab down with it.
        body["classification"] = "hard_failure"
        body.setdefault("message", "the run ended without producing a result")
    body.setdefault("run_id", run_id)
    body.setdefault("capability", capability)
    if started_at is not None:
        body.setdefault("started_at", started_at)
    runs[run_id] = body
    return body


def load_turns(path: str | Path) -> list[dict[str, Any]]:
    """The chat transcript. Missing or corrupt is an empty conversation.

    Server-side rather than in the browser, and that is not incidental. The
    dashboard is held to "no route out of the page except fetch", and a
    transcript in `localStorage` would be a new persistence surface the
    redaction sink never sees -- which is how every one of this project's
    leaks happened: not in the redaction code, but at a new place data could
    come to rest. Here it goes out through a sink like every other body.
    """
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def save_turns(path: str | Path, turns: list[dict[str, Any]], sink=None) -> None:
    """Write the transcript, most recent kept."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        (sink or null_sink()).write_json(path, turns[-MAX_TURNS:])
    except OSError:
        pass


# A demo conversation, not a support history.
MAX_TURNS = 60
