"""Load a capability artifact from disk and resolve a tenant overlay onto it.

Storage is one JSON file per capability version at
`capabilities/{id}/{version}.json`, with tenant overlays beside it at
`capabilities/{id}/tenants/{tenant}.json`. Files, not a database: a
single-tenant demo does not need one, and the brief explicitly does not
reward premature infrastructure.

The overlay model is the answer to "hundreds of tenants run the same vendor
product, configured differently". An overlay may specialise *how a control
is found* and *what a parameter looks like*; it may not change what the
capability does. That line is enforced here rather than by convention,
because an overlay that could rewrite steps or outputs would mean a calling
agent could not trust the base artifact's contract without reading every
tenant file.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any, Optional

from pydantic import ValidationError

from .schema import Artifact, TenantOverlay

# Top-level overlay keys that are structurally impossible to allow. Each maps
# to why -- the message is user-facing, so it explains the fix rather than
# just naming the rule.
_FORBIDDEN_OVERLAY_KEYS = {
    "steps": "the recorded flow differs, which means this is a different capability",
    "outputs": "the returned contract differs, which callers depend on being stable",
    "inputs": "use 'input_overrides' to adjust pattern/description/example",
    "elements": "use 'element_overrides' to specialise how a control is found",
    "outcomes": "the flow's declared business outcomes differ",
    "capability": "identity and version belong to the base capability",
    "provenance": "provenance records how the base was discovered",
    "target": "use 'target_overrides' (base_url and app_version only)",
    "policy": "use 'policy_overrides' (allowed_paths and allowed_actions, narrowing only)",
    "schema_version": "the format version belongs to the base capability",
}

_FORK_HINT = (
    "This divergence exceeds what a tenant overlay can express. Fork the capability "
    "into its own artifact and set capability.derived_from to record the ancestry."
)

# Keys forbidden *inside* an overlay section. Pydantic would reject these
# anyway via extra="forbid", but with a generic "extra inputs are not
# permitted" that doesn't tell the author what to do instead -- these
# checks exist so the fork hint reaches the person who needs it.
_FORBIDDEN_SECTION_KEYS = {
    "input_overrides": {
        "name": "renaming a parameter changes the capability's public contract",
        "type": "changing a parameter's type changes the capability's public contract",
        "required": "changing whether a parameter is required changes the capability's public contract",
        "sensitivity": "sensitivity drives redaction and must not vary per tenant",
    },
    "target_overrides": {
        "surface": "a different surface needs a different resolver, not an overlay",
        "app": "a different app is a different capability",
        "tenant": "the tenant is declared once, at the top level of the overlay",
        "entry_path": "where the flow starts is flow logic, not environment config",
        "auth": "authentication configuration is resolved from the environment, not overridden per tenant",
    },
    "policy_overrides": {
        "allowed_origins": "origins follow base_url; override that instead",
        "risky_action_handling": "how risky actions are handled is a system-wide policy decision",
        "max_steps": "step budget belongs to the recorded flow",
        "timeout_ms": "the overall timeout belongs to the recorded flow",
    },
}


class ArtifactError(Exception):
    """An artifact (or overlay) file is missing, malformed, or invalid."""


class OverlayError(ArtifactError):
    """An overlay tried to express something overlays are not allowed to express."""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ArtifactError(f"no such file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path}: not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ArtifactError(f"{path}: expected a JSON object at the top level")
    return data


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    """Pydantic's default rendering buries the field path. Artifacts are
    hand-edited and machine-generated alike, so the error needs to say
    exactly which field is wrong."""
    lines = [f"{path}: artifact failed validation ({exc.error_count()} error(s)):"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_artifact(path: str | Path) -> Artifact:
    """Load and fully validate a base artifact."""
    path = Path(path)
    data = _read_json(path)
    try:
        return Artifact.model_validate(data)
    except ValidationError as exc:
        raise ArtifactError(_format_validation_error(path, exc)) from exc


def load_overlay(path: str | Path) -> TenantOverlay:
    """Load a tenant overlay, rejecting forbidden keys with an explanation.

    The forbidden-key check runs before pydantic so the error names the
    concept ("steps differ -> fork the capability") instead of surfacing a
    generic 'extra inputs are not permitted'.
    """
    path = Path(path)
    data = _read_json(path)

    for key, why in _FORBIDDEN_OVERLAY_KEYS.items():
        if key in data:
            raise OverlayError(
                f"{path}: overlay may not override {key!r} -- {why}. {_FORK_HINT}"
            )

    for section, forbidden in _FORBIDDEN_SECTION_KEYS.items():
        block = data.get(section)
        if not isinstance(block, dict):
            continue
        # input_overrides/element_overrides are keyed by parameter/element
        # name; target_overrides and policy_overrides are flat.
        entries = block.values() if section == "input_overrides" else [block]
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for key, why in forbidden.items():
                if key in entry:
                    raise OverlayError(
                        f"{path}: overlay may not override {section}.{key} -- {why}. {_FORK_HINT}"
                    )

    try:
        return TenantOverlay.model_validate(data)
    except ValidationError as exc:
        raise OverlayError(_format_validation_error(path, exc)) from exc


def widening_paths(base_paths: list[str], requested_paths: list[str]) -> list[str]:
    """Requested paths NOT covered by the base allowlist.

    "Covered" means literally present in the base list, or matching one of
    its globs -- so a base of `/member/*` may be narrowed to `/member/10001`
    but never widened to `/admin`. fnmatch is the right matcher here because
    allowed_paths are already glob-shaped in the schema.

    Public because the narrowing rule has more than one caller: tenant
    overlays enforce it here, and discovery enforces it on its own CLI
    flags. One predicate, so the two cannot drift into disagreeing about
    what "narrower" means.
    """
    return [
        p
        for p in requested_paths
        if p not in base_paths and not any(fnmatch.fnmatch(p, b) for b in base_paths)
    ]


def _narrowing_paths(base_paths: list[str], overlay_paths: list[str]) -> list[str]:
    widened = widening_paths(base_paths, overlay_paths)
    if widened:
        raise OverlayError(
            "overlay policy_overrides.allowed_paths may only narrow the base allowlist; "
            f"these paths are not covered by the base {base_paths}: {widened}. {_FORK_HINT}"
        )
    return list(overlay_paths)


def _narrowing_actions(base_actions: list[str], overlay_actions: list[str]) -> list[str]:
    widened = sorted(set(overlay_actions) - set(base_actions))
    if widened:
        raise OverlayError(
            "overlay policy_overrides.allowed_actions may only narrow the base allowlist; "
            f"these actions are not in the base {sorted(base_actions)}: {widened}. {_FORK_HINT}"
        )
    return list(overlay_actions)


def apply_overlay(base: Artifact, overlay: TenantOverlay) -> Artifact:
    """Merge an overlay onto a base artifact and re-validate the result.

    Re-validating rather than trusting the merge is deliberate: an overlay
    can rename the accessible name a chain looks for, and the merged
    artifact still has to satisfy every cross-block rule (templates resolve,
    elements exist, policy covers the actions the steps use).
    """
    if overlay.extends_id != base.capability.id:
        raise OverlayError(
            f"overlay extends {overlay.extends_id!r} but the base capability is "
            f"{base.capability.id!r}"
        )
    if overlay.extends_version and overlay.extends_version != base.capability.version:
        raise OverlayError(
            f"overlay extends version {overlay.extends_version!r} but the base capability "
            f"is version {base.capability.version!r}"
        )

    data = base.model_dump(mode="json", exclude_none=True)
    # allowed_origins is derived from base_url, and an overlay may move
    # base_url. Carrying the base's value forward would make the merged
    # artifact declare an origin that disagrees with where it now points,
    # which the schema (correctly) rejects. Dropping it lets it be re-derived.
    data["policy"].pop("allowed_origins", None)

    unknown_inputs = set(overlay.input_overrides) - {i["name"] for i in data["inputs"]}
    if unknown_inputs:
        raise OverlayError(
            f"overlay overrides undeclared input(s) {sorted(unknown_inputs)}; an overlay may "
            f"specialise an existing parameter but never introduce one. {_FORK_HINT}"
        )
    for spec in data["inputs"]:
        override = overlay.input_overrides.get(spec["name"])
        if override is not None:
            spec.update(override.model_dump(exclude_none=True))

    unknown_elements = set(overlay.element_overrides) - set(data["elements"])
    if unknown_elements:
        raise OverlayError(
            f"overlay overrides undeclared element(s) {sorted(unknown_elements)}; an overlay "
            f"may specialise an existing element but never introduce one. {_FORK_HINT}"
        )
    for key, element_override in overlay.element_overrides.items():
        data["elements"][key].update(element_override.model_dump(mode="json", exclude_none=True))

    if overlay.target_overrides is not None:
        data["target"].update(overlay.target_overrides.model_dump(exclude_none=True))

    if overlay.policy_overrides is not None:
        po = overlay.policy_overrides
        if po.allowed_paths is not None:
            data["policy"]["allowed_paths"] = _narrowing_paths(
                data["policy"]["allowed_paths"], po.allowed_paths
            )
        if po.allowed_actions is not None:
            data["policy"]["allowed_actions"] = _narrowing_actions(
                data["policy"]["allowed_actions"], po.allowed_actions
            )

    data["target"]["tenant"] = overlay.tenant

    try:
        return Artifact.model_validate(data)
    except ValidationError as exc:
        raise OverlayError(
            f"applying overlay for tenant {overlay.tenant!r} produced an invalid artifact:\n"
            + _format_validation_error(Path(f"<overlay:{overlay.tenant}>"), exc)
        ) from exc


def capability_paths(
    root: str | Path, capability_id: str, version: str, tenant: Optional[str] = None
) -> tuple[Path, Optional[Path]]:
    root = Path(root)
    base_path = root / capability_id / f"{version}.json"
    overlay_path = root / capability_id / "tenants" / f"{tenant}.json" if tenant else None
    return base_path, overlay_path


class RiskDisagreement(ArtifactError):
    """The artifact's recorded risk labels disagree with the app profile.

    Not a warning and not silently corrected in either direction. Overriding
    toward `risky` would let a stale profile break a reviewed capability;
    overriding toward `safe` would let a tampered artifact post unattended.
    Either way the override hides the disagreement, and the disagreement is
    the information: something is wrong with the artifact, the profile, or
    both, and a person needs to look.
    """


def derive_risk(artifact: Artifact, profile) -> dict[str, str]:
    """What the app profile says each step's risk SHOULD be.

    Derived from the artifact alone -- no browser, no page. An element's
    chain carries the control's role and accessible name, which is exactly
    what the recorder classified on, so the verb signal reproduces
    statically. `commit_paths` cannot: the landing URL is only observable
    after the click, so this covers one of the two signals and says so.

    Only the highest-confidence rung is consulted. A fallback rung exists
    because the first might not resolve; it describes the same control, and
    reading them all would let a brittle positional rung with no name mask a
    named one.
    """
    from discovery.recorder import risk_rules_from_profile

    rules = risk_rules_from_profile(profile)
    derived: dict[str, str] = {}
    for step in artifact.steps:
        if step.action != "click" or not step.element:
            derived[step.id] = "safe"
            continue
        element = artifact.elements.get(step.element)
        rung = element.chain[0] if element and element.chain else None
        role = (rung.role or "").strip().lower() if rung else ""
        name = (rung.name or "").strip() if rung else ""
        matched = rules.match(name)
        # The recorded destination lets the commit-path signal reproduce here
        # too, so this is no longer verb-only. An artifact recorded before
        # destinations were captured simply has none, and falls back to the
        # verb signal alone -- which is why the check refuses downgrades
        # rather than asserting equality.
        commits = rules.commits(element.destination) if element else None
        # Same narrowing both signals get everywhere else: only a submit-type
        # control can commit, because navigation links share both the commit
        # vocabulary AND, on an app that serves a form from the path it posts
        # to, the committing path.
        derived[step.id] = (
            "risky" if (matched or commits) and role == "button" else "safe"
        )
    return derived


def check_risk_agreement(artifact: Artifact, profile) -> None:
    """Refuse an artifact whose risk labels the profile contradicts.

    Replay used to read `step.risk` and trust it. A hand-edited artifact
    flipping `risky` to `safe` would post unattended, and replay had the
    profile in hand the whole time -- the recorder's judgement is a
    recording-time fact that execution treated as gospel.

    Checked at load rather than mid-run, because the element chain carries
    everything needed and a browser is not. Every surface that loads an
    artifact -- replay, the API catalogue, the dashboard -- gets the refusal
    for free.
    """
    derived = derive_risk(artifact, profile)
    disagreements = [
        f"step {step.id!r} ({step.element or step.action}): artifact says "
        f"{step.risk!r}, app profile {profile.name!r} derives {derived[step.id]!r}"
        for step in artifact.steps
        # One direction only. A step the artifact calls risky that the
        # profile does not is NOT a disagreement worth refusing: a human
        # reviewer may mark a step risky for reasons no vocabulary
        # encodes, and refusing that would punish exactly the review the
        # draft->approved model asks for. The dangerous direction is the
        # other one.
        if derived[step.id] == "risky" and step.risk != "risky"
    ]
    if disagreements:
        raise RiskDisagreement(
            f"capability {artifact.capability.id!r} has step(s) the app profile "
            "considers irreversible but the artifact does not:\n  "
            + "\n  ".join(disagreements)
            + "\n\nThis is refused rather than corrected. Either the artifact was "
            "edited after recording, or the profile changed since -- and which one "
            "it is decides whether to re-record or to review the profile. "
            "Correcting it silently would hide that."
        )


def apply_profile_defaults(artifact: Artifact, profile=None) -> Artifact:
    """Fill in auth targeting the artifact did not declare, from the app profile.

    Login used to be targeted by CSS selectors inside the engine, so no
    artifact declared login elements -- and those artifacts must keep
    replaying unchanged now that targeting goes through the element registry.
    Rather than rewrite them, the app profile supplies the element
    definitions and they are injected here under reserved keys.

    An artifact that declares its own auth elements is left alone: this is a
    compatibility path for artifacts recorded before the seam existed, not a
    mechanism new artifacts should rely on.
    """
    from .profile import profile_for

    auth = artifact.target.auth
    if auth is None or auth.elements:
        return artifact

    profile = profile or profile_for(artifact.target)
    defaults = profile.auth_defaults
    if defaults is None or not defaults.fields:
        return artifact

    data = artifact.model_dump(mode="json", exclude_none=True)
    data["policy"].pop("allowed_origins", None)
    for key, element in defaults.elements.items():
        data["elements"].setdefault(key, element)
    data["target"]["auth"]["elements"] = {
        # Only the fields this artifact actually uses. A profile may describe
        # a branch select that a capability recorded before it existed never
        # supplied, and declaring an element for a field nothing fills would
        # fail validation for a value that is simply absent.
        name: key
        for name, key in defaults.fields.items()
        if name in auth.credentials_ref or name in auth.parameters
    }
    if defaults.submit:
        data["target"]["auth"]["submit"] = defaults.submit

    try:
        return Artifact.model_validate(data)
    except ValidationError as exc:
        raise ArtifactError(
            f"app profile {profile.name!r} supplied auth defaults that do not fit "
            f"capability {artifact.capability.id!r}:\n"
            + _format_validation_error(Path(f"<profile:{profile.name}>"), exc)
        ) from exc


def load_resolved(
    root: str | Path,
    capability_id: str,
    version: str,
    tenant: Optional[str] = None,
    profile=None,
) -> Artifact:
    """Load a capability and, if a tenant is named, resolve its overlay.

    A tenant with no overlay file is not an error: it means that tenant runs
    the base flow unmodified, which is the expected case for the tenant the
    capability was recorded against.
    """
    from .profile import profile_for

    base_path, overlay_path = capability_paths(root, capability_id, version, tenant)
    artifact = load_artifact(base_path)
    if overlay_path is not None and overlay_path.exists():
        artifact = apply_overlay(artifact, load_overlay(overlay_path))
    resolved = apply_profile_defaults(artifact, profile)
    check_risk_agreement(resolved, profile or profile_for(resolved.target))
    return resolved
