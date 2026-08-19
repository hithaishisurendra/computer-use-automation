"""Two-layer condition detection, run after every step.

The split is the answer to "where does error detection live":

  Layer 1, ENGINE UNIVERSALS -- session expiry, the 500 page, the
  maintenance interstitial, timeouts, unresolvable elements. These can occur
  on any step of any flow, so duplicating them into every artifact would be
  noise. They live here.

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
# properties of the CoreServ *product*, not of any one recorded flow, which
# is exactly why they belong to the engine rather than to an artifact.
SESSION_EXPIRED_MARKER = "Your session has ended."
SERVER_ERROR_MARKER = "An unexpected error occurred."
INTERSTITIAL_MARKER = "System Maintenance"
INTERSTITIAL_DISMISS = "Continue"


def detect_engine_universals(page_text: str) -> Optional[Detection]:
    """Check the universals in severity order.

    Recoverable conditions are checked BEFORE hard failures: the
    interstitial can overlay an otherwise healthy page, and dismissing it
    is cheaper and more accurate than failing a run that would have
    succeeded on retry.
    """
    if INTERSTITIAL_MARKER in page_text:
        return Detection(
            name="maintenance_interstitial",
            layer="engine",
            classification="recoverable",
            message="A maintenance interstitial is covering the page.",
            recovery=f"dismiss via {INTERSTITIAL_DISMISS!r} and retry the step",
        )

    if SESSION_EXPIRED_MARKER in page_text:
        return Detection(
            name="session_expired",
            layer="engine",
            classification="hard_failure",
            message=(
                "The application session has expired. Re-authentication mid-run is "
                "out of scope, so the run stops here."
            ),
            escalation_eligible=True,
        )

    if SERVER_ERROR_MARKER in page_text:
        return Detection(
            name="server_error",
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
        if _detect_matches(outcome, page_text, url):
            return Detection(
                name=outcome.name,
                layer="artifact",
                classification=outcome.classification,
                message=outcome.message,
            )
    return None


def _detect_matches(outcome: Outcome, page_text: str, url: str) -> bool:
    import re

    condition = outcome.detect
    if condition.type == "text_present":
        return condition.text in page_text
    if condition.type == "url_matches":
        return bool(re.search(condition.pattern, url))
    if condition.type == "any_of":
        return any(
            _detect_matches(outcome.model_copy(update={"detect": c}), page_text, url)
            for c in condition.conditions or []
        )
    if condition.type == "all_of":
        return all(
            _detect_matches(outcome.model_copy(update={"detect": c}), page_text, url)
            for c in condition.conditions or []
        )
    # element_present detection is deliberately unsupported here: outcomes
    # are recognised from page content, and an element-based detect would
    # need the resolver, coupling classification to locator resolution.
    return False


def classify(
    artifact: Artifact,
    step_outcome_names: list[str],
    page_text: str,
    url: str,
) -> Optional[Detection]:
    """Full two-layer classification for one step.

    Engine universals first -- see the module docstring for why the order is
    load-bearing rather than arbitrary.
    """
    universal = detect_engine_universals(page_text)
    if universal is not None:
        return universal
    return detect_artifact_outcomes(artifact, step_outcome_names, page_text, url)
