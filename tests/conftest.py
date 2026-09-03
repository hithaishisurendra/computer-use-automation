"""Shared fakes for exercising the replay engine without a browser.

The engine's control flow -- when a step is retried, when it escalates, what
it reports -- is the thing under test in several files, and it is a property
of the engine rather than of Chromium. So these fakes replace the page and
the action execution while keeping the *real* guards in the loop:
`check_action` and `check_risk` are called for real, because they are exactly
what the risky-step tests are about. What is faked is the click, not the
decision about whether to click.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest

from capability.profile import AppProfile
from capability.schema import Artifact
from replay.executor import ActionOutcome, check_action, check_destination, check_risk

# A profile for engine control-flow tests. Real enough to exercise the code
# paths that consult one -- markers, a recovery action, a frame model -- and
# named so it cannot be confused with a shipped profile.
TEST_PROFILE = AppProfile(
    name="test",
    content_frame=None,
    error_markers={
        "session_expired": ["Your session has ended."],
        "server_error": ["An unexpected error occurred."],
        "maintenance": ["System Maintenance"],
    },
    recovery={
        "maintenance_interstitial": {
            "kind": "dismiss_control", "control_role": "button", "control_name": "Continue",
        },
        "checkpoint_timeout": {"kind": "backoff"},
    },
    version_pattern=None,
    redaction={"literals": ["Ada Lovelace"]},
)


class FakePage:
    """Enough page for the engine, the escalation capture and the session."""

    def __init__(self, url: str = "https://example.test/start"):
        self.url = url
        self.context = object()
        self.frames: list[Any] = []

    async def screenshot(self, **kwargs) -> None:
        return None


class RecordingExecutor:
    """Runs the real policy and risk gates, then records instead of clicking.

    `calls` is the point: a retry is only observable as the same step being
    executed twice, so counting executions is how "risky steps are not
    retried" becomes a test rather than an assertion about code shape.
    """

    def __init__(self, artifact: Artifact, control=None):
        self.artifact = artifact
        self.control = control
        self.calls: list[str] = []

    async def execute(self, step, params: dict[str, Any]) -> ActionOutcome:
        if self.control is not None:
            self.control.assert_automation_may_act(f"run step {step.id}")
        check_action(self.artifact, step.action, step.id)
        if step.action == "navigate":
            check_destination(
                self.artifact,
                self.artifact.target.base_url.rstrip("/") + (step.path or ""),
                step.id,
            )
        note = check_risk(self.artifact, step)  # raises RiskBlocked for risky steps
        self.calls.append(step.id)
        return ActionOutcome(step.id, step.action, detail=note)


class Observations:
    """The page state the engine perceives, as a value the test can change.

    Held in an object rather than a closure so a test can move the world on
    mid-run -- which is what an operator performing a step manually looks
    like from the engine's side.
    """

    def __init__(self, page_text: str = "", url: str = "https://example.test/start",
                 frames: Optional[dict] = None):
        self.page_text = page_text
        self.url = url
        self.frames = frames if frames is not None else {"": {"role": "document", "name": "", "children": []}}

    async def capture(self):
        return self.frames, self.page_text, self.url

    async def perceive(self):
        return self.frames


def build_engine(artifact: Artifact, tmp_path, *, observations=None, escalate=False,
                 operator=None, profile=None):
    """A ReplayEngine wired to fakes, with its real step logic intact."""
    from escalation.session import ControlledSession
    from replay.engine import ReplayEngine

    engine = ReplayEngine(
        artifact,
        evidence_root=tmp_path / "replay",
        escalate=escalate,
        operator=operator,
        escalation_root=tmp_path / "escalation",
        profile=profile or TEST_PROFILE,
    )
    obs = observations or Observations()
    engine.page = FakePage(obs.url)
    engine.control = ControlledSession(page=engine.page)
    engine.executor = RecordingExecutor(artifact, control=engine.control)
    engine._capture = obs.capture
    engine._perceive = obs.perceive
    engine._credentials = {}
    engine.observations = obs
    return engine


ELEMENT = {
    "description": "a control",
    "frame": "",
    "chain": [{"strategy": "role_name", "role": "button", "name": "Post Transfer",
               "confidence": "high"}],
}


def make_artifact(steps: list[dict], *, risky_handling: str = "require_confirmation",
                  outcomes: Optional[list[dict]] = None,
                  allowed_paths: Optional[list[str]] = None) -> Artifact:
    """A minimal valid artifact with no auth, for engine control-flow tests.

    Auth is omitted deliberately: these tests are about what happens to a
    step, and a sign-on the engine would have to fake first is noise.
    """
    return Artifact.model_validate({
        "schema_version": "1.0",
        "capability": {"id": "t", "version": "1.0.0", "name": "t", "description": "t"},
        "target": {
            "surface": "web", "app": "t", "app_version": "1.0.0", "tenant": "t",
            "base_url": "https://example.test", "entry_path": "/start",
        },
        "inputs": [], "outputs": [],
        "elements": {"post_button": ELEMENT},
        "steps": steps,
        "outcomes": outcomes or [],
        "policy": {
            "allowed_origins": ["https://example.test"],
            "allowed_paths": allowed_paths or ["/start", "/done"],
            "allowed_actions": ["navigate", "click", "fill", "extract"],
            "risky_action_handling": risky_handling,
            "max_steps": 25, "timeout_ms": 120000,
        },
        "provenance": {
            "source": "hand_written", "discovered_at": "2026-09-03T00:00:00Z",
            "goal": "t", "steps_attempted": 1, "steps_recorded": 1,
        },
    })


@pytest.fixture
def observations():
    return Observations()
