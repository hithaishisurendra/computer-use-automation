"""LLM-driven discovery: observe -> decide -> act against a live surface.

The model is in the loop here and only here. It decides *what to do*; it does
not decide what is allowed. Every action it requests goes through the same
`replay.executor` that production replay uses, so the policy allowlist, the
risky-action rule and the frame/element resolution are literally the same code
on both paths.

That sharing is necessary and it was not sufficient. This docstring used to
claim "discovery cannot be more permissive than replay because there is no
second implementation for it to be permissive in" -- and that was false. The
executor's risk gate reads `Step.risk`, and discovery constructed every Step
without one, so the field defaulted to "safe" and `check_risk` never fired.
Risk was assigned afterwards, by the recorder, at record time. Shared code is
not shared enforcement: a guard that is never handed the input that trips it
is not a guard. Observed live -- a discovery run clicked "Post Transfer" and
moved money with no block and no escalation.

So the classification now happens *here*, at act time, using the same app
profile verb rules the recorder uses, and the same `check_risk` decides what
to do about it. Under `require_confirmation` that means a discovery run
walking an irreversible flow stops at the irreversible step.

The loop is deliberately narrow:

- **observe** hands the model a compact accessibility snapshot per frame plus
  the current URL and the result of its last action. No screenshot, no HTML.
  Whatever the model can see is what perception can express, which is what
  keeps a discovered flow recordable.
- **decide** is a tool call from a closed vocabulary matching the seven
  artifact actions exactly (see `discovery.prompts`).
- **act** executes through the shared executor and captures evidence.

Credential values never reach the model: authentication happens before the
loop starts, and observations are scrubbed of credential literals on the way
out.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from capability.profile import profile_for
from capability.redaction import Scrubber
from capability.sink import RedactionSink
from capability.schema import Element, LocatorRung, Policy, Scope, Step, Target
from capability.validate import AuthConfigError, resolve_credentials
from discovery.model import (
    ActionResult,
    estimate_cost_usd,
    AssistantAction,
    Message,
    ModelClient,
    Observation,
    build_client,
)
from discovery.prompts import ACTION_TOOLS, TERMINAL_TOOLS, build_system_prompt, build_tools
from perception.tree import filter_tree, snapshot_all_frames, to_compact_text
from replay import resolver
from escalation.session import ControlledSession
from replay.executor import Executor, PolicyViolation, RiskBlocked, check_destination
from replay.resolver import ElementUnresolvable

# Anthropic by default. Gemini stays a first-class, fully-wired alternative --
# both implementations remaining is what makes the provider seam a seam rather
# than a claim, and the Gemini-recorded evidence in evidence/discovery/ stays
# valid because the artifact does not depend on which model produced it.
DEFAULT_PROVIDER = "anthropic"

MAX_STEPS = 25
# The run's own time budget. Provider backoff is deliberately NOT counted
# against it: a run that spent four minutes waiting out a free-tier rate limit
# has not wandered for four minutes, and failing it as a timeout reports the
# agent's behaviour when the cause was the quota. Each call's backoff is
# bounded by MAX_RETRIES, so excluding it cannot make a run hang.
MAX_WALL_CLOCK_S = 300  # 5 minutes
MAX_CONSECUTIVE_FAILURES = 3


class ProvisionalArtifact:
    """What the executor needs before an artifact exists.

    The executor reads exactly three things off an artifact: `policy`,
    `target`, and the `elements` registry. During discovery the registry is
    not written yet -- it is being *discovered* -- so this stands in and
    grows an entry each time the model names a target. Duck-typing it rather
    than constructing a real `Artifact` avoids the alternative, which would
    be either loosening the artifact's own validation (it requires steps and
    coherent cross-references) or duplicating the executor. Reusing the
    executor unchanged is the point.
    """

    def __init__(self, policy: Policy, target: Target):
        self.policy = policy
        self.target = target
        self.elements: dict[str, Element] = {}


@dataclass
class Cycle:
    """One observe -> decide -> act cycle, kept whole for evidence."""

    index: int
    url: str
    observation: str
    reasoning: str
    tool_name: Optional[str] = None
    tool_input: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"  # ok | failed | terminal
    detail: Optional[str] = None
    element_key: Optional[str] = None
    resolution: Optional[dict[str, Any]] = None
    extracted: Optional[str] = None
    # For `select`: the option's value attribute as the browser reports it.
    selected_value: Optional[str] = None
    frames_before: Optional[dict[str, Any]] = None
    # The exact node acted on, as an object inside `frames_before`. Held by
    # identity rather than by id string so the recorder can probe locator
    # strategies against the same tree the node came from -- matching across
    # two separate snapshots would depend on snapshot ids being stable, which
    # is an assumption worth not making.
    acted_node: Optional[dict[str, Any]] = None
    duration_ms: float = 0.0

    def as_log(self) -> dict[str, Any]:
        return {
            "cycle": self.index,
            "url": self.url,
            "model_reasoning": self.reasoning,
            "action": self.tool_name,
            "action_input": self.tool_input,
            "status": self.status,
            "detail": self.detail,
            "element_key": self.element_key,
            "resolution": self.resolution,
            "extracted": self.extracted,
            "selected_value": self.selected_value,
            "duration_ms": round(self.duration_ms, 2),
            "observation": self.observation,
        }


@dataclass
class DiscoveryOutcome:
    status: str  # goal_reached | risk_blocked | stuck | max_steps | timeout | policy_violation | failure_limit
    run_id: str
    goal: str
    cycles: list[Cycle]
    outputs: dict[str, str] = field(default_factory=dict)
    summary: Optional[str] = None
    message: Optional[str] = None
    steps_attempted: int = 0
    duration_ms: float = 0.0
    provider: str = ""
    model: str = ""
    rate_limit_events: list[dict[str, Any]] = field(default_factory=list)
    human_interventions: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    cost_usd: Optional[float] = None

    # The cycle a risky step stopped on, if one did. Recorded but never
    # executed -- the flow up to it is real and worth keeping.
    blocked_cycle: Optional["Cycle"] = None

    @property
    def succeeded(self) -> bool:
        return self.status == "goal_reached"

    @property
    def recordable(self) -> bool:
        """Whether there is a flow worth emitting an artifact for.

        A risk block is not a failed run: everything up to the irreversible
        step worked, and the step itself is known -- it just was not
        performed. Refusing to emit anything would make the gate mean
        "irreversible capabilities cannot be recorded at all", which is a
        worse outcome than recording one that stops short and says so.
        """
        return self.status in ("goal_reached", "risk_blocked")


def _element_key(tool_input: dict[str, Any], action: str) -> str:
    """Stable, readable registry key derived from what the model targeted."""
    if action == "extract" and tool_input.get("output_name"):
        return f"{tool_input['output_name']}_source"
    name = (tool_input.get("name") or tool_input.get("role") or action).strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", name).strip("_") or action
    role = (tool_input.get("role") or "").strip().lower()
    suffix = {"textbox": "field", "button": "button", "link": "link", "combobox": "select"}.get(role, "")
    return f"{slug}_{suffix}" if suffix and not slug.endswith(suffix) else slug


def _element_from_tool_input(
    tool_input: dict[str, Any], action: str, default_frame: Optional[str] = None
) -> Element:
    """Build the single-rung element the executor needs to act *right now*.

    This is not the recorded chain. Discovery only needs something that
    resolves at this instant; `discovery.recorder` builds the real fallback
    chain afterwards by probing which strategies uniquely identify the same
    element. Conflating the two would bake whatever the model happened to say
    into the artifact as if it were a robustness decision.
    """
    frame = tool_input.get("frame") or default_frame
    role = tool_input.get("role")
    name = tool_input.get("name")
    row_contains = tool_input.get("row_contains")
    column_header = tool_input.get("column_header")

    if column_header and row_contains:
        rung = LocatorRung(
            strategy="cell_in_row",
            scope=Scope(role="row", contains=row_contains),
            column_header=column_header,
            confidence="high",
        )
    elif row_contains:
        rung = LocatorRung(
            strategy="role_name_scoped",
            role=role,
            name=name,
            scope=Scope(role="row", contains=row_contains),
            confidence="high",
        )
    else:
        rung = LocatorRung(strategy="role_name", role=role, name=name, confidence="high")

    return Element(
        description=f"{role or 'element'} {name or ''}".strip(),
        frame=frame,
        chain=[rung],
    )


class DiscoveryLoop:
    def __init__(
        self,
        goal: str,
        policy: Policy,
        target: Target,
        evidence_dir: Path,
        headless: bool = True,
        provider: str = DEFAULT_PROVIDER,
        model: Optional[str] = None,
        client: Optional[ModelClient] = None,
        profile=None,
        escalate: bool = False,
        operator=None,
        escalation_root: str | Path = "evidence/escalation",
        max_wall_clock_s: float = MAX_WALL_CLOCK_S,
    ):
        self.goal = goal
        self.policy = policy
        self.target = target
        # The client is the only provider-aware object in the loop; swapping
        # providers is a config change, not a code change.
        self.client = client or build_client(provider, model, on_retry=self._log_retry)
        self.provider = self.client.provider
        self.model = self.client.model
        self.rate_limit_events: list[dict[str, Any]] = []
        # Time spent waiting on the provider, excluded from the wall clock.
        self.backoff_s: float = 0.0
        # Token usage across the run, so a run reports what it cost rather
        # than leaving that to be inferred from the provider's dashboard.
        self.usage: dict[str, int] = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "calls": 0,
        }
        # Off by default, same reasoning as replay: a discovery run left
        # blocking on a human nobody is watching is worse than one that stops.
        self.escalate = escalate
        self.operator = operator
        self.escalation_root = escalation_root
        self.control = None
        self.human_interventions: list[dict[str, Any]] = []
        self.max_wall_clock_s = max_wall_clock_s
        self.run_id = f"disc_{uuid.uuid4().hex[:8]}"
        self.evidence_dir = Path(evidence_dir) / self.run_id
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.headless = headless

        self.artifact = ProvisionalArtifact(policy, target)
        # Discovery logs whole-page observations, so sensitive values arrive
        # as page *content* and pattern rules alone are not enough: they
        # catch an SSN by shape but not a name, address or account number.
        # Disk evidence therefore gets the seed scrubber, exactly as the a11y
        # diagnostic does. Model-facing text keeps the narrower pattern-only
        # scrubber -- see _scrub_for_model for why the two differ.
        self.profile = profile or profile_for(target)
        # Same vocabulary the recorder uses. Imported here rather than
        # duplicated so the two can never disagree about what "risky" means.
        from discovery.recorder import risk_rules_from_profile

        self.risk_rules = risk_rules_from_profile(self.profile)
        self.sink = RedactionSink(self.profile)
        self.model_scrubber = Scrubber()
        self.log_path = self.evidence_dir / "cycles.jsonl"
        self.cycles: list[Cycle] = []

    # -- evidence -----------------------------------------------------------

    def log(self, event: str, payload: dict[str, Any]) -> None:
        self.sink.append_jsonl(self.log_path, {"event": event, **payload})

    # -- perception ---------------------------------------------------------

    async def _perceive(self) -> dict[str, Optional[dict]]:
        snapshots = await snapshot_all_frames(self.page)
        return {s.frame_name: s.tree for s in snapshots}

    def _scrub_for_model(self, text: str) -> str:
        """What the model is allowed to see.

        Credential literals are removed because the requirement is absolute:
        credential values never go to the model. PII *shapes* (SSN, email,
        phone) are removed as well -- CoreServ renders a member's full SSN
        beside the balance a lookup goal legitimately reads, and shipping
        regulated data to a third party to accomplish a task that does not
        need it is exactly what the brief prohibits.

        Identifiers, names and balances are deliberately left intact: the
        model targets rows by member id and reads balances, so masking those
        would not protect anything the goal does not already require and
        would simply break the run.
        """
        return self.model_scrubber.scrub(text)

    async def _observe(self) -> tuple[str, str, dict[str, Optional[dict]]]:
        frames = await self._perceive()
        url = self.page.url
        if self.profile.content_frame is not None:
            for frame in self.page.frames:
                if frame.name == self.profile.content_frame:
                    url = frame.url or url

        blocks = []
        for name, tree in frames.items():
            compact = to_compact_text(filter_tree(tree))
            if not compact.strip():
                continue
            label = name or "(top)"
            blocks.append(f"--- FRAME {label} ---\n{compact}")

        observation = f"Current URL: {url}\n\n" + "\n\n".join(blocks)
        return self._scrub_for_model(observation), url, frames

    # -- act ----------------------------------------------------------------

    async def _act(self, cycle: Cycle, frames: dict[str, Optional[dict]]) -> str:
        """Execute one model-requested action through the shared executor."""
        action = cycle.tool_name
        tool_input = cycle.tool_input
        # None means the document. A frameless app has no frame to name, and
        # defaulting to a frame name it does not have would make every action
        # unresolvable in a way that reads like a missing control.
        frame = tool_input.get("frame") or self.profile.content_frame

        if action == "navigate":
            step = Step(id=f"d{cycle.index}", action="navigate", path=tool_input["path"], frame=frame)
            await self.executor.execute(step, {})
            return f"Navigated to {tool_input['path']} in frame {frame}."

        key = _element_key(tool_input, action)
        element = _element_from_tool_input(tool_input, action, self.profile.content_frame)
        self.artifact.elements[key] = element
        cycle.element_key = key

        # Resolve against the snapshot the model was shown, so the recorder
        # can later probe alternative strategies against that same tree. The
        # executor performs its own resolution for acting; this one exists
        # purely to pin the node for recording.
        pinned = resolver.resolve_element(key, element, frames, {})
        if pinned.resolved:
            cycle.acted_node = pinned.node

        # The risk classification the recorder would apply, applied now --
        # before the action rather than after it. Same rules, same profile, so
        # a step the artifact will call risky is a step discovery refuses to
        # perform unattended, rather than one it performs and then labels.
        step_kwargs: dict[str, Any] = {
            "id": f"d{cycle.index}",
            "action": action,
            "element": key,
            "frame": frame,
            "risk": self._classify_risk(action, tool_input, pinned),
        }
        if action in ("fill", "select"):
            step_kwargs["value"] = tool_input["value"]
        if action == "extract":
            step_kwargs["into"] = tool_input["output_name"]

        step = Step(**step_kwargs)
        outcome = await self.executor.execute(step, {})

        if outcome.resolution:
            cycle.resolution = outcome.resolution.as_dict()
        if action == "select":
            cycle.selected_value = outcome.selected_value
            return (
                f"Selected {outcome.selected_value or tool_input.get('value')!r} "
                f"in {tool_input.get('name')!r}."
            )
        if action == "extract":
            cycle.extracted = outcome.extracted
            return f"Extracted {tool_input['output_name']} = {outcome.extracted!r}"
        if action == "fill":
            return f"Filled {tool_input.get('name')!r} in frame {frame}."
        return f"Performed {action} on {tool_input.get('name')!r} in frame {frame}."

    def _classify_risk(self, action: str, tool_input: dict[str, Any], resolution) -> str:
        """Is this action irreversible, as far as the app profile can tell?

        Two signals, matching the recorder's: the control's name against the
        profile's commit verbs, and where activating it would send the page
        against the profile's commit paths. Both narrowed to submit-type
        controls, because only those commit.

        The path signal here is the pre-click one -- a form's action or a
        link's href -- rather than the landing URL the recorder sees
        afterwards. That is strictly narrower and the docstring on the
        destination below says how.
        """
        if action != "click":
            return "safe"
        name = ""
        role = ""
        if resolution is not None and resolution.resolved and resolution.node:
            name = (resolution.node.get("name") or "").strip()
            role = (resolution.node.get("role") or "").strip().lower()
        name = name or (tool_input.get("name") or "").strip()
        role = role or (tool_input.get("role") or "").strip().lower()

        # Where activating this control would send the page, from
        # perception. For a submit button that is its form's action; for a
        # link it is the href. This is the pre-click stand-in for the landing
        # URL the recorder matches after the fact -- NARROWER, deliberately:
        # it is where the request is addressed, not where the app ends up.
        # A server that redirects after posting, or a form whose action is
        # empty and posts to the current URL, both defeat it. It catches a
        # commit whose LABEL says nothing, which is the case the verb list
        # cannot reach, and it is the only such signal available before the
        # click has happened.
        props = (resolution.node.get("props") or {}) if (
            resolution is not None and resolution.resolved and resolution.node
        ) else {}
        destination = props.get("action") or props.get("url")

        matched = self.risk_rules.match(name)
        commits = self.risk_rules.commits(destination)

        # Only a submit-type control can commit. Navigation links share the
        # commit vocabulary -- MERIDIAN's member record has a link named
        # "Funds Transfer" that merely opens the form, and the first live
        # recording marked it risky. The distinction is the role: a <button>
        # or <input type=submit> is role "button"; an <a> is role "link".
        # The same narrowing applies to BOTH signals: MERIDIAN serves its
        # update form from the path it posts to, so the link opening that
        # form addresses a committing endpoint with a GET.
        if role != "button":
            if matched or commits:
                self.log("risk_classified", {
                    "control": name, "role": role, "decision": "safe",
                    "near_miss_verb": matched, "near_miss_path": commits,
                    "why": "addresses a committing endpoint but is not a submit control"
                           if commits else
                           "matched a commit verb but is not a submit-type control",
                })
            return "safe"

        if not matched and not commits:
            return "safe"
        self.log("risk_blocked_candidate", {
            "control": name, "matched_verb": matched, "matched_path": commits,
            "destination": destination,
        })
        return "risky"

    # -- authentication (never reaches the model) ---------------------------

    async def _authenticate(self) -> None:
        auth = self.target.auth
        if auth is None:
            return
        credentials = resolve_credentials_for(self.target)
        self.model_scrubber.register_secrets(credentials.values())
        self.sink.register_secrets(credentials.values())

        url = self.target.base_url.rstrip("/") + auth.path
        check_destination(self.artifact, url)
        await self.page.goto(url, wait_until="load")

        # Sign-on is targeted through the app profile's auth elements, by role
        # and accessible name, exactly as replay does it. Discovery has no
        # artifact to hold an element registry, so the profile's defaults are
        # registered on the provisional one -- which is what that stand-in
        # exists for.
        defaults = self.profile.auth_defaults
        if defaults is None or not defaults.submit:
            raise AuthConfigError("discovery", [], [])
        for key, element in defaults.elements.items():
            self.artifact.elements.setdefault(key, Element.model_validate(element))

        values = {**credentials, **auth.parameters}
        for field_name, key in defaults.fields.items():
            value = values.get(field_name)
            if value is None:
                continue
            resolution, locator = await self.executor.locate(key, {})
            role = (resolution.node.get("role") or "").strip().lower()
            if role == "combobox":
                await locator.select_option(value)
            elif role in ("checkbox", "radio"):
                await locator.check()
            else:
                await locator.fill(value)

        async with self.page.expect_navigation():
            _, submit = await self.executor.locate(defaults.submit, {})
            await submit.click()
        self.log("authenticated", {"mode": auth.mode, "env_vars": sorted(auth.credentials_ref.values())})

    # -- run ----------------------------------------------------------------

    def _record_usage(self, usage: dict[str, Any]) -> None:
        if not usage:
            return
        self.usage["calls"] += 1
        for key in (
            "input_tokens", "output_tokens",
            "cache_creation_input_tokens", "cache_read_input_tokens",
        ):
            self.usage[key] += int(usage.get(key) or 0)

    def _log_retry(self, event: dict[str, Any]) -> None:
        """Every provider retry lands in evidence. A run that spent four
        minutes in backoff is otherwise indistinguishable from a slow model."""
        self.rate_limit_events.append(event)
        self.backoff_s += float(event.get("sleep_s") or 0.0)
        self.log("rate_limited", event)

    async def run(self) -> DiscoveryOutcome:
        from playwright.async_api import async_playwright

        started = time.perf_counter()
        budget = self.max_wall_clock_s

        system = build_system_prompt(
            goal=self.goal,
            base_url=self.target.base_url,
            entry_path=self.target.entry_path,
            allowed_paths=self.policy.allowed_paths,
            allowed_actions=list(self.policy.allowed_actions),
            content_frame=self.profile.content_frame,
        )
        self.log(
            "run_started",
            {
                "run_id": self.run_id,
                "goal": self.goal,
                "provider": self.provider,
                "model": self.model,
                "policy": self.policy.model_dump(mode="json"),
            },
        )

        # Shaped by the profile: frame targeting only where frames exist.
        tools = build_tools(self.profile.content_frame)

        messages: list[Message] = []
        outputs: dict[str, str] = {}
        consecutive_failures = 0
        status = "max_steps"
        blocked_cycle = None
        summary = None
        message = None

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=self.headless)
            self.page = await browser.new_page()
            self.control = ControlledSession(page=self.page)
            self.executor = Executor(
                self.page, self.artifact, self._perceive, control=self.control
            )

            try:
                await self._authenticate()

                # The flow starts post-authentication, at the declared entry.
                # The flow starts at its declared entry. This used to
                # policy-check `entry` and then navigate to a hardcoded
                # "/home", so the declared entry_path was validated and
                # ignored -- and "/home" does not exist on every app.
                entry = self.target.base_url.rstrip("/") + self.target.entry_path
                check_destination(self.artifact, entry)
                await self.page.goto(entry, wait_until="load")

                for index in range(1, MAX_STEPS + 1):
                    working_s = (time.perf_counter() - started) - self.backoff_s
                    if working_s > budget:
                        status, message = (
                            "timeout",
                            f"wall clock limit of {budget}s reached "
                            f"(excluding {self.backoff_s:.0f}s of provider backoff)",
                        )
                        break

                    observation, url, frames = await self._observe()
                    cycle = Cycle(index=index, url=url, observation=observation, reasoning="")
                    cycle.frames_before = frames
                    cycle_started = time.perf_counter()

                    messages.append(Observation(text=observation))

                    response = self.client.complete(system, messages, tools)
                    self._record_usage(response.usage)

                    cycle.reasoning = response.text
                    tool_uses = response.calls

                    if not tool_uses:
                        consecutive_failures += 1
                        cycle.status = "failed"
                        cycle.detail = "model returned no tool call"
                        self.cycles.append(cycle)
                        self.log("cycle", cycle.as_log())
                        messages.append(
                            Observation(
                                text=(
                                    "You did not call a tool. Call exactly one tool: an action, "
                                    "goal_reached, or stuck."
                                )
                            )
                        )
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            status, message = "failure_limit", "model stopped issuing tool calls"
                            break
                        continue

                    block = tool_uses[0]
                    cycle.tool_name = block.name
                    cycle.tool_input = dict(block.arguments)
                    messages.append(
                        AssistantAction(
                            call_id=block.id,
                            name=block.name,
                            arguments=cycle.tool_input,
                            text=response.text,
                        )
                    )

                    if block.name in TERMINAL_TOOLS:
                        cycle.status = "terminal"
                        self.cycles.append(cycle)
                        self.log("cycle", cycle.as_log())
                        if block.name == "goal_reached":
                            status = "goal_reached"
                            summary = cycle.tool_input.get("summary")
                            break

                        # The model declaring itself stuck is the discovery-side
                        # trigger for human intervention: it knows it cannot
                        # proceed, which is exactly when a person is useful.
                        status = "stuck"
                        message = cycle.tool_input.get("reason")
                        if self.escalate and self.operator:
                            resumed = await self._escalate(cycle, message or "model reported stuck")
                            if resumed:
                                messages.append(
                                    Observation(
                                        text=(
                                            "A human operator took control of this same session, "
                                            "made changes, and handed control back. Re-read the "
                                            "snapshot below and continue toward the goal."
                                        )
                                    )
                                )
                                status = "max_steps"
                                consecutive_failures = 0
                                continue
                        break

                    if block.name not in ACTION_TOOLS:
                        result_text = f"Unknown action {block.name!r}."
                        cycle.status = "failed"
                        consecutive_failures += 1
                    else:
                        try:
                            result_text = await self._act(cycle, frames)
                            cycle.status = "ok"
                            consecutive_failures = 0
                            if block.name == "extract" and cycle.extracted is not None:
                                outputs[cycle.tool_input["output_name"]] = cycle.extracted
                        except RiskBlocked as exc:
                            # Not a failure the model can work around, and not
                            # one it should be invited to retry: the step is
                            # irreversible and policy says a person decides.
                            # The run stops here and the recorder emits what
                            # was learned up to and including this step.
                            cycle.status = "blocked"
                            cycle.detail = str(exc)
                            self.cycles.append(cycle)
                            self.log("cycle", cycle.as_log())
                            self.log("risk_blocked", {
                                "step": f"d{cycle.index}", "handling": exc.handling,
                                "detail": exc.detail,
                            })
                            status, message = "risk_blocked", str(exc)
                            blocked_cycle = cycle
                            break
                        except PolicyViolation as exc:
                            cycle.status = "failed"
                            cycle.detail = str(exc)
                            self.cycles.append(cycle)
                            self.log("cycle", cycle.as_log())
                            self.log("policy_violation", {"detail": exc.detail, "kind": exc.kind})
                            status, message = "policy_violation", str(exc)
                            break
                        except ElementUnresolvable as exc:
                            result_text = _unresolvable_advice(exc)
                            cycle.status = "failed"
                            cycle.detail = str(exc)
                            consecutive_failures += 1
                        except Exception as exc:
                            result_text = f"Action failed: {exc}"
                            cycle.status = "failed"
                            cycle.detail = str(exc)
                            consecutive_failures += 1

                    cycle.duration_ms = (time.perf_counter() - cycle_started) * 1000
                    self.cycles.append(cycle)
                    self.log("cycle", cycle.as_log())
                    await self._screenshot(index)

                    messages.append(
                        ActionResult(
                            call_id=block.id,
                            name=block.name,
                            text=self._scrub_for_model(result_text),
                            is_error=cycle.status == "failed",
                        )
                    )

                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        status, message = (
                            "failure_limit",
                            f"{MAX_CONSECUTIVE_FAILURES} consecutive failed actions",
                        )
                        break
            finally:
                await browser.close()

        outcome = DiscoveryOutcome(
            status=status,
            run_id=self.run_id,
            goal=self.goal,
            cycles=self.cycles,
            outputs=outputs,
            summary=summary,
            message=message,
            steps_attempted=len(self.cycles),
            duration_ms=(time.perf_counter() - started) * 1000,
            provider=self.provider,
            model=self.model,
            rate_limit_events=self.rate_limit_events,
            human_interventions=self.human_interventions,
            blocked_cycle=blocked_cycle,
            usage=dict(self.usage),
            cost_usd=estimate_cost_usd(self.model, self.usage),
        )
        self.log(
            "run_finished",
            {
                "status": outcome.status,
                "steps_attempted": outcome.steps_attempted,
                "outputs": outcome.outputs,
                "summary": outcome.summary,
                "message": outcome.message,
                "duration_ms": round(outcome.duration_ms, 2),
                "rate_limit_retries": len(outcome.rate_limit_events),
                "usage": outcome.usage,
                "cost_usd": outcome.cost_usd,
            },
        )
        return outcome

    async def _escalate(self, cycle: "Cycle", reason: str) -> bool:
        """Hand the live session to a human when the model gives up.

        Same control-transfer model replay uses, and the same session: the
        operator continues on the page the model got stuck on, so whatever
        context the run built up is still there.
        """
        from escalation.capture import HumanActionCapture, write_activity
        from escalation.request import (
            InterventionRequest,
            capture_state,
            escalation_dir,
            write_request,
        )

        directory = escalation_dir(self.run_id, self.escalation_root)
        url, screenshot, snapshot_path, snapshot = await capture_state(
            self.page, self._perceive, directory, sink=self.sink,
            content_frame=self.profile.content_frame,
        )
        request = InterventionRequest(
            run_id=self.run_id,
            source="discovery",
            goal=self.goal,
            reason=reason,
            classification="stuck",
            step_id=f"cycle_{cycle.index}",
            observed=reason,
            expected="the model to be able to continue toward the goal",
            url=url,
            screenshot_path=screenshot,
            snapshot_path=snapshot_path,
            snapshot=snapshot,
            completed_steps=[f"cycle_{c.index}" for c in self.cycles if c.status == "ok"],
        )
        request_path = write_request(request, self.escalation_root, sink=self.sink)
        self.log("intervention_raised", {"cycle": cycle.index, "request": str(request_path)})

        capture = HumanActionCapture(self.page, self._perceive, self.profile.content_frame)
        await capture.begin()
        self.control.hand_to_human(f"model reported stuck at cycle {cycle.index}: {reason}")
        try:
            decision = self.operator.handle(request)
        finally:
            self.control.take_back("operator returned control")

        activity = await capture.end(decision)
        write_activity(self.run_id, activity, self.control.as_dict(), self.escalation_root,
                       sink=self.sink)
        self.human_interventions.append(
            {
                "cycle": cycle.index,
                "decision": decision.decision.value,
                "operator": decision.operator,
                "notes": decision.notes,
                "url_changed": activity.url_changed,
            }
        )
        self.log("intervention_resolved", self.human_interventions[-1])
        if decision.resumed:
            self.control.assert_same_session()
        return decision.resumed

    async def _screenshot(self, index: int) -> None:
        try:
            await self.page.screenshot(
                path=str(self.evidence_dir / f"step_{index:02d}.png"), full_page=True
            )
        except Exception:
            pass


def _unresolvable_advice(exc: ElementUnresolvable) -> str:
    """Tell the model what actually went wrong, and what to do about it.

    "Target something that is actually present" is the right advice for a
    control that is not there and the wrong advice for one that is there
    several times -- and the two arrive as the same exception. A model told to
    look for something else will keep re-sending an ambiguous target, because
    from where it sits the target looks correct. Observed: a weaker model
    repeated the same nine-way-ambiguous row scope three times and hit the
    consecutive-failure limit with the answer on screen in front of it.
    """
    ambiguous = [a for a in exc.resolution.attempts if a.outcome == "ambiguous"]
    if ambiguous:
        worst = max(ambiguous, key=lambda a: a.match_count)
        return (
            f"That target is AMBIGUOUS, not missing: it matched {worst.match_count} "
            "elements, so it does not identify one. Narrow it. If you used "
            "row_contains, the text you gave appears in several rows -- a value that "
            "is a prefix of other values (an account id whose siblings add a suffix) "
            "does this. Add more text from the same row to make it unique, or use "
            "text from a different column of that row."
        )
    return (
        f"Could not find that element: {exc}. Re-read the snapshot and target "
        "something that is actually present."
    )


def resolve_credentials_for(target: Target) -> dict[str, str]:
    """Resolve the target's declared credentials, or raise AuthConfigError.

    Wrapped so discovery reuses replay's resolution (env var names only, never
    values in the artifact) without importing an artifact it does not have.
    """

    class _Shim:
        def __init__(self, target):
            self.target = target

            class _Cap:
                id = "discovery"

            self.capability = _Cap()

    return resolve_credentials(_Shim(target))
