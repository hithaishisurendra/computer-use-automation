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


def _narrowing_paths(base_paths: list[str], overlay_paths: list[str]) -> list[str]:
    """Every overlay path must be covered by some base path.

    "Covered" means literally present in the base list, or matching one of
    its globs -- so a base of `/member/*` may be narrowed to `/member/10001`
    but never widened to `/admin`. fnmatch is the right matcher here because
    allowed_paths are already glob-shaped in the schema.
    """
    widened = [
        p
        for p in overlay_paths
        if p not in base_paths and not any(fnmatch.fnmatch(p, b) for b in base_paths)
    ]
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


def load_resolved(
    root: str | Path,
    capability_id: str,
    version: str,
    tenant: Optional[str] = None,
) -> Artifact:
    """Load a capability and, if a tenant is named, resolve its overlay.

    A tenant with no overlay file is not an error: it means that tenant runs
    the base flow unmodified, which is the expected case for the tenant the
    capability was recorded against.
    """
    base_path, overlay_path = capability_paths(root, capability_id, version, tenant)
    artifact = load_artifact(base_path)
    if overlay_path is not None and overlay_path.exists():
        return apply_overlay(artifact, load_overlay(overlay_path))
    return artifact
