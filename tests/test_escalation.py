"""Tests for human-in-the-loop escalation and handoff.

The load-bearing claims are: automation is provably locked out while a human
holds control, the human gets the *same* live session rather than a fresh
one, the request carries enough context to act on cold, and resuming
continues from where the run stopped instead of restarting it.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from capability.loader import load_artifact
from escalation.capture import HumanActionCapture, HumanActivity, write_activity
from escalation.operator import Decision, OperatorDecision, ScriptedOperator
from escalation.request import InterventionRequest, write_request
from escalation.session import (
    Control,
    ControlledSession,
    ControlViolation,
    InvalidTransition,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_PATH = REPO_ROOT / "capabilities" / "member_savings_balance" / "1.0.0.json"
BASE_URL = os.environ.get("CORESERV_URL", "http://localhost:8800")


class FakePage:
    """Stands in for a Playwright page. Identity is what matters here."""

    def __init__(self, url="http://localhost:8800/search"):
        self.url = url
        self.context = object()
        self.frames = []


@pytest.fixture
def session():
    return ControlledSession(page=FakePage())


# ---------------------------------------------------------------------------
# Control transitions
# ---------------------------------------------------------------------------


def test_automation_holds_control_by_default(session):
    assert session.owner is Control.AUTOMATION
    session.assert_automation_may_act()


def test_handing_to_human_locks_automation_out(session):
    session.hand_to_human("stuck at s4")
    assert session.owner is Control.HUMAN
    with pytest.raises(ControlViolation) as exc:
        session.assert_automation_may_act("click")
    assert "paused" in str(exc.value).lower()


def test_control_returns_to_automation_after_handoff(session):
    session.hand_to_human("stuck")
    session.take_back("operator done")
    assert session.owner is Control.AUTOMATION
    session.assert_automation_may_act()


def test_released_session_can_never_be_reacquired(session):
    session.release("run finished")
    assert session.owner is Control.RELEASED
    with pytest.raises(InvalidTransition):
        session.hand_to_human("too late")
    with pytest.raises(InvalidTransition):
        session.take_back("too late")
    with pytest.raises(ControlViolation):
        session.assert_automation_may_act()


def test_every_transition_is_logged_with_time_and_reason(session):
    session.hand_to_human("checkpoint failed at s4")
    session.take_back("operator fixed it")
    session.release("done")

    moves = [(t.from_owner, t.to_owner) for t in session.transitions]
    assert moves == [
        ("automation", "human"),
        ("human", "automation"),
        ("automation", "released"),
    ]
    for transition in session.transitions:
        assert transition.reason
        assert transition.at  # ISO timestamp
    assert "checkpoint failed at s4" in session.transitions[0].reason


def test_a_swapped_context_is_rejected(session):
    """A handoff that quietly opened a new browser would give the operator a
    different session from the one that got stuck."""
    session.page = FakePage()  # different object
    with pytest.raises(ControlViolation) as exc:
        session.hand_to_human("stuck")
    assert "same live session" in str(exc.value)


def test_context_identity_is_unchanged_across_a_handoff(session):
    before = session.context_identity
    page_before = session.page

    session.hand_to_human("stuck")
    session.take_back("resumed")

    assert session.context_identity == before
    assert session.page is page_before
    session.assert_same_session()


# ---------------------------------------------------------------------------
# Executor honours ownership
# ---------------------------------------------------------------------------


def test_executor_refuses_to_act_while_a_human_holds_control(session):
    """The gate lives in the executor, so a stray retry loop cannot race the
    operator no matter which caller triggered it."""
    from capability.schema import Step
    from replay.executor import Executor

    artifact = load_artifact(BASE_PATH)
    executor = Executor(session.page, artifact, lambda: {}, control=session)
    session.hand_to_human("stuck")

    step = Step(id="s2", action="fill", element="member_ref_field", value="10001")
    with pytest.raises(ControlViolation):
        asyncio.run(executor.execute(step, {"member_ref": "10001"}))


def test_executor_acts_normally_when_no_control_is_configured():
    """Escalation off means no ownership object at all -- the gate must not
    become a required dependency."""
    from replay.executor import Executor

    artifact = load_artifact(BASE_PATH)
    executor = Executor(FakePage(), artifact, lambda: {})
    assert executor.control is None


# ---------------------------------------------------------------------------
# Intervention request
# ---------------------------------------------------------------------------


@pytest.fixture
def request_obj():
    return InterventionRequest(
        run_id="run_test01",
        source="replay",
        goal="Replay member_savings_balance@1.0.0",
        reason="Step s4 checkpoint not met",
        classification="hard_failure",
        capability_id="member_savings_balance",
        capability_version="1.0.0",
        step_id="s4",
        expected="element 'member_detail_heading' present",
        observed="not found (after 5000ms)",
        url=f"{BASE_URL}/search/results?identifier=10001",
        screenshot_path="evidence/escalation/run_test01/stuck.png",
        snapshot_path="evidence/escalation/run_test01/stuck_snapshot.txt",
        snapshot='cell "Search Results"\nlink "View"',
        inputs_redacted={"member_ref": "***01"},
        completed_steps=["s1", "s2", "s3"],
    )


def test_request_carries_every_piece_of_required_context(request_obj):
    """An operator who did not watch the run has to be able to act on this."""
    payload = request_obj.as_dict()

    assert payload["capability"]["id"] == "member_savings_balance"
    assert payload["capability"]["version"] == "1.0.0"
    assert payload["run_id"] == "run_test01"
    assert payload["goal"]
    assert payload["stopped"]["step_id"] == "s4"
    assert payload["stopped"]["classification"] == "hard_failure"
    # Why it stopped, in the same expected/observed form the result uses.
    assert payload["stopped"]["expected"]
    assert payload["stopped"]["observed"]
    assert payload["state"]["url"]
    assert payload["state"]["screenshot"]
    assert payload["state"]["accessibility_snapshot"]
    assert payload["completed_steps"] == ["s1", "s2", "s3"]
    assert payload["raised_at"]


def test_request_is_written_through_the_seed_scrubber(tmp_path):
    """An intervention request is a whole-page capture by construction, so
    pattern-only scrubbing would miss names and account numbers."""
    from coreserv.data import MEMBERS

    member = MEMBERS[0]
    leaky = InterventionRequest(
        run_id="run_leak",
        source="replay",
        goal="g",
        reason="r",
        classification="hard_failure",
        snapshot=(
            f'cell "{member["ssn"]}" cell "{member["first_name"]} {member["last_name"]}" '
            f'cell "{member["address"]}" cell "{member["accounts"][0]["account_number"]}"'
        ),
    )
    path = write_request(leaky, tmp_path)
    written = path.read_text(encoding="utf-8")

    assert member["ssn"] not in written
    assert f'{member["first_name"]} {member["last_name"]}' not in written
    assert member["address"] not in written
    assert member["accounts"][0]["account_number"] not in written


def test_request_renders_for_a_human(request_obj):
    text = "\n".join(request_obj.summary_lines())
    assert "INTERVENTION REQUIRED" in text
    assert "s4" in text and "member_savings_balance" in text
    assert "expected" in text and "observed" in text


# ---------------------------------------------------------------------------
# Human activity capture
# ---------------------------------------------------------------------------


def test_capture_diffs_url_and_controls_across_the_handoff():
    class MovingPage(FakePage):
        pass

    page = MovingPage("http://localhost:8800/search")
    states = [
        {"content": {"role": "document", "name": "", "children": [
            {"role": "button", "name": "Submit", "children": []}]}},
        {"content": {"role": "document", "name": "", "children": [
            {"role": "link", "name": "Open Sub-Account", "children": []}]}},
    ]

    async def perceive():
        return states.pop(0) if states else {}

    capture = HumanActionCapture(page, perceive)
    asyncio.run(capture.begin())
    page.url = "http://localhost:8800/member/10001"  # human navigated
    activity = asyncio.run(capture.end(OperatorDecision(Decision.RESUME, notes="opened record")))

    assert activity.url_changed
    assert any("Open Sub-Account" in c for c in activity.controls_appeared)
    assert any("Submit" in c for c in activity.controls_disappeared)
    assert activity.notes == "opened record"
    assert activity.decision == "resume"


def test_activity_states_its_own_capture_scope():
    """Honest about capturing effects, not keystrokes."""
    activity = HumanActivity(started_at="t0")
    assert "effect-level" in activity.as_dict()["capture_scope"]


def test_handoff_record_is_written_scrubbed(tmp_path, session):
    from coreserv.data import MEMBERS

    activity = HumanActivity(started_at="t0", notes=f'looked at {MEMBERS[0]["ssn"]}')
    session.hand_to_human("stuck")
    session.take_back("done")
    path = write_activity("run_x", activity, session.as_dict(), tmp_path)
    written = path.read_text(encoding="utf-8")

    assert MEMBERS[0]["ssn"] not in written
    payload = json.loads(written)
    assert payload["control"]["transitions"][0]["to"] == "human"


# ---------------------------------------------------------------------------
# Operator surface
# ---------------------------------------------------------------------------


def test_scripted_operator_runs_work_while_holding_control(request_obj):
    """The callback stands in for the manual steps a person would take."""
    performed = []
    operator = ScriptedOperator(
        decisions=[OperatorDecision(Decision.RESUME, notes="fixed it")],
        on_control=lambda req: performed.append(req.step_id),
    )
    decision = operator.handle(request_obj)

    assert performed == ["s4"]
    assert decision.resumed
    assert operator.seen[0] is request_obj


def test_scripted_operator_aborts_when_out_of_decisions(request_obj):
    decision = ScriptedOperator().handle(request_obj)
    assert not decision.resumed
    assert decision.decision is Decision.ABORT


# ---------------------------------------------------------------------------
# Live handoff through the replay engine
# ---------------------------------------------------------------------------


def coreserv_up() -> bool:
    import httpx

    try:
        return httpx.get(f"{BASE_URL}/_faults", timeout=2).status_code == 200
    except Exception:
        return False


live = pytest.mark.skipif(not coreserv_up(), reason=f"CoreServ not running at {BASE_URL}")


@pytest.fixture
def faults():
    import httpx

    httpx.post(f"{BASE_URL}/_faults/reset", timeout=5)

    def set_fault(name: str, enabled: bool = True):
        httpx.post(f"{BASE_URL}/_faults", json={"fault": name, "enabled": enabled}, timeout=5)

    yield set_fault
    httpx.post(f"{BASE_URL}/_faults/reset", timeout=5)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("CORESERV_USERNAME", "testoperator")
    monkeypatch.setenv("CORESERV_PASSWORD", "testpassword")


@live
def test_escalation_is_off_by_default_so_replay_stays_unattended(faults, creds, tmp_path):
    """A capability invoked in production has nobody at a terminal."""
    from replay.engine import ReplayEngine

    faults("session_expired")
    engine = ReplayEngine(load_artifact(BASE_PATH), evidence_root=tmp_path)
    result = asyncio.run(engine.run({"member_ref": "10001"}))

    assert result.classification == "hard_failure"
    assert result.escalation_eligible
    assert result.human_interventions == []


@live
def test_stuck_replay_raises_an_intervention_and_resumes_on_the_same_session(
    faults, creds, tmp_path
):
    """The whole loop: detect, route, human drives the same session, resume
    from the failed step rather than the start."""
    import httpx
    from replay.engine import ReplayEngine

    faults("maintenance_interstitial")

    seen_context: dict[str, int] = {}

    def operator_work(request):
        # Standing in for a person: clear the condition that blocked the run.
        # Deliberately done through the app's own control plane rather than
        # by driving the page, so the fix is unambiguous in the assertion.
        httpx.post(
            f"{BASE_URL}/_faults",
            json={"fault": "maintenance_interstitial", "enabled": False},
            timeout=5,
        )
        seen_context["during"] = engine.control.context_identity
        assert engine.control.owner is Control.HUMAN

    operator = ScriptedOperator(
        decisions=[OperatorDecision(Decision.RESUME, notes="dismissed maintenance", operator="tester")],
        on_control=operator_work,
    )

    engine = ReplayEngine(
        load_artifact(BASE_PATH),
        evidence_root=tmp_path,
        escalate=True,
        operator=operator,
        escalation_root=tmp_path / "escalation",
    )
    # Force escalation early: recovery would otherwise clear the interstitial
    # on its own, so the retry budget is removed to reach the escalation path.
    import replay.classify as classify_module

    original = classify_module.MAX_RECOVERY_ATTEMPTS
    classify_module.MAX_RECOVERY_ATTEMPTS = 0
    try:
        result = asyncio.run(engine.run({"member_ref": "10001"}))
    finally:
        classify_module.MAX_RECOVERY_ATTEMPTS = original

    assert operator.seen, "operator was never called"
    request = operator.seen[0]
    assert request.capability_id == "member_savings_balance"
    assert request.step_id
    assert request.url and request.snapshot

    # Same live session throughout -- never a fresh context.
    assert seen_context["during"] == engine.control.context_identity
    engine.control.assert_same_session()

    moves = [(t.from_owner, t.to_owner) for t in engine.control.transitions]
    assert ("automation", "human") in moves
    assert ("human", "automation") in moves

    assert result.human_interventions
    assert result.human_interventions[0]["decision"] == "resume"
    assert Path(result.evidence["intervention_request"]).exists()
    assert Path(result.evidence["handoff"]).exists()


@live
def test_resume_continues_from_the_failed_step_not_from_the_start(faults, creds, tmp_path):
    """Restarting would repeat completed steps, repeating their side effects
    and potentially undoing what the operator just did."""
    import httpx
    from replay.engine import ReplayEngine

    faults("maintenance_interstitial")

    def operator_work(request):
        httpx.post(
            f"{BASE_URL}/_faults",
            json={"fault": "maintenance_interstitial", "enabled": False},
            timeout=5,
        )

    # Several resumes on purpose. Clearing the fault flag does not repaint a
    # page that is already rendered, so the condition persists until the flow
    # navigates again -- meaning the run escalates more than once. That is the
    # realistic shape of a partial fix, and the property under test survives
    # it: every escalation resumes where it stopped, so no step is ever
    # executed twice.
    operator = ScriptedOperator(
        decisions=[
            OperatorDecision(Decision.RESUME, notes="cleared", operator="tester")
            for _ in range(6)
        ],
        on_control=operator_work,
    )
    engine = ReplayEngine(
        load_artifact(BASE_PATH),
        evidence_root=tmp_path,
        escalate=True,
        operator=operator,
        escalation_root=tmp_path / "escalation",
    )

    import replay.classify as classify_module

    original = classify_module.MAX_RECOVERY_ATTEMPTS
    classify_module.MAX_RECOVERY_ATTEMPTS = 0
    try:
        result = asyncio.run(engine.run({"member_ref": "10001"}))
    finally:
        classify_module.MAX_RECOVERY_ATTEMPTS = original

    assert result.classification == "success", result.message
    assert result.outputs["savings_balance"] == "8320.10"

    # Each step appears exactly once: the run continued, it did not restart.
    step_ids = [t.step_id for t in result.trace]
    assert len(step_ids) == len(set(step_ids)), f"a step was re-run: {step_ids}"
    assert step_ids == [s.id for s in load_artifact(BASE_PATH).steps]


@live
def test_operator_abort_ends_the_run_as_a_failure(faults, creds, tmp_path):
    from replay.engine import ReplayEngine

    faults("session_expired")
    operator = ScriptedOperator(
        decisions=[OperatorDecision(Decision.ABORT, notes="cannot fix", operator="tester")]
    )
    engine = ReplayEngine(
        load_artifact(BASE_PATH),
        evidence_root=tmp_path,
        escalate=True,
        operator=operator,
        escalation_root=tmp_path / "escalation",
    )
    result = asyncio.run(engine.run({"member_ref": "10001"}))

    assert result.classification == "hard_failure"
    assert result.human_interventions[0]["decision"] == "abort"
    assert result.outputs == {}


@live
def test_control_is_released_when_the_run_ends(faults, creds, tmp_path):
    from replay.engine import ReplayEngine

    engine = ReplayEngine(load_artifact(BASE_PATH), evidence_root=tmp_path)
    result = asyncio.run(engine.run({"member_ref": "10001"}))

    assert result.classification == "success"
    assert result.control["owner"] == "released"
