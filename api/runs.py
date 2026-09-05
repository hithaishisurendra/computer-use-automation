"""Runs that outlive the request that started them.

A replay that stops at a risky step has to stay alive: the browser window is
the operator's workspace, the session holds the half-completed flow, and the
whole point of the escalation model is that the human works on *that* session
rather than a fresh one. An HTTP request cannot hold it -- the response has to
return so the dashboard can render the intervention.

So an attended run executes on its own thread with its own event loop, and
the operator surface blocks that thread instead of blocking the server.
`ConsoleOperator` blocks on stdin; `PendingOperator` here blocks on an Event
that a later HTTP request sets. Same `OperatorSurface` protocol, same
`ReplayEngine`, same control-transfer machinery -- the dashboard is a second
operator surface, not a second escalation mechanism.

What this module deliberately does NOT do is drive the browser. The human
still performs the manual step in the live window on the machine running this
process. The dashboard signals resume; it does not host the session.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from escalation.operator import Decision, OperatorDecision
from escalation.request import InterventionRequest

# A paused run holds a browser, a thread and a live application session. If
# nobody ever answers, that leaks all three -- so a pause has a deadline and
# expires into an abort rather than waiting forever. Long enough for a person
# to actually do the work, short enough that a forgotten run cleans itself up.
DEFAULT_PAUSE_TIMEOUT_S = 30 * 60


class PendingOperator:
    """An operator surface a human reaches over HTTP.

    Structurally identical to ConsoleOperator: it receives the intervention
    request, blocks until a person decides, and returns their decision. The
    only difference is where the decision comes from.
    """

    def __init__(self, timeout_s: float = DEFAULT_PAUSE_TIMEOUT_S):
        self.timeout_s = timeout_s
        self.request: Optional[InterventionRequest] = None
        self.decision: Optional[OperatorDecision] = None
        self.waiting_since: Optional[float] = None
        self._answered = threading.Event()

    # -- called on the run's thread -----------------------------------------

    def handle(self, request: InterventionRequest) -> OperatorDecision:
        self.request = request
        self.waiting_since = time.time()
        answered = self._answered.wait(self.timeout_s)
        self.waiting_since = None
        if not answered or self.decision is None:
            # Nobody came. Abort rather than resume: resuming would continue a
            # run whose blocked step nobody performed, and the checkpoint
            # would then fail with a misleading reason.
            return OperatorDecision(
                Decision.ABORT,
                notes=f"no operator responded within {self.timeout_s:.0f}s",
                operator="dashboard (timed out)",
            )
        return self.decision

    # -- called on the HTTP thread ------------------------------------------

    @property
    def is_waiting(self) -> bool:
        return self.request is not None and not self._answered.is_set()

    def answer(self, decision: Decision, notes: str, operator: str) -> bool:
        """Record a person's decision and unblock the run. False if it was
        not waiting for one -- answering twice, or answering a run that
        already finished, must not look like it worked."""
        if not self.is_waiting:
            return False
        self.decision = OperatorDecision(decision, notes=notes, operator=operator)
        self._answered.set()
        return True


@dataclass
class RunRecord:
    """One run, and everything a surface needs to render it."""

    run_id: str
    capability_id: str
    version: str
    started_at: float
    attended: bool
    operator: Optional[PendingOperator] = None
    evidence_dir: Optional[Path] = None
    # Escalation evidence -- the stuck screenshot, the snapshot, the request
    # and the handoff record -- is written to its own tree by the engine, so
    # a dashboard looking only at the replay directory would offer an
    # operator an intervention with no screenshot to look at.
    escalation_dir: Optional[Path] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    thread: Optional[threading.Thread] = None
    _sink: Any = None

    @property
    def finished(self) -> bool:
        return self.result is not None or self.error is not None

    @property
    def awaiting_operator(self) -> bool:
        return bool(self.operator and self.operator.is_waiting)

    def status(self) -> str:
        if self.awaiting_operator:
            return "escalation_required"
        if self.error is not None:
            return "hard_failure"
        if self.result is not None:
            return self.result.get("classification", "unknown")
        return "running"

    def summary(self) -> dict[str, Any]:
        request = self.operator.request if self.operator else None
        return {
            "run_id": self.run_id,
            "capability": {"id": self.capability_id, "version": self.version},
            "status": self.status(),
            "attended": self.attended,
            "started_at": self.started_at,
            "duration_ms": (self.result or {}).get("duration_ms"),
            "inputs": (self.result or {}).get("inputs", {}),
            "awaiting_operator": self.awaiting_operator,
            "blocked_step": request.step_id if request else None,
        }


class RunManager:
    """Every run this process has served, and the threads still holding one."""

    def __init__(self, pause_timeout_s: float = DEFAULT_PAUSE_TIMEOUT_S):
        self.pause_timeout_s = pause_timeout_s
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.Lock()

    # -- starting -----------------------------------------------------------

    def start(self, engine, inputs: dict[str, Any], *, attended: bool,
              operator: Optional[PendingOperator] = None) -> RunRecord:
        """Begin a run on its own thread and return immediately.

        The engine is constructed by the caller so its run_id and sink exist
        before the thread starts -- the record has to be findable the instant
        this returns, or a dashboard polling for it races the run.
        """
        record = RunRecord(
            run_id=engine.run_id,
            capability_id=engine.artifact.capability.id,
            version=engine.artifact.capability.version,
            started_at=time.time(),
            attended=attended,
            operator=operator,
            evidence_dir=Path(engine.evidence.dir),
            escalation_dir=Path(engine.escalation_root) / engine.run_id,
            _sink=engine.sink,
        )

        def target() -> None:
            try:
                result = asyncio.run(engine.run(dict(inputs)))
                record.result = engine.sink.payload(result.as_dict())
            except Exception as exc:  # a crashed thread must not vanish
                record.error = f"{type(exc).__name__}: {exc}"

        record.thread = threading.Thread(
            target=target, name=f"replay-{engine.run_id}", daemon=True
        )
        with self._lock:
            self._runs[record.run_id] = record
        record.thread.start()
        return record

    def register(self, record: RunRecord) -> None:
        with self._lock:
            self._runs[record.run_id] = record

    # -- reading ------------------------------------------------------------

    def get(self, run_id: str) -> Optional[RunRecord]:
        return self._runs.get(run_id)

    def list(self) -> list[RunRecord]:
        return sorted(self._runs.values(), key=lambda r: r.started_at, reverse=True)

    def pending(self) -> list[RunRecord]:
        return [r for r in self.list() if r.awaiting_operator]

    # -- deciding -----------------------------------------------------------

    def decide(self, run_id: str, decision: Decision, notes: str, operator: str,
               settle_s: float = 20.0) -> tuple[bool, str]:
        """Hand a decision to a waiting run and wait for it to move on.

        Waiting briefly is what lets the dashboard show the outcome rather
        than an optimistic "resumed" that might be about to fail its
        checkpoint. It is bounded: a slow page must not hold the request.
        """
        record = self.get(run_id)
        if record is None:
            return False, f"no run {run_id!r} in this process"
        if not record.awaiting_operator:
            return False, (
                f"run {run_id!r} is not waiting for an operator "
                f"(status: {record.status()})"
            )
        if not record.operator.answer(decision, notes, operator):
            return False, f"run {run_id!r} was already answered"

        # Wait for the run to actually finish, not merely to stop waiting.
        # `awaiting_operator` flips the instant the decision is delivered, so
        # a loop that also breaks on it returned while the thread was still
        # tearing down -- and the dashboard rendered "running" for a run that
        # was seconds from a final status. A resume legitimately continues
        # into further steps, so this is bounded rather than indefinite.
        deadline = time.time() + settle_s
        while time.time() < deadline and not record.finished:
            time.sleep(0.1)
        return True, "decision delivered"
