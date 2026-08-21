"""Record what the human did while holding control.

Scope, stated plainly: this captures the *effect* of the human's work, not
their keystrokes. It snapshots URL and accessibility state when control is
ceded and again when it is handed back, and diffs the two. So an operator
who dismissed a dialog, searched again and opened the right record shows up
as "url changed, these controls appeared, these disappeared" -- enough for a
reviewer to see what changed and for the run to be audited, not enough to
replay their actions.

**The seam for full action-level capture.** Recording individual actions
means listening to the session rather than sampling it, and the hook already
exists: the operator drives the same Playwright context, so a CDP listener
(`Page.addScriptToEvaluateOnNewDocument` plus DOM event capture, or
`Input.dispatch*` domain events) would emit click/type/navigate events with
their targets. Those events would then go through `perception` to resolve
each target to role+name -- the same translation `discovery.recorder` does --
which is what would let a human's manual fix be *promoted into the artifact*
as recorded steps rather than merely logged. That is the natural next
increment and the reason the diff below is shaped as before/after
observations rather than free text: the consumer of both is the same.

Deliberately not doing it here: an action recorder that captures a human
operating a bank's back office is a surveillance surface, and it needs a
retention and consent story before it needs an implementation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from capability.redaction import seed_data_scrubber


def _control_lines(snapshot: str) -> set[str]:
    """Interactive controls in a compact snapshot, as comparable lines."""
    interesting = ("link ", "button ", "textbox", "combobox", "checkbox", "radio")
    return {
        line.strip()
        for line in (snapshot or "").splitlines()
        if line.strip().startswith(interesting)
    }


@dataclass
class HumanActivity:
    """What changed on the session while the human held control."""

    started_at: str
    ended_at: Optional[str] = None
    url_before: Optional[str] = None
    url_after: Optional[str] = None
    controls_appeared: list[str] = field(default_factory=list)
    controls_disappeared: list[str] = field(default_factory=list)
    notes: str = ""
    operator: str = ""
    decision: str = ""

    @property
    def url_changed(self) -> bool:
        return self.url_before != self.url_after

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "operator": self.operator,
            "decision": self.decision,
            "notes": self.notes,
            "url_before": self.url_before,
            "url_after": self.url_after,
            "url_changed": self.url_changed,
            "controls_appeared": sorted(self.controls_appeared),
            "controls_disappeared": sorted(self.controls_disappeared),
            "capture_scope": (
                "effect-level: URL and accessibility-state diff across the handoff. "
                "Individual actions are not recorded -- see module docstring for the seam."
            ),
        }


class HumanActionCapture:
    """Brackets a handoff and diffs the session across it."""

    def __init__(self, page, perceive):
        self.page = page
        self.perceive = perceive
        self._before_snapshot: str = ""
        self.activity: Optional[HumanActivity] = None

    async def _snapshot(self) -> tuple[str, str]:
        from perception.tree import filter_tree, to_compact_text

        url = self.page.url
        try:
            url = next((f.url for f in self.page.frames if f.name == "content"), url)
        except Exception:
            pass
        try:
            frames = await self.perceive()
            text = "\n".join(
                to_compact_text(filter_tree(tree)) for tree in frames.values()
            )
        except Exception:
            text = ""
        return url, text

    async def begin(self) -> HumanActivity:
        url, snapshot = await self._snapshot()
        self._before_snapshot = snapshot
        self.activity = HumanActivity(
            started_at=datetime.now(timezone.utc).isoformat(), url_before=url
        )
        return self.activity

    async def end(self, decision: Any = None) -> HumanActivity:
        assert self.activity is not None, "begin() must be called before end()"
        url, snapshot = await self._snapshot()

        before = _control_lines(self._before_snapshot)
        after = _control_lines(snapshot)

        self.activity.ended_at = datetime.now(timezone.utc).isoformat()
        self.activity.url_after = url
        self.activity.controls_appeared = sorted(after - before)
        self.activity.controls_disappeared = sorted(before - after)
        if decision is not None:
            self.activity.notes = getattr(decision, "notes", "")
            self.activity.operator = getattr(decision, "operator", "")
            self.activity.decision = getattr(
                getattr(decision, "decision", None), "value", ""
            )
        return self.activity


def write_activity(
    run_id: str, activity: HumanActivity, session_state: dict[str, Any], root: str | Path
) -> Path:
    """Persist the handoff record, scrubbed."""
    directory = Path(root) / run_id
    directory.mkdir(parents=True, exist_ok=True)
    scrubber = seed_data_scrubber()
    payload = scrubber.scrub_obj(
        {"human_activity": activity.as_dict(), "control": session_state}
    )
    path = directory / "handoff.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path
