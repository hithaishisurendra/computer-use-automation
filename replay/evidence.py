"""Evidence: structured step log, plus a screenshot and snapshot on failure.

Redaction is applied on the way *out*, at the single point where anything is
written to disk, rather than being the caller's responsibility at each log
site. That placement matters: a redaction helper you have to remember to
call is one that eventually is not called, and this is regulated financial
data.

What is masked, driven by the `sensitivity` declared in the artifact:
  secret / pii  -> fully redacted
  identifier    -> all but the last two characters
  public        -> written as-is

Credentials never reach this module at all -- only environment variable
names and whether each resolved.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from capability.sink import RedactionSink
from capability.schema import Artifact
from capability.validate import describe_credentials, redact


class EvidenceWriter:
    def __init__(self, directory: Path, artifact: Artifact, profile=None):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.artifact = artifact
        self.log_path = self.dir / "steps.jsonl"
        # Shape rules plus whatever the app profile declares. A replay runs
        # against live data it cannot enumerate, so patterns carry the load;
        # a profile's literals are precision on top. Known literals for this
        # run (its credentials) are registered via register_secrets.
        self.sink = RedactionSink(profile, artifact)
        # Written before anything else, so evidence records what redaction was
        # actually configured with rather than leaving it to be inferred from
        # what did or did not get masked.
        self.log("redaction_configured", self.sink.describe())

    # -- redaction ----------------------------------------------------------

    def register_secrets(self, values) -> None:
        """Teach the scrubber which literal strings are credentials.

        This is the one place a credential value is allowed to reach the
        evidence layer, and it is write-only. It is necessary because
        CoreServ renders the logged-in username into its nav frame ("User:
        operator"), so a page snapshot captures a credential component that
        no output-level or sensitivity-driven masking would ever see:
        redaction that only covers declared fields misses exactly the values
        that leak through the surface itself.
        """
        self.sink.register_secrets(values)


    def redaction_warning(self) -> Optional[str]:
        """Non-None when this writer is scrubbing by shape rules only."""
        return self.sink.warning()

    def redact_outputs(self, outputs: dict[str, Any]) -> dict[str, Any]:
        """Mask declared outputs by their sensitivity for logging."""
        masked = {}
        for name, value in outputs.items():
            spec = self.artifact.output_map.get(name)
            sensitivity = spec.sensitivity if spec else "public"
            masked[name] = redact(value, sensitivity)
        return masked

    # -- writing ------------------------------------------------------------

    def log(self, event: str, payload: dict[str, Any]) -> None:
        self.sink.append_jsonl(self.log_path, {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **payload,
        })

    def describe_auth(self) -> dict[str, Any]:
        """Only env var names and resolution booleans -- never a value."""
        return describe_credentials(self.artifact)

    def write_result(self, result) -> None:
        payload = result.as_dict()
        if payload.get("outputs"):
            payload["outputs"] = self.redact_outputs(payload["outputs"])
        self.log("result", payload)
        self.sink.write_json(self.dir / "result.json", payload)

    async def capture_failure(self, page, frames: dict[str, Optional[dict]]) -> dict[str, str]:
        """Screenshot plus the compact accessibility snapshot where it stopped."""
        from perception.tree import filter_tree, to_compact_text

        paths: dict[str, str] = {}

        shot = self.dir / "failure.png"
        try:
            await page.screenshot(path=str(shot), full_page=True)
            # A screenshot of a member record shows everything the page
            # showed and no text pass can mask it. Recorded as unscrubbable
            # rather than quietly treated as clean.
            self.sink.note_unscrubbable(shot, "screenshot: image content cannot be text-scrubbed")
            paths["screenshot"] = str(shot)
        except Exception:
            pass

        lines = []
        for name, tree in frames.items():
            lines.append(f"--- FRAME {name!r} ---")
            lines.append(to_compact_text(filter_tree(tree)) or "(empty)")
        snapshot = self.sink.write_text(self.dir / "failure_snapshot.txt", "\n".join(lines))
        paths["snapshot"] = str(snapshot)

        return paths
