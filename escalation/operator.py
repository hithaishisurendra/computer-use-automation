"""Mock operator surface.

Deliberately minimal, per the brief's scope note: a full real-time
co-browsing console is out of scope, but the *handoff mechanism* is real.
What is genuine here and would survive a real console unchanged:

- the run pauses and automation is provably locked out (`ControlledSession`),
- the operator drives the same live browser context, not a copy,
- the operator's press is an explicit signal to LOOK, and the blocked
  step's checkpoint decides the outcome -- never the button,
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
    """What an operator surface sends back.

    `DONE` is the only one an operator surface should send now. It means "I
    have finished with the session; look at it" -- a trigger for the engine
    to evaluate the blocked step's checkpoint, NOT a claim about what
    happened. The outcome is decided by the checkpoint against the live page.

    `RESUME` and `ABORT` remain as accepted aliases so existing surfaces and
    recorded evidence keep working. They no longer mean anything different:
    both are treated as DONE, because the previous design -- where the
    operator's choice *was* the outcome -- is exactly the defect this
    replaces. An operator who aborted after performing the step had the run
    record that nothing happened, which in a bank is a reconciliation problem.
    """

    DONE = "done"
    RESUME = "resume"
    ABORT = "abort"


@dataclass
class OperatorDecision:
    decision: Decision
    notes: str = ""
    operator: str = "unknown"
    #: True when nobody answered within the pause deadline. The checkpoint is
    #: still evaluated -- an operator may have performed the step and simply
    #: never pressed the button -- but the classification differs when it
    #: fails, because "nobody came" and "someone looked and did not do it"
    #: are different operational facts.
    timed_out: bool = False
    decided_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @property
    def resumed(self) -> bool:
        """Deprecated. The engine no longer branches on this.

        Kept because recorded evidence and existing tests reference it. It
        must never regain control over the outcome: the checkpoint decides.
        """
        return self.decision is not Decision.ABORT

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "notes": self.notes,
            "operator": self.operator,
            "timed_out": self.timed_out,
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
        print("  If you approve of this step, perform it in that window.")
        print("  Then press [d] and the run will CHECK the page to see what happened.")
        print("  You are not telling it the outcome -- it looks for itself.")
        print("=" * 72)

        while True:
            try:
                answer = input("  press [d] when you are done: ").strip().lower()
            except EOFError:
                # No terminal attached. Still DONE rather than a claim that
                # nothing happened: the checkpoint is evaluated either way,
                # and asserting "not performed" without looking is the bug
                # this design removes.
                return OperatorDecision(
                    Decision.DONE,
                    notes="no interactive terminal available",
                    operator=self.operator_name,
                )
            # r/a are accepted for muscle memory and old scripts. They mean
            # the same thing now -- the checkpoint decides, not the key.
            if answer in ("d", "done", "", "r", "resume", "a", "abort"):
                notes = _prompt("  notes (optional, audit trail only): ")
                return OperatorDecision(
                    Decision.DONE, notes=notes, operator=self.operator_name
                )
            print("  press 'd' (or Enter) when you have finished with the session")


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
            Decision.DONE, notes="no scripted decision left", operator=self.operator_name
        )
