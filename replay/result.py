"""The single typed result every replay returns.

One shape for every ending, discriminated by `classification`. A caller
should never have to distinguish "the call raised" from "the call returned
a failure" -- both are results, and conflating a business outcome with a
crash is the most common design mistake this contract exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

Classification = str  # success | business_outcome | caller_error | auth_failure | hard_failure


@dataclass
class StepTrace:
    """What happened on one step, including which locator rung fired."""

    step_id: str
    action: str
    status: str = "pending"  # pending | ok | recovered | failed | skipped
    duration_ms: float = 0.0
    resolution: Optional[dict[str, Any]] = None
    checkpoint: Optional[dict[str, Any]] = None
    detections: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 1
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "step_id": self.step_id,
            "action": self.action,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "attempts": self.attempts,
        }
        if self.resolution:
            d["resolution"] = self.resolution
        if self.checkpoint:
            d["checkpoint"] = self.checkpoint
        if self.detections:
            d["detections"] = self.detections
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class ReplayResult:
    classification: Classification
    capability_id: str
    capability_version: str
    tenant: str
    run_id: str
    duration_ms: float = 0.0

    outputs: dict[str, Any] = field(default_factory=dict)
    outcome: Optional[str] = None
    message: Optional[str] = None

    failed_step: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None

    trace: list[StepTrace] = field(default_factory=list)
    recoverable_conditions: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    violations: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, str] = field(default_factory=dict)
    escalation_eligible: bool = False
    inputs_redacted: dict[str, str] = field(default_factory=dict)
    human_interventions: list[dict[str, Any]] = field(default_factory=list)
    control: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "classification": self.classification,
            "capability": {"id": self.capability_id, "version": self.capability_version},
            "tenant": self.tenant,
            "run_id": self.run_id,
            "duration_ms": round(self.duration_ms, 2),
            "inputs": self.inputs_redacted,
        }
        if self.classification == "success":
            d["outputs"] = self.outputs
        if self.outcome:
            d["outcome"] = self.outcome
        if self.message:
            d["message"] = self.message
        if self.failed_step:
            d["failure"] = {
                "step_id": self.failed_step,
                "expected": self.expected,
                "observed": self.observed,
            }
        if self.violations:
            d["violations"] = self.violations
        if self.recoverable_conditions:
            d["recoverable_conditions"] = self.recoverable_conditions
        if self.warnings:
            d["warnings"] = self.warnings
        if self.escalation_eligible:
            d["escalation_eligible"] = True
        if self.human_interventions:
            d["human_interventions"] = self.human_interventions
        if self.control:
            d["control"] = self.control
        if self.evidence:
            d["evidence"] = self.evidence
        d["trace"] = [t.as_dict() for t in self.trace]
        return d
