"""CLI entry point for deterministic replay.

    python -m replay.run --capability member_savings_balance --version 1.0.0 \
        --input member_ref=10001 [--tenant cascade]

Prints the structured result as JSON. Exit code is 0 for success and a
business outcome alike -- a business outcome is a legitimate answer, not a
failure -- and non-zero for caller_error, auth_failure and hard_failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from capability.loader import ArtifactError, load_resolved
from capability.sink import null_sink
from replay.engine import ReplayEngine

EXIT_CODES = {
    "success": 0,
    "business_outcome": 0,
    "caller_error": 2,
    "auth_failure": 3,
    "hard_failure": 1,
}


def parse_inputs(pairs: list[str]) -> dict[str, str]:
    params: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--input expects name=value, got {pair!r}")
        name, value = pair.split("=", 1)
        params[name.strip()] = value
    return params


def main() -> None:
    parser = argparse.ArgumentParser(prog="replay.run")
    parser.add_argument("--capability", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--input", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--tenant", default=None)
    parser.add_argument("--capabilities-root", default="capabilities")
    parser.add_argument("--evidence-root", default="evidence/replay")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--escalate",
        action="store_true",
        help=(
            "Hand a stuck run to a human operator on the same live session instead of "
            "failing. Implies --headed, since a person has to be able to drive it. "
            "Off by default so unattended replay stays unattended."
        ),
    )
    args = parser.parse_args()

    try:
        artifact = load_resolved(
            args.capabilities_root, args.capability, args.version, tenant=args.tenant
        )
    except ArtifactError as exc:
        print(null_sink().emit({"classification": "hard_failure", "message": str(exc)}))
        raise SystemExit(1)

    operator = None
    if args.escalate:
        from escalation.operator import ConsoleOperator

        operator = ConsoleOperator()

    engine = ReplayEngine(
        artifact,
        evidence_root=args.evidence_root,
        # A headless browser cannot be driven by a human, so escalation forces
        # a visible one rather than silently offering an unusable handoff.
        headless=not (args.headed or args.escalate),
        escalate=args.escalate,
        operator=operator,
    )
    result = asyncio.run(engine.run(parse_inputs(args.input)))

    # A payload handed to a caller is not safer than one written to disk.
    # This printed the raw result until the chokepoint existed, and it is the
    # shape the capability API's response will take.
    print(engine.sink.emit(result.as_dict()))
    raise SystemExit(EXIT_CODES.get(result.classification, 1))


if __name__ == "__main__":
    main()
