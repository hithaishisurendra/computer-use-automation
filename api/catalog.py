"""The capability catalogue: what an agent can call, and what it needs.

Reads the artifacts on disk and renders each one as a *contract* -- name,
typed inputs, typed outputs, declared business outcomes, and whether it
contains an irreversible step. Nothing here mentions a browser, a locator or
a page, because a calling agent has no business knowing the capability is
driven through a UI at all; that is the whole point of the artifact.

Kept separate from the endpoints so the catalogue can be listed, validated
and tested without an HTTP server, and so a second front door (a chatbot's
tool list, say) reads the same description rather than a parallel one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from capability.loader import ArtifactError, check_risk_agreement, load_artifact, load_resolved
from capability.profile import ProfileError, profile_for
from capability.schema import Artifact

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "capabilities"


def _describe_input(spec) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": spec.name,
        "type": spec.type,
        "required": spec.required,
        "description": spec.description,
        "sensitivity": spec.sensitivity,
    }
    if spec.pattern:
        d["pattern"] = spec.pattern
    if spec.example is not None:
        d["example"] = spec.example
    return d


def describe(artifact: Artifact) -> dict[str, Any]:
    """The agent-facing contract for one capability.

    `risky_steps` and `requires_human` are surfaced deliberately. An agent
    deciding whether to call something needs to know in advance that it
    cannot complete unattended -- discovering that from a failed invocation
    is worse for the caller and worse for the audit trail.
    """
    risky = [s.id for s in artifact.steps if s.risk == "risky"]
    return {
        "id": artifact.capability.id,
        "version": artifact.capability.version,
        "name": artifact.capability.name,
        "description": artifact.capability.description,
        "status": artifact.capability.status,
        "app": artifact.target.app,
        "tenant": artifact.target.tenant,
        "inputs": [_describe_input(i) for i in artifact.inputs],
        "outputs": [
            {"name": o.name, "type": o.type, "required": o.required,
             "description": o.description, "sensitivity": o.sensitivity}
            for o in artifact.outputs
        ],
        "outcomes": [
            {"name": o.name, "classification": o.classification, "message": o.message}
            for o in artifact.outcomes
        ],
        "risky_steps": risky,
        "requires_human": bool(risky)
        and artifact.policy.risky_action_handling != "flag",
        "flow_completed": artifact.provenance.flow_completed,
        "invocable": artifact.provenance.flow_completed,
    }


def iter_versions(root: str | Path = DEFAULT_ROOT):
    """Every (capability_id, version, path) on disk."""
    root = Path(root)
    if not root.exists():
        return
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        for path in sorted(directory.glob("*.json")):
            yield directory.name, path.stem, path


def catalog(root: str | Path = DEFAULT_ROOT) -> list[dict[str, Any]]:
    """Every loadable capability, plus an entry for any that will not load.

    A broken artifact appears in the listing with its error rather than
    silently vanishing: a capability an agent expected to find and cannot is
    a fact worth reporting, and a catalogue that quietly shrinks is how you
    discover the problem in production instead.
    """
    entries: list[dict[str, Any]] = []
    for capability_id, version, path in iter_versions(root):
        try:
            artifact = load_artifact(path)
            # The same agreement check `load_resolved` runs. Listing has to
            # apply it too: a catalogue that advertises a capability the
            # invoke path refuses is worse than one that says why -- an agent
            # would read `invocable: true` and call something that cannot run.
            # This is the audit's own pattern, one entry point checked and its
            # sibling not.
            check_risk_agreement(artifact, profile_for(artifact.target))
            entries.append(describe(artifact))
        except (ArtifactError, ProfileError) as exc:
            entries.append({
                "id": capability_id, "version": version, "status": "unloadable",
                "invocable": False, "error": str(exc),
            })
    return entries


def load(capability_id: str, version: str, tenant: Optional[str] = None,
         root: str | Path = DEFAULT_ROOT) -> Artifact:
    return load_resolved(root, capability_id, version, tenant=tenant)
