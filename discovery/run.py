"""CLI for an LLM-driven discovery run.

    python -m discovery.run \
        --goal "Look up member 10001 and read their current savings balance" \
        --target http://localhost:8800 --entry /search

Runs the loop against a live surface, records the successful path as a
capability artifact, and validates that artifact by loading it back through
`capability.loader` before writing it out. Validation is not decoration: an
artifact that does not load is not a capability, and discovery reporting
success while emitting one would be the failure mode most worth catching.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from capability.loader import ArtifactError, load_artifact, widening_paths
from capability.redaction import Scrubber
from capability.schema import AuthSpec, Condition, Policy, Target
from discovery.loop import DEFAULT_PROVIDER, DiscoveryLoop
from discovery.model import DEFAULT_MODELS, PROVIDERS, load_dotenv
from discovery.recorder import record

# Declared, reviewable, version-controlled. Not assembled from CLI flags:
# an allowlist a caller can extend at invocation time is not an allowlist,
# and it would contradict the rule the capability layer already enforces on
# tenant overlays -- allowlists may only narrow.
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / "policy.json"


class PolicyWidened(Exception):
    """A caller tried to grant discovery more reach than its declared policy."""


def load_policy(path: str | Path, base_url: str, narrow_to: list[str] | None) -> Policy:
    """Load the declared discovery policy, optionally narrowed.

    `narrow_to` (from --allow-path) can only ever shrink the declared set.
    Each requested path must be covered by a declared one -- literally or by
    glob -- checked with `capability.loader.widening_paths`, the same
    predicate tenant overlays are held to. Sharing the predicate is the
    point: two implementations of "narrower" would eventually disagree.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    data.pop("_comment", None)
    declared = Policy(**{**data, "allowed_origins": [base_url.rstrip("/")]})

    if not narrow_to:
        return declared

    widened = widening_paths(declared.allowed_paths, narrow_to)
    if widened:
        raise PolicyWidened(
            f"--allow-path may only narrow the declared discovery policy. "
            f"These are not covered by {declared.allowed_paths}: {widened}. "
            f"To grant genuinely new reach, edit {path} so the change is reviewable."
        )
    return declared.model_copy(update={"allowed_paths": list(narrow_to)})


def build_target(base_url: str, entry: str, tenant: str, app_version: str) -> Target:
    return Target(
        surface="web",
        app="coreserv",
        app_version=app_version,
        tenant=tenant,
        base_url=base_url.rstrip("/"),
        entry_path=entry,
        auth=AuthSpec(
            mode="form_login",
            path="/",
            credentials_ref={
                "username": "CORESERV_USERNAME",
                "password": "CORESERV_PASSWORD",
            },
            success_check=Condition(type="url_matches", pattern="/home|/search"),
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="discovery.run")
    parser.add_argument("--goal", required=True)
    parser.add_argument("--target", default="http://localhost:8800")
    parser.add_argument("--entry", default="/search")
    parser.add_argument(
        "--allow-path",
        action="append",
        default=[],
        help=(
            "Restrict the agent to these paths (repeatable). May only NARROW the "
            "declared policy in discovery/policy.json; a path outside it is refused."
        ),
    )
    parser.add_argument("--policy", default=str(DEFAULT_POLICY_PATH))
    parser.add_argument("--capability-id", default=None)
    parser.add_argument("--version", default="1.0.0")
    parser.add_argument("--tenant", default="northridge")
    parser.add_argument("--app-version", default="4.2.1")
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, choices=sorted(PROVIDERS))
    parser.add_argument("--model", default=None, help="Defaults to the provider's standard model.")
    parser.add_argument("--evidence-root", default="evidence/discovery")
    parser.add_argument("--out", default=None, help="Where to write the emitted artifact.")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    load_dotenv()

    try:
        policy = load_policy(args.policy, args.target, args.allow_path)
    except PolicyWidened as exc:
        print(json.dumps({"status": "policy_error", "message": str(exc)}, indent=2))
        raise SystemExit(2)
    target = build_target(args.target, args.entry, args.tenant, args.app_version)

    loop = DiscoveryLoop(
        goal=args.goal,
        policy=policy,
        target=target,
        evidence_dir=Path(args.evidence_root),
        headless=not args.headed,
        provider=args.provider,
        model=args.model,
    )
    outcome = asyncio.run(loop.run())

    summary = {
        "run_id": outcome.run_id,
        "status": outcome.status,
        "goal": outcome.goal,
        "provider": outcome.provider,
        "model": outcome.model,
        "rate_limit_retries": len(outcome.rate_limit_events),
        "steps_attempted": outcome.steps_attempted,
        "outputs": outcome.outputs,
        "summary": outcome.summary,
        "message": outcome.message,
        "duration_ms": round(outcome.duration_ms, 2),
    }

    if not outcome.succeeded:
        summary["artifact"] = None
        _write_summary(loop, summary)
        print(json.dumps(summary, indent=2))
        raise SystemExit(1)

    capability_id = args.capability_id or _derive_capability_id(args.goal)
    artifact = record(
        outcome=outcome,
        capability_id=capability_id,
        version=args.version,
        target=target,
        policy=policy,
        goal=args.goal,
        model=f"{outcome.provider}:{outcome.model}",
    )

    out_path = Path(args.out) if args.out else loop.evidence_dir / "artifact.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(artifact, indent=2), encoding="utf-8")

    # Load it back. If discovery cannot produce something the loader accepts,
    # the recorder is wrong -- not the artifact, which is why this fails the
    # run rather than writing a warning.
    try:
        load_artifact(out_path)
        summary["artifact_valid"] = True
    except ArtifactError as exc:
        summary["artifact_valid"] = False
        summary["validation_error"] = str(exc)

    summary["artifact"] = str(out_path)
    summary["steps_recorded"] = len(artifact["steps"])
    _write_summary(loop, summary)

    print(json.dumps(summary, indent=2))
    raise SystemExit(0 if summary.get("artifact_valid") else 1)


# Filler that names no part of the capability. "current"/"latest" and the
# like describe when, not what.
_GOAL_STOPWORDS = {
    "a", "an", "and", "the", "their", "its", "his", "her",
    "for", "of", "to", "in", "on", "up", "at", "from", "with",
    "read", "look", "get", "find", "fetch", "retrieve", "show", "check",
    "current", "currently", "latest", "please", "then",
}


def _derive_capability_id(goal: str) -> str:
    """Name the capability after what it does, not the record it was found on.

    Numeric tokens are dropped: the goal that discovered this flow named one
    member, but the flow is not about that member -- the parameter carries
    the identity. `member_10001_current_savings_balance` would make every
    invocation for a different member look like it was calling the wrong
    capability.
    """
    import re

    words = re.sub(r"[^a-z0-9\s]", " ", goal.lower()).split()
    keep = [
        w
        for w in words
        # Any token containing a digit is a value from this run, not a name.
        if w not in _GOAL_STOPWORDS and not any(c.isdigit() for c in w)
    ][:5]
    return "_".join(keep) or "discovered_capability"


def _write_summary(loop: DiscoveryLoop, summary: dict) -> None:
    scrubbed = Scrubber().scrub_obj(summary)
    (loop.evidence_dir / "summary.json").write_text(
        json.dumps(scrubbed, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
