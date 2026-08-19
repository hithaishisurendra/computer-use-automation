"""Checkpoint evaluation.

A checkpoint answers "did the step actually do what it was supposed to?".
Every evaluation returns both what was expected and what was *observed*,
because that pair is the debuggable error the brief asks for: "step s4
expected the member detail heading, observed the results table still
present" is actionable; "step s4 failed" is not.

Checkpoints poll until their per-checkpoint deadline rather than checking
once, since a server-rendered page arrives asynchronously and a single
immediate check would race the navigation.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from capability.schema import Artifact, Condition
from replay import resolver


@dataclass
class CheckResult:
    satisfied: bool
    expected: str
    observed: str
    duration_ms: float = 0.0
    children: list["CheckResult"] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "satisfied": self.satisfied,
            "expected": self.expected,
            "observed": self.observed,
        }
        if self.duration_ms:
            d["duration_ms"] = round(self.duration_ms, 2)
        if self.children:
            d["children"] = [c.as_dict() for c in self.children]
        return d


def describe(condition: Condition) -> str:
    """Human-readable rendering of what a condition demands."""
    t = condition.type
    if t == "element_present":
        return f"element {condition.element!r} present"
    if t == "text_present":
        return f"text {condition.text!r} present on page"
    if t == "url_matches":
        return f"url matching {condition.pattern!r}"
    if t in ("any_of", "all_of"):
        joiner = " OR " if t == "any_of" else " AND "
        return "(" + joiner.join(describe(c) for c in condition.conditions or []) + ")"
    return f"<unknown condition {t!r}>"


def evaluate_once(
    condition: Condition,
    artifact: Artifact,
    frames: dict[str, Optional[dict]],
    page_text: str,
    url: str,
    params: dict[str, Any],
) -> CheckResult:
    """Evaluate a condition against one already-captured snapshot.

    Takes a snapshot rather than a live page so that every sub-condition of
    an any_of/all_of is judged against the *same* observed state -- polling
    each child independently would let a checkpoint pass against two
    different moments in time.
    """
    expected = describe(condition)
    t = condition.type

    if t == "element_present":
        element = artifact.elements.get(condition.element)
        if element is None:
            return CheckResult(False, expected, f"element {condition.element!r} not declared")
        resolution = resolver.resolve_element(condition.element, element, frames, params)
        if resolution.resolved:
            return CheckResult(True, expected, f"resolved via rung {resolution.rung_index}")
        last = resolution.attempts[-1] if resolution.attempts else None
        observed = "not found"
        if last and last.outcome == "ambiguous":
            observed = f"ambiguous: {last.match_count} nodes matched"
        return CheckResult(False, expected, observed)

    if t == "text_present":
        if condition.text in page_text:
            return CheckResult(True, expected, "found")
        return CheckResult(False, expected, "text not present on page")

    if t == "url_matches":
        if re.search(condition.pattern, url):
            return CheckResult(True, expected, f"url is {url!r}")
        return CheckResult(False, expected, f"url is {url!r}")

    if t in ("any_of", "all_of"):
        children = [
            evaluate_once(c, artifact, frames, page_text, url, params)
            for c in condition.conditions or []
        ]
        satisfied = (
            any(c.satisfied for c in children) if t == "any_of" else all(c.satisfied for c in children)
        )
        if satisfied:
            met = next((c for c in children if c.satisfied), None) if t == "any_of" else None
            observed = f"satisfied by: {met.expected}" if met else "all sub-conditions satisfied"
        else:
            observed = "; ".join(f"{c.expected} -> {c.observed}" for c in children)
        return CheckResult(satisfied, expected, observed, children=children)

    return CheckResult(False, expected, f"unknown condition type {t!r}")


async def evaluate(
    condition: Condition,
    artifact: Artifact,
    capture,
    params: dict[str, Any],
    timeout_ms: int,
    poll_ms: int = 250,
) -> CheckResult:
    """Poll a condition until it is satisfied or the deadline passes.

    `capture` is an awaitable returning (frames, page_text, url) -- injected
    rather than taking a page directly so this module has no browser
    dependency and stays unit-testable without launching Chromium.
    """
    started = time.perf_counter()
    deadline = started + (timeout_ms / 1000)
    result = None

    while True:
        frames, page_text, url = await capture()
        result = evaluate_once(condition, artifact, frames, page_text, url, params)
        if result.satisfied:
            break
        if time.perf_counter() >= deadline:
            result.observed = f"{result.observed} (after {timeout_ms}ms)"
            break
        await _sleep(poll_ms / 1000)

    result.duration_ms = (time.perf_counter() - started) * 1000
    return result


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)
