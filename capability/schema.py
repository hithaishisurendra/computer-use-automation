"""Capability artifact schema.

The artifact is the output of discovery and the input to replay: a
*capability contract* a calling agent can read to learn what a flow needs,
what it does, and what it returns, without reading any code. See
docs/artifact-schema-spec.md for the design rationale behind each block.

Two invariants this module enforces that are easy to lose otherwise:

1. Nothing here names Playwright, CSS, XPath or pixels. Targets are
   described as role + accessible name + containing scope -- the way a
   human operator would describe them -- which is the seam that lets a
   desktop resolver execute the same steps.
2. Sensitivity is declared on the data, not remembered at the call site.
   `pii`/`secret` values are never written to logs or evidence in full and
   `identifier` is masked, so redaction follows the model around instead of
   depending on someone calling a redact helper.
"""

from __future__ import annotations

import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

# {{param}} references inside step values and scope.contains.
TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

# Environment-variable naming convention. credentials_ref stores *names*
# only -- this pattern is what stops a literal password being pasted in and
# committed to a public repo.
ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

Sensitivity = Literal["public", "identifier", "pii", "secret"]
InputType = Literal["string", "integer", "number", "boolean"]
OutputType = Literal["string", "money", "integer", "number", "boolean", "date"]
CapabilityStatus = Literal["draft", "approved", "deprecated"]
Surface = Literal["web", "desktop"]
Risk = Literal["safe", "risky"]
Confidence = Literal["high", "medium", "low"]

# Only form_login is implemented. The others are declared so the schema
# doesn't have to change shape when an SSO or pre-authenticated-session
# target shows up -- the engine rejects unimplemented modes at runtime.
AuthMode = Literal["form_login", "sso", "basic", "preauthenticated_session"]

Strategy = Literal["role_name", "role_name_scoped", "cell_in_row", "role_ordinal"]

# Deliberately small. A small vocabulary constrains the LLM during
# discovery as much as it simplifies replay: an action that cannot be
# expressed cannot be recorded, which is itself a safety property.
# `wait_for` is declared but unused in 1.0.0 -- per-step checkpoints cover
# waiting today; it stays in the vocabulary so a flow that genuinely needs
# a bare wait doesn't force a schema bump.
Action = Literal["navigate", "click", "fill", "select", "check", "extract", "wait_for"]

ConditionType = Literal["element_present", "text_present", "any_of"]

# What a *flow* may declare about itself. Deliberately narrower than
# ResultClassification below: auth_failure and caller_error are properties
# of the system's own configuration or of the caller's arguments, not
# things a recorded flow can meaningfully assert about the app it drives.
OutcomeClassification = Literal["business_outcome", "recoverable", "hard_failure"]

# What the engine reports back to a caller. `auth_failure` means our own
# credentials or session are bad -- not the caller's problem and not
# retryable. `caller_error` means the supplied inputs were invalid and no
# browser was ever opened.
ResultClassification = Literal[
    "success",
    "business_outcome",
    "recoverable",
    "hard_failure",
    "auth_failure",
    "caller_error",
]


class StrictModel(BaseModel):
    """Base for every artifact block. extra="forbid" is load-bearing: an
    unrecognised key is far more likely to be a typo'd or hallucinated
    field from discovery than a deliberate extension, and silently
    dropping it would make the artifact lie about what replay will do."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# capability / target
# ---------------------------------------------------------------------------


class Capability(StrictModel):
    id: str
    version: str
    name: str
    description: str
    status: CapabilityStatus = "draft"
    derived_from: Optional[str] = Field(
        default=None,
        description=(
            "Set when this capability was forked from another because a tenant's "
            "divergence exceeded what an overlay can express (see loader.OverlayError). "
            "Format: 'capability_id@version'. Recording the ancestry is the point; "
            "no forking logic is built on it yet."
        ),
    )


class AuthSpec(StrictModel):
    """How the engine establishes a session before step 1 runs.

    Session establishment is deliberately *not* recorded as flow steps.
    Putting login in the step list would push a credential parameter into
    every capability's public contract, and the recorded flow should be the
    business flow. The engine runs this block before the first step in both
    discovery and replay, asserts `success_check`, and classifies any
    failure here as `auth_failure` rather than a business outcome.
    """

    mode: AuthMode
    path: str = Field(description="Path on the target where authentication is performed.")
    credentials_ref: dict[str, str] = Field(
        description=(
            "Maps a credential role (e.g. 'username', 'password') to the NAME of the "
            "environment variable holding it. Never the value -- artifacts are committed "
            "to a public repo. Resolved at runtime by capability.validate.resolve_credentials."
        )
    )
    success_check: "Condition" = Field(
        description="Asserted after authenticating; failing it is an auth_failure."
    )

    @field_validator("credentials_ref")
    @classmethod
    def _names_not_values(cls, v: dict[str, str]) -> dict[str, str]:
        for role, var_name in v.items():
            if not ENV_VAR_RE.match(var_name):
                raise ValueError(
                    f"credentials_ref[{role!r}] must be an ENVIRONMENT VARIABLE NAME "
                    f"(e.g. 'CORESERV_PASSWORD'), not a credential value. Got {var_name!r}. "
                    "Artifacts are committed to source control; secrets must never appear here."
                )
        return v


class Target(StrictModel):
    surface: Surface
    app: str
    app_version: str = Field(
        description=(
            "The app version this flow was recorded against. Never changes on its own -- "
            "it is a drift signal replay compares against what it observes live."
        )
    )
    tenant: str
    base_url: str
    entry_path: str = Field(
        description="Where the flow starts, understood as POST-authentication."
    )
    auth: Optional[AuthSpec] = None


# ---------------------------------------------------------------------------
# inputs / outputs
# ---------------------------------------------------------------------------


class InputSpec(StrictModel):
    name: str
    type: InputType = "string"
    required: bool = True
    pattern: Optional[str] = Field(
        default=None,
        description="Regex, validated BEFORE the browser opens. String inputs only.",
    )
    description: str
    sensitivity: Sensitivity = "public"
    example: Optional[Any] = None

    @model_validator(mode="after")
    def _pattern_only_on_strings(self) -> "InputSpec":
        if self.pattern is not None:
            if self.type != "string":
                raise ValueError(
                    f"input {self.name!r}: pattern is only meaningful on type 'string', "
                    f"got type {self.type!r}"
                )
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"input {self.name!r}: pattern is not a valid regex: {exc}") from exc
        return self


class OutputSpec(StrictModel):
    name: str
    type: OutputType = "string"
    required: bool = True
    description: str
    sensitivity: Sensitivity = "public"


# ---------------------------------------------------------------------------
# elements (the registry)
# ---------------------------------------------------------------------------


class Scope(StrictModel):
    """A container to search within -- a row, a form, a region.

    `contains` supports {{param}} substitution, which is what makes "the
    View link in the row for member 12345" expressible instead of "the
    third View link".

    Resolution semantics, learned the hard way from the a11y diagnostic:
    when several nested containers match, the resolver takes the INNERMOST
    one. CoreServ nests tables three deep, so an outer wrapper row's
    subtree text transitively contains every inner row's text; matching
    outermost-first would select the page shell instead of the data row.
    """

    role: str
    contains: Optional[str] = None
    name: Optional[str] = None


class LocatorRung(StrictModel):
    """One rung of an element's resolution chain, tried in order.

    `confidence` and `brittle` are not decoration: replay records which
    rung actually resolved, so falling through to a brittle rung is a drift
    signal worth surfacing even on a run that succeeded.
    """

    strategy: Strategy
    role: Optional[str] = None
    name: Optional[str] = None
    scope: Optional[Scope] = None
    column_header: Optional[str] = None
    column_index: Optional[int] = None
    index: Optional[int] = None
    confidence: Confidence
    brittle: bool = False
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _shape_matches_strategy(self) -> "LocatorRung":
        s = self.strategy
        if s == "role_name":
            if not self.role or not self.name:
                raise ValueError("strategy 'role_name' requires both 'role' and 'name'")
        elif s == "role_name_scoped":
            if not self.role or not self.name:
                raise ValueError("strategy 'role_name_scoped' requires both 'role' and 'name'")
            if self.scope is None:
                raise ValueError("strategy 'role_name_scoped' requires 'scope'")
        elif s == "cell_in_row":
            if self.scope is None:
                raise ValueError("strategy 'cell_in_row' requires 'scope'")
            has_header = self.column_header is not None
            has_index = self.column_index is not None
            if has_header == has_index:
                raise ValueError(
                    "strategy 'cell_in_row' requires exactly one of 'column_header' or "
                    "'column_index' (column_index exists for label/value tables that have "
                    "no column headers at all)"
                )
        elif s == "role_ordinal":
            if not self.role or self.index is None:
                raise ValueError("strategy 'role_ordinal' requires 'role' and 'index'")
            if not self.brittle:
                raise ValueError(
                    "strategy 'role_ordinal' must be marked brittle: true -- positional "
                    "targeting is a last resort and replay needs to be able to flag it"
                )
        return self


class Element(StrictModel):
    description: str
    frame: str = Field(description="Frame the control lives in, by frame name.")
    chain: list[LocatorRung] = Field(min_length=1)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# conditions / checkpoints
# ---------------------------------------------------------------------------


class Condition(StrictModel):
    type: ConditionType
    element: Optional[str] = None
    text: Optional[str] = None
    conditions: Optional[list["Condition"]] = None

    @model_validator(mode="after")
    def _shape_matches_type(self) -> "Condition":
        if self.type == "element_present":
            if not self.element:
                raise ValueError("condition 'element_present' requires 'element'")
        elif self.type == "text_present":
            if not self.text:
                raise ValueError("condition 'text_present' requires 'text'")
        elif self.type == "any_of":
            if not self.conditions:
                raise ValueError("condition 'any_of' requires a non-empty 'conditions' list")
        return self


class Checkpoint(Condition):
    """A condition asserted after a step, with a deadline.

    Checkpoints are per-step rather than terminal-only on purpose: a click
    that silently does nothing is the most common failure in UI automation,
    and a terminal-only check tells you the flow failed but not where.
    """

    timeout_ms: int = Field(default=5000, gt=0)


# ---------------------------------------------------------------------------
# steps / outcomes / policy / provenance
# ---------------------------------------------------------------------------


class Step(StrictModel):
    id: str
    action: Action
    path: Optional[str] = None
    element: Optional[str] = None
    value: Optional[str] = None
    into: Optional[str] = None
    risk: Risk = "safe"
    checkpoint: Optional[Checkpoint] = None
    outcomes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _shape_matches_action(self) -> "Step":
        a = self.action
        requires_element = {"click", "fill", "select", "check", "extract", "wait_for"}
        if a == "navigate":
            if not self.path:
                raise ValueError(f"step {self.id}: action 'navigate' requires 'path'")
        if a in requires_element and not self.element:
            raise ValueError(f"step {self.id}: action {a!r} requires 'element'")
        if a in {"fill", "select"} and self.value is None:
            raise ValueError(f"step {self.id}: action {a!r} requires 'value'")
        if a == "extract" and not self.into:
            raise ValueError(f"step {self.id}: action 'extract' requires 'into'")
        if a != "extract" and self.into:
            raise ValueError(f"step {self.id}: 'into' is only valid on action 'extract'")
        return self


class Outcome(StrictModel):
    name: str
    classification: OutcomeClassification
    detect: Condition
    terminal: bool = True
    message: str


class Policy(StrictModel):
    """Enforced at the executor boundary immediately before an action runs,
    not in the prompt. A prompt-level guardrail is a suggestion; an
    executor-level one is a control. The same policy layer sits under both
    discovery and replay."""

    allowed_origins: list[str] = Field(min_length=1)
    allowed_paths: list[str] = Field(min_length=1)
    allowed_actions: list[Action] = Field(min_length=1)
    risky_action_handling: Literal["block", "require_confirmation", "flag"] = "require_confirmation"
    max_steps: int = Field(default=25, gt=0)
    timeout_ms: int = Field(default=120000, gt=0)


class Provenance(StrictModel):
    """The honest record of how the artifact came to exist.

    `steps_attempted` vs `steps_recorded` is the point: the model wandered,
    and the artifact keeps the path that worked rather than the raw
    transcript. The goal string is retained; raw model reasoning is not --
    transcripts belong in evidence, not in the capability contract.
    """

    source: Literal["discovery", "hand_written"] = "discovery"
    discovered_at: str
    goal: str
    model: Optional[str] = None
    discovery_run_id: Optional[str] = None
    steps_attempted: int = Field(ge=0)
    steps_recorded: int = Field(ge=0)
    human_interventions: int = Field(default=0, ge=0)
    notes: Optional[str] = None


# ---------------------------------------------------------------------------
# the artifact
# ---------------------------------------------------------------------------


class Artifact(StrictModel):
    schema_version: str
    capability: Capability
    target: Target
    inputs: list[InputSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    elements: dict[str, Element]
    steps: list[Step] = Field(min_length=1)
    outcomes: list[Outcome] = Field(default_factory=list)
    policy: Policy
    provenance: Provenance

    # -- convenience lookups -------------------------------------------------

    @property
    def input_map(self) -> dict[str, InputSpec]:
        return {i.name: i for i in self.inputs}

    @property
    def output_map(self) -> dict[str, OutputSpec]:
        return {o.name: o for o in self.outputs}

    @property
    def outcome_map(self) -> dict[str, Outcome]:
        return {o.name: o for o in self.outcomes}

    # -- cross-block integrity ----------------------------------------------

    @field_validator("schema_version")
    @classmethod
    def _known_schema_version(cls, v: str) -> str:
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {v!r}; this engine parses {SCHEMA_VERSION!r}"
            )
        return v

    @model_validator(mode="after")
    def _references_resolve(self) -> "Artifact":
        """Everything an artifact points at must exist inside the artifact.

        These are the errors worth catching at load time rather than three
        steps into a live browser session: a step naming an element that was
        never declared, an extract writing to an undeclared output, a
        {{param}} with no matching input, or a step using an action the
        policy forbids.
        """
        errors: list[str] = []

        element_keys = set(self.elements)
        input_names = {i.name for i in self.inputs}
        output_names = {o.name for o in self.outputs}
        outcome_names = {o.name for o in self.outcomes}
        step_ids = [s.id for s in self.steps]

        if len(set(step_ids)) != len(step_ids):
            errors.append("step ids must be unique")

        def check_condition(cond: Condition, where: str) -> None:
            if cond.element and cond.element not in element_keys:
                errors.append(f"{where}: references unknown element {cond.element!r}")
            for sub in cond.conditions or []:
                check_condition(sub, where)

        def check_template(text: Optional[str], where: str) -> None:
            for param in TEMPLATE_RE.findall(text or ""):
                if param not in input_names:
                    errors.append(
                        f"{where}: template {{{{{param}}}}} does not match any declared input"
                    )

        for key, element in self.elements.items():
            for rung in element.chain:
                if rung.scope is not None:
                    check_template(rung.scope.contains, f"element {key!r} scope.contains")

        allowed_actions = set(self.policy.allowed_actions)
        for step in self.steps:
            where = f"step {step.id!r}"
            if step.action not in allowed_actions:
                errors.append(
                    f"{where}: action {step.action!r} is not in policy.allowed_actions "
                    f"{sorted(allowed_actions)}"
                )
            if step.element and step.element not in element_keys:
                errors.append(f"{where}: references unknown element {step.element!r}")
            if step.into and step.into not in output_names:
                errors.append(f"{where}: extracts into undeclared output {step.into!r}")
            check_template(step.value, f"{where} value")
            if step.checkpoint is not None:
                check_condition(step.checkpoint, f"{where} checkpoint")
            for name in step.outcomes:
                if name not in outcome_names:
                    errors.append(f"{where}: references undeclared outcome {name!r}")

        for outcome in self.outcomes:
            check_condition(outcome.detect, f"outcome {outcome.name!r} detect")

        if self.target.auth is not None:
            check_condition(self.target.auth.success_check, "target.auth.success_check")

        # A run that reaches the end but extracts nothing is a failure, not
        # a success -- so a required output with no step producing it is a
        # promise the artifact cannot keep.
        produced = {s.into for s in self.steps if s.into}
        for output in self.outputs:
            if output.required and output.name not in produced:
                errors.append(
                    f"output {output.name!r} is required but no extract step writes into it"
                )

        if errors:
            raise ValueError("; ".join(errors))
        return self


# ---------------------------------------------------------------------------
# tenant overlay
# ---------------------------------------------------------------------------


class InputOverride(StrictModel):
    """What a tenant may say about an input.

    Name, type and required are absent on purpose. A tenant that needs a
    *different* parameter is not running the same capability, and pretending
    otherwise via an overlay would produce an artifact whose contract
    silently differs from the one a calling agent read.
    """

    pattern: Optional[str] = None
    description: Optional[str] = None
    example: Optional[Any] = None


class ElementOverride(StrictModel):
    description: Optional[str] = None
    frame: Optional[str] = None
    chain: Optional[list[LocatorRung]] = None
    notes: Optional[str] = None


class TargetOverride(StrictModel):
    """Environment config only. base_url is where this tenant's instance
    lives; app_version is which build of the same vendor product they run,
    which is the drift signal replay compares against."""

    base_url: Optional[str] = None
    app_version: Optional[str] = None


class PolicyOverride(StrictModel):
    """Tenants may only ever NARROW the allowlist. Enforced at merge time in
    loader.apply_overlay -- an overlay that could widen it would make the
    base artifact's guardrail unreviewable, since you'd have to read every
    tenant file to know what the capability is actually permitted to do."""

    allowed_paths: Optional[list[str]] = None
    allowed_actions: Optional[list[Action]] = None


class TenantOverlay(StrictModel):
    extends: str = Field(description="'capability_id@version' this overlay specialises.")
    tenant: str
    input_overrides: dict[str, InputOverride] = Field(default_factory=dict)
    element_overrides: dict[str, ElementOverride] = Field(default_factory=dict)
    target_overrides: Optional[TargetOverride] = None
    policy_overrides: Optional[PolicyOverride] = None
    notes: Optional[str] = None

    @property
    def extends_id(self) -> str:
        return self.extends.split("@", 1)[0]

    @property
    def extends_version(self) -> Optional[str]:
        parts = self.extends.split("@", 1)
        return parts[1] if len(parts) == 2 else None


Condition.model_rebuild()
AuthSpec.model_rebuild()
