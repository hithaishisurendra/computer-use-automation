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

from capability.redaction import Scrubber
from capability.schema import Artifact
from capability.validate import describe_credentials, redact


class EvidenceWriter:
    def __init__(self, directory: Path, artifact: Artifact):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.artifact = artifact
        self.log_path = self.dir / "steps.jsonl"
        # Shape-based rules only by default: a replay runs against live data
        # it cannot enumerate. Known literals (this run's credentials) are
        # registered on top via register_secrets.
        self.scrubber = Scrubber()

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
        self.scrubber.register_secrets(values)

    def _scrub_text(self, text: str) -> str:
        return self.scrubber.scrub(text)

    def _scrub_obj(self, obj: Any) -> Any:
        return self.scrubber.scrub_obj(obj)

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
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **self._scrub_obj(payload),
        }
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

    def describe_auth(self) -> dict[str, Any]:
        """Only env var names and resolution booleans -- never a value."""
        return describe_credentials(self.artifact)

    def write_result(self, result) -> None:
        payload = result.as_dict()
        if payload.get("outputs"):
            payload["outputs"] = self.redact_outputs(payload["outputs"])
        self.log("result", payload)
        (self.dir / "result.json").write_text(
            json.dumps(self._scrub_obj(payload), indent=2, default=str), encoding="utf-8"
        )

    async def capture_failure(self, page, frames: dict[str, Optional[dict]]) -> dict[str, str]:
        """Screenshot plus the compact accessibility snapshot where it stopped."""
        from perception.tree import filter_tree, to_compact_text

        paths: dict[str, str] = {}

        shot = self.dir / "failure.png"
        try:
            await page.screenshot(path=str(shot), full_page=True)
            paths["screenshot"] = str(shot)
        except Exception:
            pass

        lines = []
        for name, tree in frames.items():
            lines.append(f"--- FRAME {name!r} ---")
            lines.append(to_compact_text(filter_tree(tree)) or "(empty)")
        snapshot = self.dir / "failure_snapshot.txt"
        snapshot.write_text(self._scrub_text("\n".join(lines)), encoding="utf-8")
        paths["snapshot"] = str(snapshot)

        return paths
