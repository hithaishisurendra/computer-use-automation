"""Replay orchestration. No model client is imported anywhere in replay/.

Order of operations, and why it is this order:

    validate inputs        -- cheapest check, and a caller error should never
                              cost a browser launch
    resolve credentials    -- our own configuration; also free
    launch browser
    run target.auth        -- the flow starts post-authentication
    assert success_check   -- prove the session exists before trusting step 1
    execute steps          -- each with policy check, checkpoint, classification
    coerce outputs         -- a caller gets a Decimal, not "$1,240.55"
    return one typed result

The first two run before Chromium starts, which is what makes `caller_error`
and `auth_failure` honestly distinguishable from anything that happened on a
page.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from capability.schema import Artifact, Step
from capability.validate import (
    AuthConfigError,
    CallerInputError,
    redact,
    resolve_credentials,
    validate_inputs,
)
from perception.tree import snapshot_all_frames, filter_tree, to_compact_text
from replay import checkpoints, classify
from replay.executor import Executor, PolicyViolation, check_destination
from replay.evidence import EvidenceWriter
from replay.resolver import ElementUnresolvable
from escalation.session import ControlledSession
from replay.result import ReplayResult, StepTrace

# CoreServ prints its build in the nav frame footer, e.g. "CoreServ 4.2.1".
APP_VERSION_RE = re.compile(r"CoreServ\s+(\d+\.\d+\.\d+)")


class ReplayEngine:
    def __init__(
        self,
        artifact: Artifact,
        evidence_root: str | Path = "evidence/replay",
        headless: bool = True,
        escalate: bool = False,
        operator=None,
        escalation_root: str | Path = "evidence/escalation",
    ):
        self.artifact = artifact
        self.headless = headless
        self.run_id = f"run_{uuid.uuid4().hex[:8]}"
        self.evidence = EvidenceWriter(Path(evidence_root) / self.run_id, artifact)
        # Off by default: unattended replay must stay unattended. A capability
        # invoked by an agent in production has nobody at a terminal, and
        # blocking forever for one is worse than failing cleanly.
        self.escalate = escalate
        self.operator = operator
        self.escalation_root = escalation_root
        self.control = None

    # -- perception ---------------------------------------------------------

    async def _perceive(self) -> dict[str, Optional[dict]]:
        """Augmented accessibility trees, keyed by frame name.

        This is the only view of the page the resolver ever gets -- replay
        targets role + accessible name, never DOM or coordinates.
        """
        snapshots = await snapshot_all_frames(self.page)
        return {s.frame_name: s.tree for s in snapshots}

    async def _capture(self):
        """(frames, page_text, url) for checkpoints and classification.

        Policy is asserted here, on every observation, and not only in the
        executor immediately after an action. The immediate check races the
        app: clicking a link returns before the navigation lands, so the
        frame still reports its previous URL and an off-allowlist
        destination sails through. Checking at every capture closes that --
        you cannot observe a page without validating where it is. The
        executor's pre-navigation check stays as well, so an off-limits
        destination is refused before the request is ever issued.
        """
        frames = await self._perceive()
        for frame in self.page.frames:
            url = frame.url or ""
            if url and not url.startswith("about:"):
                check_destination(self.artifact, url)
        texts = []
        for tree in frames.values():
            compact = to_compact_text(filter_tree(tree))
            if compact:
                texts.append(compact)
        url = ""
        for frame in self.page.frames:
            if frame.name == "content":
                url = frame.url or ""
        if not url:
            url = self.page.url
        return frames, "\n".join(texts), url

    # -- drift --------------------------------------------------------------

    def _check_drift(self, page_text: str, result: ReplayResult) -> None:
        """Compare the recorded app_version against what the page shows.

        A mismatch is a warning, not a failure: the flow may well still work
        on a newer build, and refusing to run would make every vendor patch
        an outage. Recording it is what makes drift observable.
        """
        match = APP_VERSION_RE.search(page_text)
        if not match:
            return
        observed = match.group(1)
        expected = self.artifact.target.app_version
        if observed == expected:
            return
        warning = (
            f"app version drift: artifact recorded against CoreServ {expected}, "
            f"observed CoreServ {observed}"
        )
        # Drift is re-observed on every capture (the footer is on every
        # page); it is one fact about the run, so report it once.
        if warning not in result.warnings:
            result.warnings.append(warning)

    # -- auth ---------------------------------------------------------------

    async def _authenticate(self, credentials: dict[str, str]) -> None:
        auth = self.artifact.target.auth
        if auth is None:
            return
        if auth.mode != "form_login":
            raise AuthConfigError(self.artifact.capability.id, [], [])

        target = self.artifact.target.base_url.rstrip("/") + auth.path
        check_destination(self.artifact, target)
        await self.page.goto(target, wait_until="load")

        # Field targeting for the login form is intentionally by form field
        # name rather than by the element registry: authentication is engine
        # infrastructure, not a recorded step, so it must not depend on the
        # artifact declaring login elements.
        await self.page.fill('input[name="username"]', credentials.get("username", ""))
        await self.page.fill('input[name="password"]', credentials.get("password", ""))
        async with self.page.expect_navigation():
            await self.page.click('button:has-text("Login")')

    # -- steps --------------------------------------------------------------

    async def _run_step(
        self, step: Step, params: dict[str, Any], result: ReplayResult
    ) -> tuple[str, Optional[classify.Detection], Optional[str]]:
        """Run one step with recovery. Returns (status, terminal_detection, extracted).

        Recovery is bounded: a recoverable condition is retried at most
        MAX_RECOVERY_ATTEMPTS times, after which it is escalated to a hard
        failure rather than looped on.
        """
        trace = StepTrace(step_id=step.id, action=step.action)
        started = time.perf_counter()
        extracted: Optional[str] = None
        attempt = 0

        while True:
            attempt += 1
            trace.attempts = attempt

            try:
                outcome = await self.executor.execute(step, params)
                extracted = outcome.extracted
                if outcome.resolution:
                    trace.resolution = outcome.resolution.as_dict()
                    if outcome.resolution.used_brittle_rung:
                        result.warnings.append(
                            f"step {step.id}: element {step.element!r} resolved only via a "
                            f"brittle rung ({outcome.resolution.rung.strategy}) -- drift signal"
                        )
                if outcome.detail:
                    trace.detail = outcome.detail
            except ElementUnresolvable as exc:
                detection = classify.element_unresolvable_detection(
                    exc.resolution.element_key, str(exc)
                )
                trace.resolution = exc.resolution.as_dict()
                trace.detections.append(detection.as_dict())
                trace.status = "failed"
                trace.duration_ms = (time.perf_counter() - started) * 1000
                result.trace.append(trace)
                return "failed", detection, None
            except PolicyViolation:
                # Policy violations are the one class of error that must not
                # be softened into a step result -- they abort the whole run.
                raise
            except Exception as exc:
                # Anything the browser raises that we did not anticipate
                # still has to leave through the result contract rather than
                # as a traceback: a caller gets one typed result, always.
                detection = classify.Detection(
                    name="driver_error",
                    layer="engine",
                    classification="hard_failure",
                    message=f"Unexpected driver error on step {step.id}: {exc}",
                    escalation_eligible=True,
                )
                trace.detections.append(detection.as_dict())
                trace.status = "failed"
                trace.duration_ms = (time.perf_counter() - started) * 1000
                result.trace.append(trace)
                return "failed", detection, None

            frames, page_text, url = await self._capture()
            self._check_drift(page_text, result)

            # Classification runs BEFORE the checkpoint: an interstitial or a
            # session bounce explains why a checkpoint would fail, and
            # reporting "checkpoint not met" when the real cause is an
            # expired session is the misleading error this ordering avoids.
            detection = classify.classify(self.artifact, step.outcomes, page_text, url)

            if detection is not None and detection.classification == "recoverable":
                trace.detections.append(detection.as_dict())
                result.recoverable_conditions.append(
                    {"step_id": step.id, "attempt": attempt, **detection.as_dict()}
                )
                if attempt <= classify.MAX_RECOVERY_ATTEMPTS:
                    await self._recover(detection)
                    continue
                escalated = classify.Detection(
                    name=detection.name,
                    layer="engine",
                    classification="hard_failure",
                    message=(
                        f"{detection.message} Recovery attempted "
                        f"{classify.MAX_RECOVERY_ATTEMPTS} times without clearing it."
                    ),
                    escalation_eligible=True,
                )
                trace.status = "failed"
                trace.duration_ms = (time.perf_counter() - started) * 1000
                result.trace.append(trace)
                return "failed", escalated, None

            if detection is not None:
                trace.detections.append(detection.as_dict())
                trace.status = "failed" if detection.classification == "hard_failure" else "ok"
                trace.duration_ms = (time.perf_counter() - started) * 1000
                result.trace.append(trace)
                return trace.status, detection, extracted

            if step.checkpoint is not None:
                check = await checkpoints.evaluate(
                    step.checkpoint,
                    self.artifact,
                    self._capture,
                    params,
                    step.checkpoint.timeout_ms,
                )
                trace.checkpoint = check.as_dict()
                if not check.satisfied:
                    # Re-classify against the state at timeout: a checkpoint
                    # can fail because a declared outcome occurred, which is
                    # an answer rather than a failure.
                    _, late_text, late_url = await self._capture()
                    late = classify.classify(self.artifact, step.outcomes, late_text, late_url)
                    if late is not None:
                        trace.detections.append(late.as_dict())
                        trace.status = "failed" if late.classification == "hard_failure" else "ok"
                        trace.duration_ms = (time.perf_counter() - started) * 1000
                        result.trace.append(trace)
                        return trace.status, late, extracted

                    timeout_det = classify.timeout_detection(step.id, check.expected, check.observed)
                    result.recoverable_conditions.append(
                        {"step_id": step.id, "attempt": attempt, **timeout_det.as_dict()}
                    )
                    if attempt <= classify.MAX_RECOVERY_ATTEMPTS:
                        trace.detections.append(timeout_det.as_dict())
                        await asyncio.sleep(0.5 * attempt)
                        continue
                    trace.status = "failed"
                    trace.duration_ms = (time.perf_counter() - started) * 1000
                    result.trace.append(trace)
                    return "failed", None, None

            trace.status = "recovered" if attempt > 1 else "ok"
            trace.duration_ms = (time.perf_counter() - started) * 1000
            result.trace.append(trace)
            return "ok", None, extracted

    async def _recover(self, detection: classify.Detection) -> None:
        if detection.name == "maintenance_interstitial":
            # The interstitial renders into EVERY frame while the condition
            # holds, so it must be dismissed in every frame that shows it,
            # not just the first one found. Dismissing once clears the
            # server-side condition, but a frame that does not re-render
            # keeps displaying its stale copy -- and since classification
            # reads text from all frames, that stale copy would keep
            # matching and burn the retry budget on an already-cleared
            # condition. Clicking each frame's own Continue re-renders that
            # frame via its own return_to.
            for frame in self.page.frames:
                try:
                    button = frame.locator(f'button:has-text("{classify.INTERSTITIAL_DISMISS}")')
                    if await button.count() >= 1:
                        await button.first.click()
                        await self._await_dismissal(frame)
                except Exception:
                    continue
            return
        await asyncio.sleep(0.5)

    async def _await_dismissal(self, frame, timeout_s: float = 5.0) -> None:
        """Wait until a dismissed interstitial has actually gone.

        Returning as soon as the click is issued leaves the app's
        POST-redirect in flight, and the retry's navigation then races it --
        which the browser reports as an aborted or interrupted navigation
        and which would surface as a spurious driver error on a run that was
        recovering correctly. Waiting for the control to disappear is the
        observable signal that the re-render landed; polling for it beats
        sleeping a guessed interval.
        """
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            try:
                await frame.wait_for_load_state("load", timeout=1000)
            except Exception:
                pass
            try:
                remaining = await frame.locator(
                    f'button:has-text("{classify.INTERSTITIAL_DISMISS}")'
                ).count()
            except Exception:
                return  # frame navigated out from under us; that is the goal
            if remaining == 0:
                return
            await asyncio.sleep(0.1)

    # -- outputs ------------------------------------------------------------

    def _coerce(self, name: str, raw: str) -> Any:
        """Type-coerce an extracted value per its declared output type.

        Returning "$1,240.55" to a calling agent passes the parsing problem
        downstream, which is the thing declared output types exist to stop.
        """
        spec = self.artifact.output_map[name]
        text = (raw or "").strip()
        if spec.type == "money":
            cleaned = re.sub(r"[^0-9.\-]", "", text)
            try:
                return str(Decimal(cleaned))
            except (InvalidOperation, ValueError):
                raise ValueError(f"output {name!r}: could not parse {text!r} as money")
        if spec.type == "integer":
            return int(re.sub(r"[^0-9\-]", "", text))
        if spec.type == "number":
            return float(re.sub(r"[^0-9.\-]", "", text))
        if spec.type == "boolean":
            return text.lower() in ("true", "yes", "1", "checked")
        return text

    # -- run ----------------------------------------------------------------

    async def run(self, params: dict[str, Any]) -> ReplayResult:
        started = time.perf_counter()
        result = ReplayResult(
            classification="success",
            capability_id=self.artifact.capability.id,
            capability_version=self.artifact.capability.version,
            tenant=self.artifact.target.tenant,
            run_id=self.run_id,
        )

        # 1. Caller inputs -- before any browser exists.
        try:
            validated = validate_inputs(self.artifact, params)
        except CallerInputError as exc:
            result.classification = "caller_error"
            result.message = str(exc)
            result.violations = exc.as_result()["violations"]
            result.duration_ms = (time.perf_counter() - started) * 1000
            self.evidence.write_result(result)
            return result

        result.inputs_redacted = validated.redacted
        self.evidence.log(
            "inputs_validated", {"inputs": validated.redacted, "run_id": self.run_id}
        )

        # 2. Our own credentials -- also before any browser exists.
        try:
            credentials = self._credentials = resolve_credentials(self.artifact)
        except AuthConfigError as exc:
            result.classification = "auth_failure"
            result.message = str(exc)
            result.duration_ms = (time.perf_counter() - started) * 1000
            self.evidence.log("auth_failure", exc.as_result())
            self.evidence.write_result(result)
            return result

        # Register the literal values with the scrubber before anything can
        # be written: CoreServ renders the username into its nav frame, so
        # page snapshots would otherwise carry it verbatim.
        self.evidence.register_secrets(credentials.values())
        self.evidence.log("credentials_resolved", self.evidence.describe_auth())

        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            self.page = await browser.new_page()
            # One session for the whole run. If a human is called in, they
            # drive this context -- never a fresh one.
            self.control = ControlledSession(page=self.page)
            self.executor = Executor(
                self.page, self.artifact, self._perceive, control=self.control
            )

            try:
                await self._run_flow(validated.values, result)
            except PolicyViolation as exc:
                result.classification = "hard_failure"
                result.failed_step = exc.step_id
                result.expected = f"action/destination within policy ({exc.kind})"
                result.observed = exc.detail
                result.message = str(exc)
                result.escalation_eligible = True
                result.violations.append(
                    {"kind": exc.kind, "detail": exc.detail, "step_id": exc.step_id}
                )
                await self._capture_failure_evidence(result)
            finally:
                result.duration_ms = (time.perf_counter() - started) * 1000
                if self.control is not None:
                    self.control.release(f"run finished: {result.classification}")
                    result.control = self.control.as_dict()
                self.evidence.write_result(result)
                await browser.close()

        return result

    async def _run_flow(self, params: dict[str, Any], result: ReplayResult) -> None:
        auth = self.artifact.target.auth
        if auth is not None:
            await self._authenticate(self._credentials)

            frames, page_text, url = await self._capture()
            check = checkpoints.evaluate_once(
                auth.success_check, self.artifact, frames, page_text, url, params
            )
            self.evidence.log("auth", {"success_check": check.as_dict()})
            if not check.satisfied:
                # Engine universals are checked first here for the same
                # reason they are during steps: when the app is throwing 500s
                # or the session bounced, the auth assertion fails as a
                # *symptom*. Reporting auth_failure would send someone to
                # check credentials that are perfectly fine, when the real
                # answer is that the application is down.
                universal = classify.detect_engine_universals(page_text)
                if universal is not None:
                    result.classification = "hard_failure"
                    result.failed_step = "auth"
                    result.expected = check.expected
                    result.observed = universal.message
                    result.message = universal.message
                    result.escalation_eligible = universal.escalation_eligible
                    await self._capture_failure_evidence(result)
                    return

                result.classification = "auth_failure"
                result.expected = check.expected
                result.observed = check.observed
                result.message = (
                    "Authentication did not produce a usable session: "
                    f"expected {check.expected}, observed {check.observed}."
                )
                await self._capture_failure_evidence(result)
                return

        if len(self.artifact.steps) > self.artifact.policy.max_steps:
            raise PolicyViolation(
                "max_steps",
                f"artifact declares {len(self.artifact.steps)} steps, policy allows "
                f"{self.artifact.policy.max_steps}",
            )

        extracted: dict[str, str] = {}

        index = 0
        while index < len(self.artifact.steps):
            step = self.artifact.steps[index]
            index += 1
            status, detection, value = await self._run_step(step, params, result)
            self.evidence.log("step", result.trace[-1].as_dict() if result.trace else {})

            if detection is not None and detection.classification == "business_outcome":
                result.classification = "business_outcome"
                result.outcome = detection.name
                result.message = detection.message
                await self._capture_failure_evidence(result)
                return

            if status == "failed":
                result.classification = "hard_failure"
                result.failed_step = step.id
                trace = result.trace[-1] if result.trace else None
                if detection is not None:
                    result.message = detection.message
                    result.expected = f"step {step.id} to complete"
                    result.observed = detection.message
                    result.escalation_eligible = detection.escalation_eligible
                elif trace and trace.checkpoint:
                    result.expected = trace.checkpoint.get("expected")
                    result.observed = trace.checkpoint.get("observed")
                    result.message = (
                        f"Step {step.id} checkpoint not met: expected "
                        f"{result.expected}, observed {result.observed}."
                    )

                if self._may_escalate(result):
                    resumed = await self._escalate(step, params, result)
                    if resumed:
                        # Resume from where it stopped, not from the top.
                        # Re-running completed steps would repeat side
                        # effects and undo whatever the operator just fixed.
                        result.classification = "success"
                        result.failed_step = None
                        result.expected = result.observed = result.message = None
                        continue

                await self._capture_failure_evidence(result)
                return

            if step.action == "extract" and value is not None:
                extracted[step.into] = value

        # Coerce declared outputs. A run that reached the end but produced
        # nothing is a failure, not a success.
        for spec in self.artifact.outputs:
            if spec.name in extracted:
                try:
                    result.outputs[spec.name] = self._coerce(spec.name, extracted[spec.name])
                except ValueError as exc:
                    result.classification = "hard_failure"
                    result.failed_step = "output_coercion"
                    result.expected = f"output {spec.name!r} parseable as {spec.type}"
                    result.observed = str(exc)
                    result.message = str(exc)
                    await self._capture_failure_evidence(result)
                    return
            elif spec.required:
                result.classification = "hard_failure"
                result.failed_step = "output_extraction"
                result.expected = f"required output {spec.name!r} to be extracted"
                result.observed = "no value was extracted"
                result.message = (
                    f"Flow completed but required output {spec.name!r} was never extracted."
                )
                await self._capture_failure_evidence(result)
                return

        result.classification = "success"

    # -- escalation ---------------------------------------------------------

    def _may_escalate(self, result: ReplayResult) -> bool:
        """Escalate only where a human could actually help.

        Gated on `escalate` so unattended replay stays unattended: a
        production caller invoking a capability has nobody at a terminal, and
        a run that blocks forever waiting for one is worse than a run that
        fails cleanly.
        """
        return bool(self.escalate and self.operator and result.escalation_eligible)

    async def _escalate(self, step, params: dict[str, Any], result: ReplayResult) -> bool:
        """Pause, hand the live session to a human, then resume on the same one.

        Returns True when the operator resumed AND the step's checkpoint now
        holds. A resume that leaves the checkpoint still failing is not a
        recovery: the run would carry on from a state it never verified.
        """
        from escalation.capture import HumanActionCapture, write_activity
        from escalation.request import InterventionRequest, capture_state, escalation_dir, write_request

        directory = escalation_dir(self.run_id, self.escalation_root)
        url, screenshot, snapshot_path, snapshot = await capture_state(
            self.page, self._perceive, directory
        )

        request = InterventionRequest(
            run_id=self.run_id,
            source="replay",
            goal=f"Replay {self.artifact.capability.id}@{self.artifact.capability.version}",
            reason=result.message or "step failed",
            classification="hard_failure",
            capability_id=self.artifact.capability.id,
            capability_version=self.artifact.capability.version,
            step_id=step.id,
            expected=result.expected,
            observed=result.observed,
            url=url,
            screenshot_path=screenshot,
            snapshot_path=snapshot_path,
            snapshot=snapshot,
            inputs_redacted=result.inputs_redacted,
            completed_steps=[t.step_id for t in result.trace if t.status in ("ok", "recovered")],
        )
        request_path = write_request(request, self.escalation_root)
        result.evidence["intervention_request"] = str(request_path)
        self.evidence.log("intervention_raised", {"step_id": step.id, "request": str(request_path)})

        capture = HumanActionCapture(self.page, self._perceive)
        await capture.begin()

        # Automation is locked out from here until control comes back. The
        # executor asserts this on every action, so a stray retry cannot race
        # the operator.
        self.control.hand_to_human(f"step {step.id}: {result.message}")
        try:
            decision = self.operator.handle(request)
        finally:
            self.control.take_back(f"operator returned control after step {step.id}")

        activity = await capture.end(decision)
        handoff_path = write_activity(
            self.run_id, activity, self.control.as_dict(), self.escalation_root
        )
        result.evidence["handoff"] = str(handoff_path)
        result.human_interventions.append(
            {
                "step_id": step.id,
                "decision": decision.decision.value,
                "operator": decision.operator,
                "notes": decision.notes,
                "url_changed": activity.url_changed,
            }
        )
        self.evidence.log("intervention_resolved", result.human_interventions[-1])

        if not decision.resumed:
            result.message = f"Operator aborted at step {step.id}: {decision.notes}".strip()
            return False

        # The session must be the one that got stuck.
        self.control.assert_same_session()

        if step.checkpoint is None:
            return True

        check = await checkpoints.evaluate(
            step.checkpoint, self.artifact, self._capture, params, step.checkpoint.timeout_ms
        )
        if check.satisfied:
            return True

        result.expected = check.expected
        result.observed = check.observed
        result.message = (
            f"Operator resumed at step {step.id} but its checkpoint still fails: "
            f"expected {check.expected}, observed {check.observed}."
        )
        return False

    async def _capture_failure_evidence(self, result: ReplayResult) -> None:
        """On any non-success, capture the page as it stood when it stopped."""
        try:
            frames, page_text, _ = await self._capture()
            paths = await self.evidence.capture_failure(self.page, frames)
            result.evidence.update(paths)
        except Exception as exc:  # evidence must never mask the real failure
            self.evidence.log("evidence_capture_failed", {"error": str(exc)})


async def replay(artifact: Artifact, params: dict[str, Any], **kwargs) -> ReplayResult:
    engine = ReplayEngine(artifact, **kwargs)
    return await engine.run(params)
