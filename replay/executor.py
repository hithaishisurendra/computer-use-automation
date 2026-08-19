"""Action execution with policy enforcement at the boundary.

The policy check lives HERE, immediately before the action runs, and not in
the caller. That placement is the whole point: a guardrail the caller is
trusted to invoke is a convention, and conventions are not enforced when a
future code path forgets them. Every action goes through `execute`, and
`execute` refuses before touching the page.

A violation is an immediate hard failure, never a warning and never a skip.
An agent that tried to navigate outside its allowlist has done something the
operator did not sanction, and continuing would mean the allowlist describes
intent rather than behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

from capability.schema import Artifact, Step
from replay import resolver
from replay.resolver import Resolution


class PolicyViolation(Exception):
    """An action or destination outside what the artifact's policy permits."""

    def __init__(self, kind: str, detail: str, step_id: Optional[str] = None):
        self.kind = kind
        self.detail = detail
        self.step_id = step_id
        super().__init__(f"policy violation ({kind}): {detail}")


@dataclass
class ActionOutcome:
    step_id: str
    action: str
    resolution: Optional[Resolution] = None
    extracted: Optional[str] = None
    detail: Optional[str] = None


def _path_allowed(path: str, allowed: list[str]) -> bool:
    """Glob-aware path check. `/member/*` covers `/member/10001`.

    Matching is done on the path alone, with any query string stripped by
    the caller, so an allowlist entry never has to anticipate query
    parameters.
    """
    import fnmatch

    return any(path == entry or fnmatch.fnmatch(path, entry) for entry in allowed)


def check_destination(artifact: Artifact, url: str, step_id: Optional[str] = None) -> None:
    """Assert a URL is inside the artifact's allowed origins and paths."""
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in artifact.policy.allowed_origins:
        raise PolicyViolation(
            "origin",
            f"{origin!r} is not in allowed_origins {artifact.policy.allowed_origins}",
            step_id,
        )

    if not _path_allowed(parsed.path or "/", artifact.policy.allowed_paths):
        raise PolicyViolation(
            "path",
            f"{parsed.path!r} is not in allowed_paths {artifact.policy.allowed_paths}",
            step_id,
        )


def check_action(artifact: Artifact, action: str, step_id: Optional[str] = None) -> None:
    if action not in artifact.policy.allowed_actions:
        raise PolicyViolation(
            "action",
            f"{action!r} is not in allowed_actions {artifact.policy.allowed_actions}",
            step_id,
        )


def check_risk(artifact: Artifact, step: Step) -> Optional[str]:
    """Apply the artifact's risky-action policy.

    Returns a note when a risky step is merely flagged. Raises when policy
    says to block. `require_confirmation` has no interactive channel during
    an unattended replay, so it is treated as a block and routed to the
    human-escalation path rather than silently proceeding -- proceeding is
    exactly what "requires confirmation" rules out.
    """
    if step.risk != "risky":
        return None

    handling = artifact.policy.risky_action_handling
    if handling == "flag":
        return f"step {step.id} is risky and was flagged (policy: flag)"
    raise PolicyViolation(
        "risky_action",
        (
            f"step {step.id} is marked risky and policy is {handling!r}; unattended replay "
            "cannot obtain confirmation, so the step is blocked pending human escalation"
        ),
        step.id,
    )


class Executor:
    """Performs the seven actions against a live Playwright page.

    The browser is the only thing this class knows about that the rest of
    replay/ does not; keeping it in one place is what lets the resolver,
    checkpoints and classifier stay unit-testable without Chromium.
    """

    def __init__(self, page, artifact: Artifact, perceive):
        self.page = page
        self.artifact = artifact
        # perceive() -> dict[frame_name, augmented tree]; injected so the
        # executor never reaches into perception directly.
        self.perceive = perceive

    # -- locating -----------------------------------------------------------

    async def _locate(self, element_key: str, params: dict[str, Any]) -> tuple[Resolution, Any]:
        element = self.artifact.elements[element_key]
        frames = await self.perceive()
        resolution = resolver.require_element(element_key, element, frames, params)

        ref = resolution.node.get("ref")
        if not ref:
            raise resolver.ElementUnresolvable(resolution)

        frame = self._frame_by_name(resolution.frame_name)
        locator = frame.locator(f"aria-ref={ref}")
        return resolution, locator

    def _frame_by_name(self, name: Optional[str]):
        for frame in self.page.frames:
            if (frame.name or "") == (name or ""):
                return frame
        raise PolicyViolation("frame", f"frame {name!r} is not present on the page")

    # -- actions ------------------------------------------------------------

    async def execute(self, step: Step, params: dict[str, Any]) -> ActionOutcome:
        """Run one step. Policy is checked before anything touches the page."""
        check_action(self.artifact, step.action, step.id)
        risk_note = check_risk(self.artifact, step)

        if step.action == "navigate":
            target = self.artifact.target.base_url.rstrip("/") + step.path
            check_destination(self.artifact, target, step.id)
            if step.frame:
                # Frameset app: load into the content region. A top-level
                # goto would replace the frameset itself, and every element
                # in the flow declares the frame it lives in.
                await self._goto(self._frame_by_name(step.frame), target)
            else:
                await self._goto(self.page, target)
            await self._assert_current_url(step.id)
            return ActionOutcome(step.id, step.action, detail=risk_note)

        if step.action == "wait_for":
            # Declared in the vocabulary but unused in 1.0.0: the per-step
            # checkpoint that follows is what actually waits.
            return ActionOutcome(step.id, step.action, detail=risk_note)

        resolution, locator = await self._locate(step.element, params)

        if step.action == "click":
            await locator.click()
            await self._settle()
            await self._assert_current_url(step.id)
            return ActionOutcome(step.id, step.action, resolution, detail=risk_note)

        if step.action == "fill":
            value = resolver.substitute(step.value, params)
            await locator.fill(value)
            return ActionOutcome(step.id, step.action, resolution, detail=risk_note)

        if step.action == "select":
            value = resolver.substitute(step.value, params)
            await locator.select_option(value)
            return ActionOutcome(step.id, step.action, resolution, detail=risk_note)

        if step.action == "check":
            await locator.check()
            return ActionOutcome(step.id, step.action, resolution, detail=risk_note)

        if step.action == "extract":
            text = (resolution.node.get("name") or "").strip()
            if not text:
                text = (await locator.inner_text()).strip()
            return ActionOutcome(step.id, step.action, resolution, extracted=text, detail=risk_note)

        raise PolicyViolation("action", f"unhandled action {step.action!r}", step.id)

    async def _goto(self, target_context, url: str) -> None:
        """Navigate, tolerating a redirect already in flight.

        After the engine dismisses a maintenance interstitial, the app's own
        POST-redirect is still travelling toward the same URL the retry is
        about to request. Playwright reports that overlap as "interrupted by
        another navigation" -- which is not a failure when the frame ends up
        exactly where the step asked it to go. Anything else re-raises.
        """
        benign = ("interrupted by another navigation", "ERR_ABORTED")
        try:
            await target_context.goto(url, wait_until="load")
        except Exception as exc:
            if not any(marker in str(exc) for marker in benign):
                raise
            try:
                await target_context.wait_for_load_state("load", timeout=5000)
            except Exception:
                pass
            landed = (target_context.url or "").split("?")[0]
            if landed.rstrip("/") != url.split("?")[0].rstrip("/"):
                raise

    async def _settle(self) -> None:
        """Let a server-rendered navigation land before the checkpoint polls.

        Waits on every frame, not just the page: in a frameset the top
        document is already loaded, so waiting on the page alone returns
        instantly while the content frame is still navigating.
        """
        for context in (self.page, *self.page.frames):
            try:
                await context.wait_for_load_state("load", timeout=5000)
            except Exception:
                # A click that triggers no navigation is legitimate; the
                # checkpoint is what decides whether the step worked.
                continue

    async def _assert_current_url(self, step_id: str) -> None:
        """Re-check policy against where we actually ended up.

        A click can navigate anywhere the app chooses -- a redirect to an
        interstitial, a session bounce to the login page. Checking only the
        intended destination would let the allowlist be escaped by any
        server-side redirect.
        """
        for frame in self.page.frames:
            url = frame.url or ""
            if not url or url.startswith("about:"):
                continue
            check_destination(self.artifact, url, step_id)
