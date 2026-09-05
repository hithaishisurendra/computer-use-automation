"""Tests for the deterministic replay engine.

Split into two groups:

- Unit tests over the resolver, classifier and policy layer, which run
  against synthetic accessibility trees with no browser at all. Those layers
  are pure functions over a tree, and keeping them testable without Chromium
  is a large part of why the browser lives only in the executor.
- Live tests that drive a real CoreServ instance. They skip (rather than
  fail) when the app is not running, so the suite stays useful offline, and
  they reset fault state around themselves so one test's fault cannot leak
  into another's result.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from capability.loader import load_artifact, load_resolved
from capability.profile import load_profile
from capability.schema import Artifact, Element, LocatorRung, Scope
from replay import classify, resolver
from tests import scope
from escalation.operator import Decision, OperatorDecision
from replay.executor import PolicyViolation, check_action, check_destination

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"
BASE_PATH = CAPABILITIES / "member_savings_balance" / "1.0.0.json"
BASE_URL = os.environ.get("CORESERV_URL", "http://localhost:8800")


@pytest.fixture
def artifact() -> Artifact:
    return load_artifact(BASE_PATH)


@pytest.fixture
def profile():
    """The app profile the committed artifact is recorded against.

    Error markers and recovery actions moved out of the classifier into this
    file, so a classification test now has to say which application it is
    classifying for -- which is the point of the change.
    """
    return load_profile("coreserv")


# ---------------------------------------------------------------------------
# No model client anywhere in replay/
# ---------------------------------------------------------------------------

MODEL_CLIENT_MODULES = {
    "anthropic",
    "openai",
    "cohere",
    "google.generativeai",
    "mistralai",
    "ollama",
    "litellm",
    "langchain",
    "langchain_openai",
    "transformers",
}


def _imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_no_model_client_is_imported_by_replay():
    """Replay is deterministic by construction: the LLM is not in the loop,
    and this asserts that structurally rather than by convention."""
    offenders = {}
    for source in (s for p in scope.packages() if p != "discovery"
                   for s in scope.sources(p)):
        for name in _imported_names(source):
            root = name.split(".")[0]
            if root in MODEL_CLIENT_MODULES or name in MODEL_CLIENT_MODULES:
                offenders.setdefault(source.name, []).append(name)
    assert not offenders, (
        "a model client is imported outside discovery/, so the LLM is in a "
        f"decision path it should not be in: {offenders}")


def test_replay_package_imports_with_model_clients_blocked():
    """Stronger than reading imports: the whole package loads and runs a
    resolution with every model client made unimportable."""
    program = """
import sys
for name in ("anthropic", "openai", "cohere", "litellm", "langchain", "transformers"):
    sys.modules[name] = None
sys.path.insert(0, %r)
import replay.engine, replay.resolver, replay.executor, replay.checkpoints, replay.classify
print("ok")
""" % str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


# ---------------------------------------------------------------------------
# Resolver, against synthetic trees (no browser)
# ---------------------------------------------------------------------------


def node(role, name="", children=None, ref=None, **extra):
    return {
        "role": role,
        "name": name,
        "children": children or [],
        "ref": ref or f"e{abs(hash((role, name))) % 10000}",
        **extra,
    }


def results_grid_tree():
    """A results grid shaped like CoreServ's: three rows whose View links are
    identically named, wrapped in the outer table/row nesting that makes
    naive scoping ambiguous."""
    def row(member_id, name, status):
        return node("row", "", [
            node("cell", member_id),
            node("cell", name),
            node("cell", status),
            node("cell", "", [node("link", "View", ref=f"view_{member_id}")]),
        ])

    grid = node("table", "", [
        node("row", "", [
            node("columnheader", "Member ID"),
            node("columnheader", "Name"),
            node("columnheader", "Status"),
            node("columnheader", ""),
        ]),
        row("10002", "Mary Nguyen", "active"),
        row("10004", "Linda Nguyen", "restricted"),
        row("10006", "Patricia Nguyen", "active"),
    ])
    # The wrapper row/cell nesting CoreServ actually emits.
    return node("document", "", [node("table", "", [node("row", "", [node("cell", "", [grid])])])])


def test_scoped_row_resolution_picks_the_right_view_link():
    element = Element(
        description="View link for a member's row",
        frame="content",
        chain=[
            LocatorRung(
                strategy="role_name_scoped",
                role="link",
                name="View",
                scope=Scope(role="row", contains="{{member_ref}}"),
                confidence="high",
            )
        ],
    )
    res = resolver.resolve_element(
        "results_view_link", element, {"content": results_grid_tree()}, {"member_ref": "10004"}
    )
    assert res.resolved
    assert res.node["ref"] == "view_10004"
    assert res.rung_index == 0


def test_innermost_row_wins_over_wrapper_rows():
    """CoreServ's wrapper rows transitively contain every inner row's text.
    Without innermost-match the scope is ambiguous and nothing resolves."""
    tree = results_grid_tree()
    scope = Scope(role="row", contains="10004")
    matches = resolver.find_scopes(tree, scope, {})
    assert len(matches) == 1
    assert any(c.get("name") == "Linda Nguyen" for c in matches[0]["children"])


def test_ambiguous_rung_is_a_miss_and_falls_through():
    """Three identically named View links must not silently resolve to the
    first one -- ambiguity falls through, and is recorded as ambiguous."""
    element = Element(
        description="View link",
        frame="content",
        chain=[
            LocatorRung(strategy="role_name", role="link", name="View", confidence="low"),
        ],
    )
    res = resolver.resolve_element("view", element, {"content": results_grid_tree()}, {})
    assert not res.resolved
    assert res.attempts[0].outcome == "ambiguous"
    assert res.attempts[0].match_count == 3


def test_fallthrough_to_brittle_rung_is_recorded():
    """A chain whose good rung misses must record that it succeeded only via
    the brittle rung -- that is the drift signal, and it has to survive into
    the trace even though the run succeeded."""
    element = Element(
        description="Member ref field",
        frame="content",
        chain=[
            LocatorRung(
                strategy="role_name", role="textbox", name="Account Number", confidence="high"
            ),
            LocatorRung(
                strategy="role_ordinal", role="textbox", index=1, confidence="low", brittle=True
            ),
        ],
    )
    tree = node("document", "", [
        node("textbox", "Last Name", ref="t0"),
        node("textbox", "Member ID", ref="t1"),
    ])
    res = resolver.resolve_element("member_ref_field", element, {"content": tree}, {})

    assert res.resolved
    assert res.rung_index == 1
    assert res.used_brittle_rung
    assert res.confidence == "low"
    assert res.attempts[0].outcome == "no_match"
    assert res.attempts[1].outcome == "resolved"
    assert res.as_dict()["brittle"] is True


def test_cell_in_row_by_column_header():
    accounts = node("table", "", [
        node("row", "", [
            node("columnheader", "Account Number"),
            node("columnheader", "Type"),
            node("columnheader", "Balance"),
        ]),
        node("row", "", [
            node("cell", "4471820020"),
            node("cell", "Savings"),
            node("cell", "8320.10"),
        ]),
        node("row", "", [
            node("cell", "4471820019"),
            node("cell", "Checking"),
            node("cell", "2140.55"),
        ]),
    ])
    element = Element(
        description="Savings balance",
        frame="content",
        chain=[
            LocatorRung(
                strategy="cell_in_row",
                scope=Scope(role="row", contains="Savings"),
                column_header="Balance",
                confidence="high",
            )
        ],
    )
    res = resolver.resolve_element("savings_balance_cell", element, {"content": accounts}, {})
    assert res.resolved
    assert res.node["name"] == "8320.10"


def test_cell_in_row_by_column_index():
    fields = node("table", "", [
        node("row", "", [node("cell", "Name"), node("cell", "John Smith")]),
        node("row", "", [node("cell", "Status"), node("cell", "active")]),
    ])
    element = Element(
        description="Member name value cell",
        frame="content",
        chain=[
            LocatorRung(
                strategy="cell_in_row",
                scope=Scope(role="row", contains="Name"),
                column_index=1,
                confidence="medium",
            )
        ],
    )
    res = resolver.resolve_element("member_name_cell", element, {"content": fields}, {})
    assert res.resolved
    assert res.node["name"] == "John Smith"


def test_resolution_never_crosses_frames():
    """Nav and content both hold a button named Submit; the frame is what
    disambiguates them, so resolution must not search outside its frame."""
    frames = {
        "navFrame": node("document", "", [node("button", "Submit", ref="nav_submit")]),
        "content": node("document", "", [node("button", "Submit", ref="content_submit")]),
    }
    element = Element(
        description="Search submit",
        frame="content",
        chain=[LocatorRung(strategy="role_name", role="button", name="Submit", confidence="high")],
    )
    res = resolver.resolve_element("search_submit", element, frames, {})
    assert res.resolved
    assert res.node["ref"] == "content_submit"


def test_missing_frame_is_reported_not_crashed():
    element = Element(
        description="x",
        frame="content",
        chain=[LocatorRung(strategy="role_name", role="button", name="Submit", confidence="high")],
    )
    res = resolver.resolve_element("x", element, {"navFrame": node("document")}, {})
    assert not res.resolved
    assert "not present" in (res.attempts[0].detail or "")


# ---------------------------------------------------------------------------
# Policy enforcement
# ---------------------------------------------------------------------------


def test_disallowed_action_is_blocked(artifact):
    with pytest.raises(PolicyViolation) as exc:
        check_action(artifact, "select", "s2")
    assert exc.value.kind == "action"


def test_allowed_action_passes(artifact):
    check_action(artifact, "click", "s3")


def test_fault_control_endpoint_is_blocked(artifact):
    """The agent must not be able to manipulate the app's own fault state."""
    with pytest.raises(PolicyViolation) as exc:
        check_destination(artifact, f"{BASE_URL}/_faults", "s1")
    assert exc.value.kind == "path"


def test_foreign_origin_is_blocked(artifact):
    with pytest.raises(PolicyViolation) as exc:
        check_destination(artifact, "http://evil.example.com/search", "s1")
    assert exc.value.kind == "origin"


def test_allowed_paths_accept_globbed_member_routes(artifact):
    check_destination(artifact, f"{BASE_URL}/member/10001", "s4")
    check_destination(artifact, f"{BASE_URL}/search/results?last_name=x", "s3")


def test_risky_step_is_blocked_under_require_confirmation(artifact):
    """Blocked, and blocked as its own exception type.

    This previously asserted a PolicyViolation, which is what made risky
    steps unable to reach a human: the engine aborts the run on a policy
    violation, by design, so the escalation route was never consulted. The
    refusal is real either way; what changed is that it is now distinguishable
    from "you tried to leave the allowlist".
    """
    from replay.executor import RiskBlocked, check_risk

    risky = artifact.steps[2].model_copy(update={"risk": "risky"})
    with pytest.raises(RiskBlocked) as exc:
        check_risk(artifact, risky)
    assert exc.value.handling == "require_confirmation"
    assert exc.value.step_id == risky.id


def test_risky_step_under_block_is_refused_with_the_blocking_policy(artifact):
    from replay.executor import RiskBlocked, check_risk

    blocking = artifact.model_copy(
        update={"policy": artifact.policy.model_copy(update={"risky_action_handling": "block"})}
    )
    risky = artifact.steps[2].model_copy(update={"risk": "risky"})
    with pytest.raises(RiskBlocked) as exc:
        check_risk(blocking, risky)
    assert exc.value.handling == "block"


# ---------------------------------------------------------------------------
# Classification layering
# ---------------------------------------------------------------------------


def test_engine_universals_win_over_artifact_outcomes(artifact, profile):
    """A session bounce shows a login page containing none of the member's
    data. A flow-first classifier would call that 'no such member' -- a wrong
    answer returned confidently. Universals must be checked first."""
    page_text = 'cell "Your session has ended."\ncell "No records match your criteria."'
    detection = classify.classify(artifact, ["member_not_found"], page_text, "/", profile)
    assert detection.name == "session_expired"
    assert detection.classification == "hard_failure"


def test_interstitial_is_recoverable_before_hard_failures(artifact, profile):
    page_text = 'cell "System Maintenance"\ncell "An unexpected error occurred."'
    detection = classify.detect_engine_universals(page_text, profile)
    assert detection.name == "maintenance_interstitial"
    assert detection.classification == "recoverable"


def test_artifact_outcome_detected_when_no_universal_applies(artifact, profile):
    page_text = 'cell "No records match your criteria."'
    detection = classify.classify(artifact, ["member_not_found"], page_text, "/search/results", profile)
    assert detection.name == "member_not_found"
    assert detection.classification == "business_outcome"


def test_an_artifact_outcome_is_only_reported_where_the_step_declares_it(artifact, profile):
    """Step scoping still governs the ARTIFACT layer: an outcome a flow
    declares is checked only where the flow said it could occur."""
    page_text = 'cell "No records match your criteria."'
    detection = classify.detect_artifact_outcomes(artifact, [], page_text, "/member/10001")
    assert detection is None


def test_an_app_level_outcome_is_reported_on_any_step(artifact, profile):
    """And profile-declared outcomes deliberately are not step-scoped.

    Phase 1 assumed every business outcome was flow-specific, on the grounds
    that only the flow knows a not-found search is an answer. That is too
    strong: "No records match" can only mean the search found nothing,
    whichever step is running when the page says it. Leaving it flow-only is
    what made a capability recorded from a happy path -- which never observes
    a not-found -- report "no such member" as a 502.
    """
    page_text = 'cell "No records match your criteria."'
    detection = classify.classify(artifact, [], page_text, "/member/10001", profile)
    assert detection is not None
    assert detection.layer == "profile"
    assert detection.classification == "business_outcome"


def test_the_artifact_layer_wins_over_the_app_layer(artifact, profile):
    """A capability can always be more precise about its own flow than the
    application is about itself."""
    page_text = 'cell "No records match your criteria."'
    detection = classify.classify(
        artifact, ["member_not_found"], page_text, "/search/results", profile)
    assert detection.layer == "artifact"


def test_unresolvable_element_is_escalation_eligible():
    detection = classify.element_unresolvable_detection("member_ref_field", "tried 2 rungs")
    assert detection.classification == "hard_failure"
    assert detection.escalation_eligible


# ---------------------------------------------------------------------------
# Risky steps: blocked before acting, and never retried
#
# The two rules under test are safety properties, not behaviours: a risky
# step must not be performed unattended, and once performed it must not be
# performed a second time because its confirmation was slow. Both are
# asserted by counting executions, since a retry is only visible as the same
# step running twice.
# ---------------------------------------------------------------------------

from tests.conftest import Observations, build_engine, make_artifact  # noqa: E402

RISKY_CLICK = {
    "id": "s1",
    "action": "click",
    "element": "post_button",
    "risk": "risky",
    "checkpoint": {"type": "text_present", "text": "TRANSFER POSTED", "timeout_ms": 300},
}
SAFE_CLICK = {
    "id": "s1",
    "action": "click",
    "element": "post_button",
    "risk": "safe",
    "checkpoint": {"type": "text_present", "text": "TRANSFER POSTED", "timeout_ms": 300},
}


def run_flow(engine, params=None):
    from replay.result import ReplayResult

    result = ReplayResult(
        classification="success", capability_id="t", capability_version="1.0.0",
        tenant="t", run_id=engine.run_id,
    )
    asyncio.run(engine._run_flow(params or {}, result))
    return result


def test_risky_step_is_blocked_before_the_action_runs(tmp_path):
    """check_risk fires ahead of the click, so a blocked step leaves the
    application in exactly the state the previous step left it in."""
    artifact = make_artifact([RISKY_CLICK])
    engine = build_engine(artifact, tmp_path)
    result = run_flow(engine)

    assert engine.executor.calls == [], "the risky action must not have been performed"
    assert result.classification == "hard_failure"
    assert result.observed == "the step was not performed; automation stopped before acting"
    assert "irreversible" in result.message


def test_risky_block_reports_what_the_checkpoint_would_verify(tmp_path):
    """The operator needs to know what 'done' looks like before they act, so
    `expected` describes the checkpoint rather than restating the error."""
    engine = build_engine(make_artifact([RISKY_CLICK]), tmp_path)
    result = run_flow(engine)
    assert "TRANSFER POSTED" in result.expected


def test_risky_step_without_escalation_hard_fails_cleanly(tmp_path):
    """Unattended replay stays unattended: it fails, it does not block."""
    engine = build_engine(make_artifact([RISKY_CLICK]), tmp_path, escalate=False)
    result = run_flow(engine)

    assert result.classification == "hard_failure"
    assert result.failed_step == "s1"
    assert result.escalation_eligible is True  # eligible, but nobody was listening
    assert not result.human_interventions


def test_block_policy_is_not_escalation_eligible(tmp_path):
    """'block' means no. Offering a human the chance to say yes would make it
    a synonym for require_confirmation."""
    artifact = make_artifact([RISKY_CLICK], risky_handling="block")
    engine = build_engine(artifact, tmp_path)
    result = run_flow(engine)

    assert result.classification == "hard_failure"
    assert result.escalation_eligible is False
    assert engine.executor.calls == []


def test_flag_policy_still_performs_the_step(tmp_path):
    """The third handling is unchanged: note it and carry on."""
    artifact = make_artifact([RISKY_CLICK], risky_handling="flag")
    engine = build_engine(
        artifact, tmp_path, observations=Observations(page_text="TRANSFER POSTED")
    )
    result = run_flow(engine)

    assert engine.executor.calls == ["s1"]
    assert result.classification == "success"


def test_risky_step_is_not_retried_on_checkpoint_timeout(tmp_path):
    """The dangerous case: the click landed and the confirmation did not
    arrive. A retry here is how one transfer becomes two."""
    artifact = make_artifact([RISKY_CLICK], risky_handling="flag")
    engine = build_engine(artifact, tmp_path, observations=Observations(page_text=""))
    result = run_flow(engine)

    assert engine.executor.calls == ["s1"], "a risky step must never be clicked twice"
    assert result.classification == "hard_failure"
    assert "may or may not have taken effect" in result.message
    assert result.escalation_eligible is True


def test_risky_step_is_not_retried_on_a_recoverable_detection(tmp_path):
    """Same rule via the other retry path: an interstitial after a risky
    click is not a reason to click again."""
    artifact = make_artifact([RISKY_CLICK], risky_handling="flag")
    engine = build_engine(
        artifact, tmp_path,
        observations=Observations(page_text=load_profile("coreserv").error_markers.maintenance[0]),
    )
    result = run_flow(engine)

    assert engine.executor.calls == ["s1"]
    assert result.classification == "hard_failure"
    assert "never retried automatically" in result.message


def test_safe_step_still_retries_up_to_the_budget(tmp_path):
    """The guard is keyed on risk, not applied to everything: a safe step
    keeps the two recovery attempts it always had."""
    artifact = make_artifact([SAFE_CLICK])
    engine = build_engine(artifact, tmp_path, observations=Observations(page_text=""))
    result = run_flow(engine)

    assert len(engine.executor.calls) == classify.MAX_RECOVERY_ATTEMPTS + 1 == 3
    assert result.classification == "hard_failure"


def test_origin_violation_hard_fails_whatever_the_escalate_flag_says(tmp_path):
    """A policy violation is a different thing from a risk decision and must
    not have picked up the escalation route on the way past."""
    from escalation.operator import ScriptedOperator
    from replay.executor import PolicyViolation as PV

    artifact = make_artifact([{
        "id": "s1", "action": "navigate", "path": "/forbidden", "risk": "safe",
    }], allowed_paths=["/start"])

    for escalate in (False, True):
        engine = build_engine(
            artifact, tmp_path / f"e{escalate}", escalate=escalate,
            operator=ScriptedOperator([OperatorDecision(Decision.RESUME)]) if escalate else None,
        )
        with pytest.raises(PV) as exc:
            run_flow(engine)
        assert exc.value.kind == "path"
        assert not engine.executor.calls


def test_risk_and_policy_are_separate_exception_types():
    """Structural: if these shared a type, the escalation route could not
    tell 'this is risky, ask a person' from 'this is not permitted'."""
    from replay.executor import PolicyViolation as PV
    from replay.executor import RiskBlocked

    assert not issubclass(RiskBlocked, PV)
    assert not issubclass(PV, RiskBlocked)


# ---------------------------------------------------------------------------
# Live tests against a running CoreServ
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
    """Set faults for one test and always clear them afterwards, so a fault
    can never leak into the next test's result."""
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


def run_replay(artifact, params, tmp_path):
    from replay.engine import ReplayEngine

    engine = ReplayEngine(artifact, evidence_root=tmp_path)
    return asyncio.run(engine.run(params)), engine


@live
def test_happy_path_returns_the_correct_savings_balance(artifact, faults, creds, tmp_path):
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)

    assert result.classification == "success", result.message
    # John Smith's savings account, per coreserv/data.py -- coerced to a
    # decimal string, not the raw cell text.
    assert result.outputs["savings_balance"] == "8320.10"
    assert result.outputs["member_name"] == "John Smith"
    assert [t.status for t in result.trace] == ["ok"] * 6


@live
def test_every_step_records_which_rung_resolved(artifact, faults, creds, tmp_path):
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    resolved = [t for t in result.trace if t.resolution]
    assert len(resolved) == 5  # every step except the navigate
    for trace in resolved:
        assert trace.resolution["resolved"] is True
        assert trace.resolution["rung_index"] == 0
        assert trace.resolution["brittle"] is False


@live
def test_unknown_member_is_a_business_outcome_not_an_exception(artifact, faults, creds, tmp_path):
    result, _ = run_replay(artifact, {"member_ref": "99999"}, tmp_path)
    assert result.classification == "business_outcome"
    assert result.outcome == "member_not_found"
    assert result.outputs == {}


@live
def test_member_not_found_fault_is_a_business_outcome(artifact, faults, creds, tmp_path):
    faults("member_not_found")
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    assert result.classification == "business_outcome"
    assert result.outcome == "member_not_found"


@live
def test_restricted_member_is_a_business_outcome(artifact, faults, creds, tmp_path):
    faults("restricted_member")
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    assert result.classification == "business_outcome"
    assert result.outcome == "permission_denied"


@live
def test_session_expiry_is_a_hard_failure(artifact, faults, creds, tmp_path):
    faults("session_expired")
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    assert result.classification == "hard_failure"
    assert result.escalation_eligible


@live
def test_maintenance_interstitial_is_recovered(artifact, faults, creds, tmp_path):
    faults("maintenance_interstitial")
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)

    assert result.classification == "success", result.message
    assert result.outputs["savings_balance"] == "8320.10"
    fired = [c["name"] for c in result.recoverable_conditions]
    assert "maintenance_interstitial" in fired
    assert any(t.status == "recovered" for t in result.trace)


@live
def test_bad_input_never_opens_a_browser(artifact, faults, creds, tmp_path):
    result, _ = run_replay(artifact, {"member_ref": "not-an-id"}, tmp_path)
    assert result.classification == "caller_error"
    assert result.violations[0]["code"] == "pattern_mismatch"
    assert result.trace == []


@live
def test_missing_credentials_is_an_auth_failure(artifact, faults, monkeypatch, tmp_path):
    monkeypatch.delenv("CORESERV_USERNAME", raising=False)
    monkeypatch.delenv("CORESERV_PASSWORD", raising=False)
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    assert result.classification == "auth_failure"
    assert result.trace == []


@live
def test_policy_violation_blocks_the_run(artifact, faults, creds, tmp_path):
    """Narrow the allowlist so the member detail route is off-limits, then
    confirm the run is stopped rather than warned."""
    narrowed = artifact.model_copy(deep=True)
    narrowed.policy.allowed_paths = ["/", "/home", "/nav", "/search", "/search/results"]

    result, _ = run_replay(narrowed, {"member_ref": "10001"}, tmp_path)

    assert result.classification == "hard_failure"
    assert result.violations
    assert result.violations[0]["kind"] == "path"
    assert "/member/10001" in result.violations[0]["detail"]
    assert result.outputs == {}


@live
def test_no_credential_value_appears_in_any_evidence_file(artifact, faults, creds, tmp_path):
    """CoreServ renders the logged-in username into its nav frame, so this
    is a real leak path, not a theoretical one."""
    faults("restricted_member")  # force a failure so a snapshot is captured
    result, engine = run_replay(artifact, {"member_ref": "10001"}, tmp_path)

    written = list(Path(tmp_path).rglob("*"))
    text_files = [p for p in written if p.is_file() and p.suffix in (".json", ".jsonl", ".txt")]
    assert text_files, "expected evidence to be written"

    for path in text_files:
        content = path.read_text(encoding="utf-8", errors="ignore")
        assert "testpassword" not in content, f"password leaked into {path.name}"
        assert "testoperator" not in content, f"username leaked into {path.name}"
        # The env var NAMES are fine -- that is what should be recorded.
    combined = "\n".join(
        p.read_text(encoding="utf-8", errors="ignore") for p in text_files
    )
    assert "CORESERV_PASSWORD" in combined


@live
def test_full_ssn_is_scrubbed_from_evidence(artifact, faults, creds, tmp_path):
    """The member detail screen renders full SSNs beside the data this flow
    legitimately reads; they must not survive into a page snapshot."""
    import re

    faults("restricted_member")
    run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    for path in Path(tmp_path).rglob("*"):
        if path.is_file() and path.suffix in (".json", ".jsonl", ".txt"):
            content = path.read_text(encoding="utf-8", errors="ignore")
            assert not re.search(r"\b\d{3}-\d{2}-\d{4}\b", content), f"SSN in {path.name}"


@live
def test_identifier_input_is_masked_in_the_result(artifact, faults, creds, tmp_path):
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    assert result.inputs_redacted["member_ref"] == "***01"
    assert "10001" not in json.dumps(result.as_dict()["inputs"])


@live
def test_app_version_drift_is_a_warning_not_a_failure(artifact, faults, creds, tmp_path):
    """The cascade overlay records app_version 4.2.3; the northridge server
    reports 4.2.1. The run should still proceed and merely warn."""
    drifted = artifact.model_copy(deep=True)
    drifted.target.app_version = "9.9.9"

    result, _ = run_replay(drifted, {"member_ref": "10001"}, tmp_path)

    assert result.classification == "success", result.message
    assert any("drift" in w for w in result.warnings)
    assert any("9.9.9" in w and "4.2.1" in w for w in result.warnings)


@live
def test_evidence_files_are_written_for_a_run(artifact, faults, creds, tmp_path):
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    run_dir = Path(tmp_path) / result.run_id
    assert (run_dir / "steps.jsonl").exists()
    assert (run_dir / "result.json").exists()
    records = [
        json.loads(line) for line in (run_dir / "steps.jsonl").read_text().splitlines() if line
    ]
    assert {"inputs_validated", "credentials_resolved", "result"} <= {r["event"] for r in records}


@live
def test_failure_captures_screenshot_and_snapshot(artifact, faults, creds, tmp_path):
    faults("restricted_member")
    result, _ = run_replay(artifact, {"member_ref": "10001"}, tmp_path)
    assert result.evidence.get("snapshot")
    assert Path(result.evidence["snapshot"]).exists()
    assert Path(result.evidence["screenshot"]).exists()


# ---------------------------------------------------------------------------
# Session re-auth: a per-step property, never a global behaviour
#
# The question is never "can we re-authenticate". It is "is it safe to retry a
# step whose side effect may already have happened".
# ---------------------------------------------------------------------------


def test_a_risky_step_may_not_declare_retry_after_reauth():
    """Enforced by the schema rather than merely defaulted false. Defaulting
    would leave the unsafe combination expressible by anyone hand-editing an
    artifact, and a session that died mid-post gives no way to know whether
    the post landed first."""
    from capability.schema import Step

    with pytest.raises(ValueError) as exc:
        Step(id="s1", action="click", element="e", risk="risky",
             retry_after_reauth=True,
             checkpoint={"type": "text_present", "text": "POSTED"})
    assert "not permitted on a step marked risk='risky'" in str(exc.value)


def test_a_safe_step_may_declare_it():
    from capability.schema import Step

    step = Step(id="s1", action="extract", element="e", into="o",
                retry_after_reauth=True)
    assert step.retry_after_reauth is True


def test_the_default_is_false():
    """A step nobody has thought about is not retried."""
    from capability.schema import Step

    assert Step(id="s1", action="extract", element="e", into="o").retry_after_reauth is False


def test_every_shipped_risky_step_refuses_reauth_retry():
    """The regression guard. If a capability ever ships an irreversible step
    that re-authenticates and retries, a session blip becomes a double post."""
    import glob

    for path in sorted(glob.glob(str(REPO_ROOT / "capabilities" / "*" / "1.0.0.json"))):
        data = json.loads(Path(path).read_text())
        for step in data["steps"]:
            if step["risk"] == "risky":
                assert step.get("retry_after_reauth") is not True, (
                    f"{data['capability']['id']} {step['id']}")


def test_the_recorder_grants_it_to_safe_steps_only():
    from capability.loader import load_resolved

    artifact = load_resolved(CAPABILITIES, "member_funds_transfer", "1.0.0")
    for step in artifact.steps:
        if step.risk == "risky":
            assert step.retry_after_reauth is False
        else:
            assert step.retry_after_reauth is True


def test_meridian_s_expiry_marker_matches_its_live_page():
    """Detection has to work before recovery means anything. The CoreServ
    marker did NOT match its live page for a whole phase, and the condition
    was invisible to the classifier the entire time."""
    from capability.profile import load_profile

    # Text as perception renders MERIDIAN's 440 page, from the diagnostic.
    page = ('cell "MERIDIAN CORE Member Services Platform v4.2.1" '
            'text "YOUR SESSION HAS TIMED OUT For security, your session ended '
            'due to inactivity." link "Return to Sign On" cell "NOT SIGNED ON"')
    profile = load_profile("meridian")
    assert profile.matches("session_expired", page)
    detection = classify.detect_engine_universals(page, profile)
    assert detection is not None and detection.name == classify.SESSION_EXPIRED


def test_reauth_replays_only_the_repeatable_prefix():
    """Re-authenticating lands on the entry page, not where the flow was, so
    the steps before the failure have to be re-walked -- but only those the
    artifact says are safe to repeat."""
    import inspect

    from replay.engine import ReplayEngine

    source = inspect.getsource(ReplayEngine._replay_prefix)
    assert "if not earlier.retry_after_reauth:" in source
    assert "continue" in source
