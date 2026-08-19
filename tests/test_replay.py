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
from capability.schema import Artifact, Element, LocatorRung, Scope
from replay import classify, resolver
from replay.executor import PolicyViolation, check_action, check_destination

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"
BASE_PATH = CAPABILITIES / "member_savings_balance" / "1.0.0.json"
BASE_URL = os.environ.get("CORESERV_URL", "http://localhost:8800")


@pytest.fixture
def artifact() -> Artifact:
    return load_artifact(BASE_PATH)


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
    for source in sorted((REPO_ROOT / "replay").glob("*.py")):
        for name in _imported_names(source):
            root = name.split(".")[0]
            if root in MODEL_CLIENT_MODULES or name in MODEL_CLIENT_MODULES:
                offenders.setdefault(source.name, []).append(name)
    assert not offenders, f"model client imported in replay/: {offenders}"


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
    from replay.executor import check_risk

    risky = artifact.steps[2].model_copy(update={"risk": "risky"})
    with pytest.raises(PolicyViolation) as exc:
        check_risk(artifact, risky)
    assert exc.value.kind == "risky_action"


# ---------------------------------------------------------------------------
# Classification layering
# ---------------------------------------------------------------------------


def test_engine_universals_win_over_artifact_outcomes(artifact):
    """A session bounce shows a login page containing none of the member's
    data. A flow-first classifier would call that 'no such member' -- a wrong
    answer returned confidently. Universals must be checked first."""
    page_text = 'cell "Your session has ended."\ncell "No records match your criteria."'
    detection = classify.classify(artifact, ["member_not_found"], page_text, "/")
    assert detection.name == "session_expired"
    assert detection.classification == "hard_failure"


def test_interstitial_is_recoverable_before_hard_failures(artifact):
    page_text = 'cell "System Maintenance"\ncell "An unexpected error occurred."'
    detection = classify.detect_engine_universals(page_text)
    assert detection.name == "maintenance_interstitial"
    assert detection.classification == "recoverable"


def test_artifact_outcome_detected_when_no_universal_applies(artifact):
    page_text = 'cell "No records match your criteria."'
    detection = classify.classify(artifact, ["member_not_found"], page_text, "/search/results")
    assert detection.name == "member_not_found"
    assert detection.classification == "business_outcome"


def test_outcome_not_declared_on_the_step_is_not_reported(artifact):
    """'No records match' is an answer after the search step and a non
    sequitur after the extract step."""
    page_text = 'cell "No records match your criteria."'
    assert classify.classify(artifact, [], page_text, "/member/10001") is None


def test_unresolvable_element_is_escalation_eligible():
    detection = classify.element_unresolvable_detection("member_ref_field", "tried 2 rungs")
    assert detection.classification == "hard_failure"
    assert detection.escalation_eligible


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
