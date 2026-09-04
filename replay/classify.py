"""Two-layer condition detection, run after every step.

The split is the answer to "where does error detection live":

  Layer 1, ENGINE UNIVERSALS -- session expiry, the 500 page, the
  maintenance interstitial, timeouts, unresolvable elements. These can occur
  on any step of any flow, so duplicating them into every artifact would be
  noise. The *logic* lives here; the strings and the recovery actions that
  vary between applications live in the app profile, because they are facts
  about a vendor product rather than about this engine.

  Layer 2, ARTIFACT-DECLARED OUTCOMES -- "No records match" meaning no such
  member. Only the flow knows that a not-found search is a legitimate
  answer rather than a fault, so it is declared in the artifact.

ORDER MATTERS, and the engine layer is checked first. Two concrete reasons:

1. A universal can *masquerade* as a flow outcome. When the session-expired
   fault fires, CoreServ bounces to the login page -- which contains none of
   the member's data. A flow-first classifier could observe "the results
   grid is absent" and report the business outcome "no such member", which
   is a wrong answer returned confidently to a caller who will act on it.
   Checking universals first means the bounce is correctly reported as a
   session failure.
2. Recoverable universals must be caught before anything is concluded at
   all. The maintenance interstitial overlays a page whose real content is
   still underneath; classifying against it would read the interstitial, not
   the app. It has to be dismissed and the step retried first.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from capability.schema import Artifact, Outcome

# Max attempts for the two recoverable universals. Deliberately small: a
# condition that survives two dismissals or two backoffs is not transient,
# and retrying indefinitely converts a fast failure into a hung run.
#
# The budget applies to SAFE steps only. A step marked risky is never retried
# by either recovery path -- see risky_not_retried_detection below.
MAX_RECOVERY_ATTEMPTS = 2


@dataclass
class Detection:
    """Something the classifier recognised on the page after a step."""

    name: str
    layer: str  # engine | artifact
    classification: str  # recoverable | hard_failure | business_outcome
    message: str
    recovery: Optional[str] = None
    escalation_eligible: bool = False

    def as_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "layer": self.layer,
            "classification": self.classification,
            "message": self.message,
        }
        if self.recovery:
            d["recovery"] = self.recovery
        if self.escalation_eligible:
            d["escalation_eligible"] = True
        return d


# ---------------------------------------------------------------------------
# Layer 1: engine universals
# ---------------------------------------------------------------------------

# Marker text is matched against the page's visible text. These are
# properties of the *application*, not of any one recorded flow -- which is
# why they live in an app profile rather than as constants here. They were
# constants here, and on a second app every one of them was wrong: CoreServ
# says "Your session has ended.", MERIDIAN says "YOUR SESSION HAS TIMED OUT",
# and the engine detected neither expiry nor interstitial on the app it was
# not written against, quietly.
MAINTENANCE_INTERSTITIAL = "maintenance_interstitial"
SESSION_EXPIRED = "session_expired"
SERVER_ERROR = "server_error"


def detect_engine_universals(page_text: str, profile) -> Optional[Detection]:
    """Check the universals in severity order, using the app's own markers.

    Recoverable conditions are checked BEFORE hard failures: the
    interstitial can overlay an otherwise healthy page, and dismissing it
    is cheaper and more accurate than failing a run that would have
    succeeded on retry.

    `recovery` on the returned Detection describes what the profile
    prescribes, so the message an operator reads matches what the engine will
    actually do rather than assuming every app dismisses a control.
    """
    marker = profile.matches("maintenance", page_text)
    if marker is not None:
        action = profile.recovery.get(MAINTENANCE_INTERSTITIAL)
        if action is not None and action.kind == "dismiss_control":
            how = f"dismiss via {action.control_name!r} and retry the step"
        elif action is not None and action.kind == "reload_step_url":
            how = "re-request the step's own URL and retry it"
        else:
            how = "back off and retry the step"
        return Detection(
            name=MAINTENANCE_INTERSTITIAL,
            layer="engine",
            classification="recoverable",
            message="A maintenance interstitial is covering the page.",
            recovery=how,
        )

    if profile.matches("session_expired", page_text) is not None:
        return Detection(
            name=SESSION_EXPIRED,
            layer="engine",
            classification="hard_failure",
            message=(
                "The application session has expired. Re-authentication mid-run is "
                "out of scope, so the run stops here."
            ),
            escalation_eligible=True,
        )

    if profile.matches("server_error", page_text) is not None:
        return Detection(
            name=SERVER_ERROR,
            layer="engine",
            classification="hard_failure",
            message="The application returned an unexpected server error.",
            escalation_eligible=True,
        )

    return None


def element_unresolvable_detection(element_key: str, detail: str) -> Detection:
    """An element that no rung could resolve.

    Escalation-eligible: the flow is sound but the surface no longer matches
    what was recorded, which is precisely the case a human should look at
    rather than a case for retrying.
    """
    return Detection(
        name="element_unresolvable",
        layer="engine",
        classification="hard_failure",
        message=f"Could not locate {element_key!r} on the page. {detail}",
        escalation_eligible=True,
    )


def risk_blocked_detection(step_id: str, handling: str, detail: str) -> Detection:
    """A risky step automation refused to perform.

    Escalation-eligible only under `require_confirmation`. Under `block` the
    answer is no, and offering a human the chance to say yes would make
    `block` mean `require_confirmation` with extra steps.

    Nothing was performed when this fires -- `check_risk` runs before the
    action -- so unlike every other hard failure here, the application is in
    exactly the state the previous step left it in.
    """
    return Detection(
        name="risky_action_blocked",
        layer="engine",
        classification="hard_failure",
        message=(
            f"Step {step_id} is marked risky and policy is {handling!r}. {detail}."
        ),
        escalation_eligible=(handling == "require_confirmation"),
    )


def risky_not_retried_detection(step_id: str, condition: str, detail: str) -> Detection:
    """A recoverable condition on a risky step, which is NOT retried.

    Retrying means re-running the step, and re-running a step means clicking
    the control again. On an irreversible action whose result we could not
    observe, a second click is how one transfer becomes two -- the first may
    well have posted and only its confirmation been slow. So the retry budget
    does not apply here at all: the run stops after one attempt and escalates
    with the page captured, because whether the side effect landed is a
    question about the account, and only a person looking at it can answer.
    """
    return Detection(
        name="risky_step_not_retried",
        layer="engine",
        classification="hard_failure",
        message=(
            f"Step {step_id} hit a recoverable condition ({condition}) after acting, "
            f"but it is marked risky and is never retried automatically: {detail} "
            "The action may or may not have taken effect -- verify against the "
            "record before re-running this capability."
        ),
        escalation_eligible=True,
    )


def timeout_detection(step_id: str, expected: str, observed: str) -> Detection:
    return Detection(
        name="checkpoint_timeout",
        layer="engine",
        classification="recoverable",
        message=f"Step {step_id} checkpoint not met: expected {expected}, observed {observed}.",
        recovery="backoff and retry the step",
    )


# ---------------------------------------------------------------------------
# Layer 2: artifact-declared outcomes
# ---------------------------------------------------------------------------


def detect_artifact_outcomes(
    artifact: Artifact,
    step_outcome_names: list[str],
    page_text: str,
    url: str,
    params: Optional[dict[str, Any]] = None,
) -> Optional[Detection]:
    """Check the outcomes this step declared it might produce.

    Only the names a step lists are considered, not every outcome in the
    artifact: "no records match" is a meaningful answer after the search
    step and would be a non sequitur after the extract step.
    """
    for name in step_outcome_names:
        outcome: Optional[Outcome] = artifact.outcome_map.get(name)
        if outcome is None:
            continue
        if _detect_matches(outcome, page_text, url, params or {}):
            return Detection(
                name=outcome.name,
                layer="artifact",
                classification=outcome.classification,
                message=outcome.message,
            )
    return None


def _detect_matches(
    outcome: Outcome, page_text: str, url: str, params: dict[str, Any]
) -> bool:
    import re

    from replay import resolver

    condition = outcome.detect
    if condition.type == "text_present":
        # An outcome may legitimately be keyed on the record being looked at
        # ("no shares for member 100234"), so its detect text carries caller
        # data like any other templated field.
        return resolver.substitute(condition.text, params) in page_text
    if condition.type == "url_matches":
        return bool(re.search(resolver.substitute_regex(condition.pattern, params), url))
    if condition.type == "any_of":
        return any(
            _detect_matches(outcome.model_copy(update={"detect": c}), page_text, url, params)
            for c in condition.conditions or []
        )
    if condition.type == "all_of":
        return all(
            _detect_matches(outcome.model_copy(update={"detect": c}), page_text, url, params)
            for c in condition.conditions or []
        )
    # element_present detection is deliberately unsupported here: outcomes
    # are recognised from page content, and an element-based detect would
    # need the resolver, coupling classification to locator resolution.
    return False


def detect_profile_outcomes(profile, page_text: str) -> Optional[Detection]:
    """Answers the application gives that any flow may receive.

    Without this a capability recorded from a happy-path run has `outcomes:
    []` -- discovery never saw a not-found -- so "no such member" arrived as
    a checkpoint failure and a 502. That is the exact mistake the brief calls
    the most common design error: conflating a legitimate answer with a
    crash. The recorder cannot invent these from a successful run, and the
    app already knows them.
    """
    for outcome in getattr(profile, "business_outcomes", []) or []:
        if outcome.text in page_text:
            return Detection(
                name=outcome.name,
                layer="profile",
                classification=outcome.classification,
                message=outcome.message,
            )
    return None


def classify(
    artifact: Artifact,
    step_outcome_names: list[str],
    page_text: str,
    url: str,
    profile,
    params: Optional[dict[str, Any]] = None,
) -> Optional[Detection]:
    """Full two-layer classification for one step.

    Engine universals first -- see the module docstring for why the order is
    load-bearing rather than arbitrary.
    """
    universal = detect_engine_universals(page_text, profile)
    if universal is not None:
        return universal
    declared = detect_artifact_outcomes(artifact, step_outcome_names, page_text, url, params)
    if declared is not None:
        return declared
    # App-level outcomes last, so a capability can always be more precise
    # about its own flow than the application is about itself.
    return detect_profile_outcomes(profile, page_text)
