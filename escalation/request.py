"""The intervention request: what a human needs to act on a stuck run.

The test of this object is whether an operator who did not watch the run can
pick it up cold. That means it carries *why* it stopped in the same
expected-versus-observed form the replay result uses -- "step s4 expected the
member detail heading, observed the results table still present" -- rather
than a bare failure string, plus the live URL, a screenshot, and the
accessibility snapshot of the page as it stands.

Everything is written through the app profile's scrubber, not a bare
pattern-only one. An intervention request is a whole-page capture by
construction: it exists precisely because something went wrong on a screen
nobody anticipated, so the values on it were never declared as fields and only
a value-aware scrubber will catch them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from capability.sink import RedactionSink, null_sink


@dataclass
class InterventionRequest:
    """A stuck run, packaged for a human."""

    run_id: str
    source: str  # replay | discovery
    goal: str
    reason: str
    classification: str

    capability_id: Optional[str] = None
    capability_version: Optional[str] = None
    step_id: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    url: Optional[str] = None
    screenshot_path: Optional[str] = None
    snapshot_path: Optional[str] = None
    snapshot: Optional[str] = None
    inputs_redacted: dict[str, str] = field(default_factory=dict)
    completed_steps: list[str] = field(default_factory=list)
    raised_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "source": self.source,
            "raised_at": self.raised_at,
            "capability": {"id": self.capability_id, "version": self.capability_version},
            "goal": self.goal,
            "stopped": {
                "step_id": self.step_id,
                "classification": self.classification,
                "reason": self.reason,
                "expected": self.expected,
                "observed": self.observed,
            },
            "state": {
                "url": self.url,
                "screenshot": self.screenshot_path,
                "snapshot_file": self.snapshot_path,
                "accessibility_snapshot": self.snapshot,
            },
            "inputs": self.inputs_redacted,
            "completed_steps": self.completed_steps,
        }

    def summary_lines(self) -> list[str]:
        """Human-readable rendering for the operator surface."""
        capability = self.capability_id or "(discovery run)"
        lines = [
            "INTERVENTION REQUIRED",
            f"  run          {self.run_id}  ({self.source})",
            f"  capability   {capability} {self.capability_version or ''}".rstrip(),
            f"  goal         {self.goal}",
            f"  stopped at   {self.step_id or 'n/a'}  [{self.classification}]",
            f"  reason       {self.reason}",
        ]
        if self.expected:
            lines.append(f"  expected     {self.expected}")
        if self.observed:
            lines.append(f"  observed     {self.observed}")
        lines += [
            f"  url          {self.url}",
            f"  completed    {', '.join(self.completed_steps) or 'none'}",
        ]
        if self.screenshot_path:
            lines.append(f"  screenshot   {self.screenshot_path}")
        if self.snapshot_path:
            lines.append(f"  snapshot     {self.snapshot_path}")
        return lines


def escalation_dir(run_id: str, root: str | Path = "evidence/escalation") -> Path:
    path = Path(root) / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_request(
    request: InterventionRequest, root: str | Path = "evidence/escalation",
    sink=None,
) -> Path:
    """Persist the request through the RUN's sink, not a fresh one.

    A fresh sink knows the app profile but not this run's credentials, and an
    intervention request is a whole-page capture of a screen nobody
    anticipated -- exactly where a credential the app renders into its own
    chrome shows up. Demonstrated: with a per-writer sink, a username the
    engine masked everywhere else survived into request.json.
    """
    directory = escalation_dir(request.run_id, root)
    return (sink or null_sink()).write_json(directory / "request.json", request.as_dict())


async def capture_state(
    page, perceive, directory: Path, sink=None, content_frame: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """Screenshot + compact accessibility snapshot of the stuck page.

    Returns (url, screenshot_path, snapshot_path, snapshot_text). Failures
    are swallowed: evidence capture must never be the reason an escalation
    cannot be raised.
    """
    from perception.tree import filter_tree, to_compact_text

    sink = sink or null_sink()
    url = None
    screenshot_path = None
    snapshot_path = None
    snapshot_text = None

    try:
        # Which frame carries "where the run is" is an application property,
        # so it comes from the profile. None means the document itself.
        url = (
            next((f.url for f in page.frames if f.name == content_frame), page.url)
            if content_frame is not None
            else page.url
        )
    except Exception:
        pass

    try:
        shot = directory / "stuck.png"
        await page.screenshot(path=str(shot), full_page=True)
        sink.note_unscrubbable(shot, "screenshot: image content cannot be text-scrubbed")
        screenshot_path = str(shot)
    except Exception:
        pass

    try:
        frames = await perceive()
        blocks = []
        for name, tree in frames.items():
            compact = to_compact_text(filter_tree(tree))
            if compact.strip():
                blocks.append(f"--- FRAME {name or '(top)'} ---\n{compact}")
        snapshot_text = sink.text("\n\n".join(blocks))
        snapshot_path = str(sink.write_text(directory / "stuck_snapshot.txt", snapshot_text))
    except Exception:
        pass

    return url, screenshot_path, snapshot_path, snapshot_text
