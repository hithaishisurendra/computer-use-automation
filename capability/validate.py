"""Pre-flight validation: caller inputs and credential availability.

Everything here runs BEFORE a browser opens. That ordering is the whole
point of the module:

- A malformed member reference is a **caller error**. The caller passed
  something the contract says is invalid; no automation ran, nothing was
  attempted, and reporting it as a replay failure would send someone
  debugging a UI flow that never started.
- A missing credential environment variable is an **auth failure**. Our own
  configuration is wrong. It is not the caller's problem and it is not
  retryable, so it is classified separately from both business outcomes and
  hard failures.

Redaction is driven by the `sensitivity` declared on each input, so error
messages can quote what was wrong with a `public` value while never echoing
a `pii` or `secret` one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from .schema import Artifact, InputSpec, Sensitivity

ViolationCode = str  # missing_required | unknown_input | type_mismatch | pattern_mismatch


def redact(value: Any, sensitivity: Sensitivity) -> str:
    """Render a value for logs and error messages according to its declared
    sensitivity. `identifier` keeps a short suffix so an operator can still
    correlate a run with a record without the full number appearing in logs.
    """
    if value is None:
        return "<none>"
    text = str(value)
    if sensitivity in ("pii", "secret"):
        return "<redacted>"
    if sensitivity == "identifier":
        if len(text) <= 2:
            return "*" * len(text)
        return "*" * (len(text) - 2) + text[-2:]
    return text


@dataclass(frozen=True)
class InputViolation:
    input_name: str
    code: ViolationCode
    message: str


class CallerInputError(Exception):
    """Raised when caller-supplied inputs do not satisfy the artifact's contract.

    Distinct from every replay failure type on purpose: this is a 4xx, not a
    5xx. `as_result()` renders the structured form an agent-facing caller
    would receive.
    """

    def __init__(self, capability_id: str, violations: list[InputViolation]):
        self.capability_id = capability_id
        self.violations = violations
        detail = "; ".join(f"{v.input_name}: {v.message}" for v in violations)
        super().__init__(f"invalid inputs for capability {capability_id!r}: {detail}")

    def as_result(self) -> dict[str, Any]:
        return {
            "classification": "caller_error",
            "capability": self.capability_id,
            "violations": [
                {"input": v.input_name, "code": v.code, "message": v.message}
                for v in self.violations
            ],
        }


class AuthConfigError(Exception):
    """Raised when the artifact's declared credentials cannot be resolved.

    Carries only environment variable *names* and whether each resolved --
    never a value, not even a partial one.
    """

    def __init__(self, capability_id: str, missing: list[str], checked: list[str]):
        self.capability_id = capability_id
        self.missing = missing
        self.checked = checked
        super().__init__(
            f"cannot authenticate for capability {capability_id!r}: unset environment "
            f"variable(s) {missing} (checked {checked}). No browser action was attempted."
        )

    def as_result(self) -> dict[str, Any]:
        return {
            "classification": "auth_failure",
            "capability": self.capability_id,
            "missing_env_vars": list(self.missing),
            "checked_env_vars": list(self.checked),
            "message": (
                "Authentication configuration is incomplete. This is a system "
                "configuration problem, not a caller error, and is not retryable."
            ),
        }


@dataclass
class ValidatedInputs:
    """Inputs that passed validation, plus a log-safe rendering of them."""

    values: dict[str, Any]
    redacted: dict[str, str] = field(default_factory=dict)


def _type_ok(value: Any, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "boolean":
        return isinstance(value, bool)
    if declared == "integer":
        # bool is a subclass of int in Python; an accidental True must not
        # satisfy an integer parameter.
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return False


def _validate_one(spec: InputSpec, value: Any) -> Optional[InputViolation]:
    shown = redact(value, spec.sensitivity)

    if not _type_ok(value, spec.type):
        return InputViolation(
            spec.name,
            "type_mismatch",
            f"expected type {spec.type!r}, got {type(value).__name__!r} ({shown})",
        )

    if spec.pattern is not None and not re.fullmatch(spec.pattern, value):
        return InputViolation(
            spec.name,
            "pattern_mismatch",
            f"value {shown} does not match required pattern {spec.pattern!r}",
        )

    return None


def validate_inputs(artifact: Artifact, params: dict[str, Any]) -> ValidatedInputs:
    """Validate caller-supplied params against the artifact's declared inputs.

    Collects every violation rather than failing on the first, so a caller
    fixing a bad request sees the whole list in one round trip.
    """
    violations: list[InputViolation] = []
    specs = artifact.input_map

    unknown = sorted(set(params) - set(specs))
    for name in unknown:
        violations.append(
            InputViolation(
                name,
                "unknown_input",
                f"capability {artifact.capability.id!r} declares no input named {name!r}",
            )
        )

    values: dict[str, Any] = {}
    for name, spec in specs.items():
        if name not in params or params[name] is None:
            if spec.required:
                violations.append(
                    InputViolation(name, "missing_required", "required input was not supplied")
                )
            continue

        violation = _validate_one(spec, params[name])
        if violation is not None:
            violations.append(violation)
        else:
            values[name] = params[name]

    if violations:
        raise CallerInputError(artifact.capability.id, violations)

    return ValidatedInputs(
        values=values,
        redacted={n: redact(v, specs[n].sensitivity) for n, v in values.items()},
    )


def describe_credentials(artifact: Artifact) -> dict[str, Any]:
    """Log-safe description of credential configuration: which environment
    variables the artifact refers to and whether each is currently set.
    Never returns a value. This is what goes into evidence."""
    auth = artifact.target.auth
    if auth is None:
        return {"auth_required": False}
    return {
        "auth_required": True,
        "mode": auth.mode,
        "env_vars": {
            role: {"name": var, "resolved": bool(os.environ.get(var))}
            for role, var in auth.credentials_ref.items()
        },
    }


def resolve_credentials(artifact: Artifact, env: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Resolve credential env var names to values, before any browser opens.

    Returns the values for the engine to use immediately; they are never
    persisted, logged, or attached to evidence -- `describe_credentials` is
    the version that is safe to record.
    """
    auth = artifact.target.auth
    if auth is None:
        return {}

    source = os.environ if env is None else env
    resolved: dict[str, str] = {}
    missing: list[str] = []
    checked: list[str] = []

    for role, var_name in auth.credentials_ref.items():
        checked.append(var_name)
        value = source.get(var_name)
        if not value:
            missing.append(var_name)
        else:
            resolved[role] = value

    if missing:
        raise AuthConfigError(artifact.capability.id, missing, checked)

    return resolved
