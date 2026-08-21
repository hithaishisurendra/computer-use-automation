"""Mock operator surface.

Deliberately minimal, per the brief's scope note: a full real-time
co-browsing console is out of scope, but the *handoff mechanism* is real.
What is genuine here and would survive a real console unchanged:

- the run pauses and automation is provably locked out (`ControlledSession`),
- the operator drives the same live browser context, not a copy,
- resume and abort are explicit signals, not timeouts,
- what the human did is captured as evidence.

What is mocked is only the presentation: a terminal prompt instead of a web
UI streaming the session. **A real console replaces `ConsoleOperator` and
nothing else.** It would attach to the same `ControlledSession`, render the
same `InterventionRequest`, and return the same `OperatorDecision` -- via
CDP screencast or a remote-debugging bridge rather than by handing the human
a browser window that is already on their screen. The interface is the seam;
the terminal is the stub behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional, Protocol

from escalation.request import InterventionRequest


class Decision(str, Enum):
    RESUME = "resume"
    ABORT = "abort"


@dataclass
class OperatorDecision:
    decision: Decision
    notes: str = ""
    operator: str = "unknown"
    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def resumed(self) -> bool:
        return self.decision is Decision.RESUME

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "notes": self.notes,
            "operator": self.operator,
            "decided_at": self.decided_at,
        }


class OperatorSurface(Protocol):
    """Whatever a human interacts with. Swap freely; the handoff is unchanged."""

    def handle(self, request: InterventionRequest) -> OperatorDecision: ...


class ConsoleOperator:
    """Terminal stub. Prints the request, waits for the human to finish.

    The browser is already headed and already on the stuck page, so the
    human simply uses it. That is the crude part -- a real console would
    stream the session instead of assuming the operator is sitting at the
    machine running the agent -- but the control transfer around it is real.
    """

    def __init__(self, operator_name: str = "console-operator"):
        self.operator_name = operator_name

    def handle(self, request: InterventionRequest) -> OperatorDecision:
        print()
        print("=" * 72)
        for line in request.summary_lines():
            print(line)
        print("-" * 72)
        print("  Automation is PAUSED and cannot act until you hand control back.")
        print("  The browser window is the same live session. Drive it directly.")
        print()
        print("  When finished:  [r] resume the run   [a] abort")
        print("=" * 72)

        while True:
            try:
                answer = input("  decision [r/a]: ").strip().lower()
            except EOFError:
                # No terminal attached: abort rather than silently resuming a
                # run nobody supervised.
                return OperatorDecision(
                    Decision.ABORT,
                    notes="no interactive terminal available",
                    operator=self.operator_name,
                )
            if answer in ("r", "resume"):
                notes = _prompt("  what did you do? ")
                return OperatorDecision(Decision.RESUME, notes=notes, operator=self.operator_name)
            if answer in ("a", "abort"):
                notes = _prompt("  why abort? ")
                return OperatorDecision(Decision.ABORT, notes=notes, operator=self.operator_name)
            print("  expected 'r' or 'a'")


def _prompt(label: str) -> str:
    try:
        return input(label).strip()
    except EOFError:
        return ""


@dataclass
class ScriptedOperator:
    """Non-interactive operator for tests and unattended demonstration.

    Holds a queue of decisions and an optional callback that runs *while the
    human holds control*, standing in for the manual steps a person would
    take on the live page. That callback is what lets a handoff be exercised
    end to end without a person present.
    """

    decisions: list[OperatorDecision] = field(default_factory=list)
    on_control: Optional[Any] = None
    seen: list[InterventionRequest] = field(default_factory=list)
    operator_name: str = "scripted-operator"

    def handle(self, request: InterventionRequest) -> OperatorDecision:
        self.seen.append(request)
        if self.on_control is not None:
            self.on_control(request)
        if self.decisions:
            return self.decisions.pop(0)
        return OperatorDecision(
            Decision.ABORT, notes="no scripted decision left", operator=self.operator_name
        )
