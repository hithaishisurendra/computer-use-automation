"""Control transfer over a live browser session.

The brief's seam question is "there must be a way to know who is (or should
be) in control". That is this module. Ownership is explicit state on the
session rather than an implicit consequence of which code happens to be
running, because the two diverge exactly when it matters: an automation
callback firing mid-handoff, a retry loop that did not notice the pause.

Three states, and the middle one is the whole point:

    AUTOMATION  the agent may act
    HUMAN       an operator is driving; automation must not touch the page
    RELEASED    the session is finished; nobody may act

Automation asserts ownership before *every* action (see
`replay.executor.Executor.execute`). That check is what makes "paused"
enforceable rather than advisory -- a paused run that can still click is not
paused, it is racing the operator.

The session wraps one Playwright browser context for its whole lifetime. It
never creates a second one: handing an operator a fresh context would give
them a different session from the one that got stuck, losing the cookies,
the frame state and the half-completed flow that are the entire reason a
human was called.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional


class Control(str, Enum):
    AUTOMATION = "automation"
    HUMAN = "human"
    RELEASED = "released"


class ControlViolation(Exception):
    """Someone tried to act without holding control."""


class InvalidTransition(Exception):
    """A control transfer that the ownership model does not allow."""


# Once RELEASED the session is over; nothing may reacquire it. Everything
# else is a legal pause/resume cycle.
_ALLOWED: dict[Control, set[Control]] = {
    Control.AUTOMATION: {Control.HUMAN, Control.RELEASED},
    Control.HUMAN: {Control.AUTOMATION, Control.RELEASED},
    Control.RELEASED: set(),
}


@dataclass
class Transition:
    at: str
    from_owner: str
    to_owner: str
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "from": self.from_owner,
            "to": self.to_owner,
            "reason": self.reason,
        }


@dataclass
class ControlledSession:
    """One live browser session with explicit ownership."""

    page: Any
    context: Any = None
    owner: Control = Control.AUTOMATION
    transitions: list[Transition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.context is None and self.page is not None:
            self.context = getattr(self.page, "context", None)
        # Identity captured at construction so a handoff can be proven to
        # have kept the same context rather than quietly opened a new one.
        self._context_identity = id(self.context)
        self._page_identity = id(self.page)

    # -- identity -----------------------------------------------------------

    @property
    def context_identity(self) -> int:
        return self._context_identity

    def assert_same_session(self) -> None:
        """The session must be the one that got stuck, not a replacement."""
        if id(self.context) != self._context_identity or id(self.page) != self._page_identity:
            raise ControlViolation(
                "the browser context changed during the run; a handoff must keep the "
                "same live session, otherwise the operator is driving a different one "
                "than the automation was"
            )

    # -- ownership ----------------------------------------------------------

    def held_by_automation(self) -> bool:
        return self.owner is Control.AUTOMATION

    def assert_automation_may_act(self, what: str = "act") -> None:
        if self.owner is not Control.AUTOMATION:
            raise ControlViolation(
                f"automation attempted to {what} while control is held by "
                f"{self.owner.value!r}. The run is paused; the operator owns the session."
            )

    def _transfer(self, to: Control, reason: str) -> Transition:
        if to not in _ALLOWED[self.owner]:
            raise InvalidTransition(
                f"cannot move control from {self.owner.value!r} to {to.value!r}"
            )
        transition = Transition(
            at=datetime.now(timezone.utc).isoformat(),
            from_owner=self.owner.value,
            to_owner=to.value,
            reason=reason,
        )
        self.owner = to
        self.transitions.append(transition)
        return transition

    def hand_to_human(self, reason: str) -> Transition:
        """Pause automation and cede the live session to an operator."""
        self.assert_same_session()
        return self._transfer(Control.HUMAN, reason)

    def take_back(self, reason: str) -> Transition:
        """Operator hands control back; automation resumes on the same session."""
        self.assert_same_session()
        return self._transfer(Control.AUTOMATION, reason)

    def release(self, reason: str = "run finished") -> Transition:
        return self._transfer(Control.RELEASED, reason)

    # -- evidence -----------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner.value,
            "context_identity": self._context_identity,
            "transitions": [t.as_dict() for t in self.transitions],
        }
