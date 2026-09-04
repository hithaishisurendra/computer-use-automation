"""Tests for the app-profile seam.

The claim under test is one sentence: pointing the core at a new application
is writing a profile, not editing `replay/` or `discovery/`. Most of these
assert that a value the engine used to hardcode now comes from config and
that two profiles genuinely produce different behaviour from the same code.

The measured values come from evidence/a11y_diagnostic_meridian/ and from
CoreServ's own templates, not from the profiles they are checking -- a test
that reads its expectation out of the file under test proves nothing.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from capability.loader import ArtifactError, apply_overlay, load_artifact, load_overlay, load_resolved
from capability.profile import AppProfile, ProfileError, load_profile, profile_for
from capability.redaction import profile_scrubber
from capability.schema import Element, LocatorRung, Scope
from replay import classify, resolver

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"
MERIDIAN_EVIDENCE = REPO_ROOT / "evidence" / "a11y_diagnostic_meridian"
SHIPPED = ["coreserv", "meridian"]


@pytest.fixture(params=SHIPPED)
def any_profile(request):
    return load_profile(request.param)


# ---------------------------------------------------------------------------
# Profiles load and are complete
# ---------------------------------------------------------------------------


def test_every_shipped_profile_loads(any_profile):
    assert any_profile.name
    for condition in ("session_expired", "server_error", "maintenance"):
        assert any_profile.markers_for(condition), (
            f"{any_profile.name} declares no marker for {condition}; the engine would "
            "not detect that condition at all, and would not say so"
        )


def test_every_profile_declares_how_to_recover_from_its_interstitial(any_profile):
    action = any_profile.recovery.get("maintenance_interstitial")
    assert action is not None
    assert action.kind in ("dismiss_control", "reload_step_url", "backoff")


def test_a_missing_profile_is_an_error_not_a_silent_default():
    """An engine with no profile has no markers and no recovery actions. It
    would fail to notice a session bounce, quietly -- the failure mode the
    whole file exists to remove."""
    with pytest.raises(ProfileError) as exc:
        load_profile("no_such_app")
    assert "no app profile" in str(exc.value)
    assert "coreserv" in str(exc.value)  # names what IS available


def test_profile_is_resolved_from_the_target_app(tmp_path):
    artifact = load_artifact(CAPABILITIES / "member_savings_balance" / "1.0.0.json")
    assert profile_for(artifact.target).name == "coreserv"


# ---------------------------------------------------------------------------
# Version patterns -- the silent no-op
# ---------------------------------------------------------------------------


def test_version_pattern_matches_text_the_app_actually_renders():
    """The bug this guards: r"CoreServ\\s+(\\d+...)" was hardcoded, so on any
    other app _check_drift returned on its first line forever and drift
    detection became invisible. Both patterns are checked against real
    captured page text rather than against themselves."""
    coreserv = load_profile("coreserv").version_re
    assert coreserv.search('cell "CoreServ 4.2.1"').group(1) == "4.2.1"

    observed = next(
        line for line in (MERIDIAN_EVIDENCE / "02_main_menu.txt").read_text().splitlines()
        if "MERIDIAN CORE" in line
    )
    assert load_profile("meridian").version_re.search(observed).group(1) == "4.2.1"


def test_each_profiles_version_pattern_does_not_match_the_other_app():
    """Otherwise a passing drift check would prove nothing."""
    meridian_line = 'cell "MERIDIAN CORE Member Services Platform v4.2.1"'
    assert load_profile("coreserv").version_re.search(meridian_line) is None


def test_a_version_pattern_with_no_capture_group_is_rejected_at_load():
    with pytest.raises(ValueError) as exc:
        AppProfile(name="x", version_pattern=r"CoreServ \d+\.\d+\.\d+")
    assert "capture group" in str(exc.value)


def test_an_uncompilable_version_pattern_is_rejected_at_load():
    with pytest.raises(ValueError):
        AppProfile(name="x", version_pattern=r"(unclosed")


# ---------------------------------------------------------------------------
# The classifier is profile-driven
# ---------------------------------------------------------------------------


def meridian_page(kind: str) -> str:
    """Real captured text from the injected-error pages."""
    return {
        "session_expired": 'cell "YOUR SESSION HAS TIMED OUT For security, your session ended due to inactivity."',
        "server_error": 'cell "APPLICATION ERROR An unexpected error occurred while processing your request."',
        "maintenance": 'cell "SCHEDULED MAINTENANCE IN PROGRESS The host is temporarily unavailable."',
    }[kind]


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("session_expired", "session_expired"),
        ("server_error", "server_error"),
        ("maintenance", "maintenance_interstitial"),
    ],
)
def test_meridian_conditions_are_detected_under_the_meridian_profile(kind, expected):
    detection = classify.detect_engine_universals(meridian_page(kind), load_profile("meridian"))
    assert detection is not None and detection.name == expected


@pytest.mark.parametrize("kind", ["session_expired", "server_error", "maintenance"])
def test_the_same_pages_are_invisible_under_the_coreserv_profile(kind):
    """This is the bug, reproduced. Every one of these was undetectable when
    the markers were constants in classify.py, and the run failed later on a
    checkpoint with a misleading reason instead."""
    assert classify.detect_engine_universals(meridian_page(kind), load_profile("coreserv")) is None


def test_coreserv_conditions_still_classify_as_before():
    coreserv = load_profile("coreserv")
    assert classify.detect_engine_universals(
        'cell "Your session has ended."', coreserv
    ).name == "session_expired"
    assert classify.detect_engine_universals(
        'cell "System Maintenance"', coreserv
    ).classification == "recoverable"


def test_recovery_advice_reflects_what_the_engine_will_actually_do():
    """CoreServ dismisses a control in place; MERIDIAN re-requests the URL
    because its 'Continue' is a link to /menu that abandons the flow. The
    message an operator reads has to match the action, not assume one."""
    cs = classify.detect_engine_universals('cell "System Maintenance"', load_profile("coreserv"))
    md = classify.detect_engine_universals(meridian_page("maintenance"), load_profile("meridian"))
    assert "dismiss via 'Continue'" in cs.recovery
    assert "re-request" in md.recovery
    assert load_profile("meridian").recovery["maintenance_interstitial"].kind == "reload_step_url"


# ---------------------------------------------------------------------------
# Redaction sources
# ---------------------------------------------------------------------------


def test_coreserv_literals_come_from_the_declared_fixture_module():
    from coreserv.data import MEMBERS

    scrubber = profile_scrubber(load_profile("coreserv"))
    assert not scrubber.sources.degraded
    member = MEMBERS[0]
    text = f'{member["first_name"]} {member["last_name"]} {member["address"]} {member["ssn"]}'
    scrubbed = scrubber.scrub(text)
    assert member["address"] not in scrubbed
    assert f'{member["first_name"]} {member["last_name"]}' not in scrubbed


def test_meridian_literals_come_from_the_profile_with_no_module_to_import():
    """The point of the change: an app with no fixture module still redacts
    names and addresses, because the profile declares them."""
    profile = load_profile("meridian")
    assert profile.redaction.fixture_module is None
    scrubber = profile_scrubber(profile)
    assert not scrubber.sources.degraded
    scrubbed = scrubber.scrub("cell \"Lovelace, Ada\" cell \"22 Harbor Lane, Arlington\"")
    assert "Lovelace, Ada" not in scrubbed
    assert "22 Harbor Lane" not in scrubbed


def test_chrome_literals_mask_the_status_bar_session_id_and_operator():
    """Values the app renders into its own furniture on every page, including
    error pages. Nothing sensitivity-driven would ever see them: they were
    never declared as a field of anything."""
    scrubbed = profile_scrubber(load_profile("meridian")).scrub(
        'cell "OPR TELLER1 | BR MAIN-001 | 09/03/2026 04:42:19 | SID A0F11594"'
    )
    assert "A0F11594" not in scrubbed
    assert "TELLER1" not in scrubbed


def test_meridian_short_phone_format_is_masked():
    """The default 3-3-4 rule does not match 555-0142, so the profile adds it."""
    assert "555-0142" not in profile_scrubber(load_profile("meridian")).scrub("Phone: 555-0142")


def test_a_profile_with_no_literal_source_reports_itself_degraded():
    """The phase-1 lesson: redaction failed quietly. Shape rules catch an SSN
    but not a name or a street address, and a whole-page capture is full of
    both."""
    bare = profile_scrubber(AppProfile(name="bare"))
    assert bare.sources.degraded
    warning = bare.sources.warning()
    assert "degraded" in warning and "NOT be masked" in warning


def test_degradation_reaches_the_caller_not_just_the_log(tmp_path):
    from replay.evidence import EvidenceWriter

    artifact = load_artifact(CAPABILITIES / "member_savings_balance" / "1.0.0.json")
    writer = EvidenceWriter(tmp_path / "run", artifact, profile=AppProfile(name="bare"))
    assert writer.redaction_warning() is not None

    logged = [json.loads(line) for line in (tmp_path / "run" / "steps.jsonl").read_text().splitlines()]
    configured = next(r for r in logged if r["event"] == "redaction_configured")
    assert configured["degraded"] is True
    assert configured["known_literals"] == 0


# ---------------------------------------------------------------------------
# Frame model
# ---------------------------------------------------------------------------


def node(role, name="", children=None, ref=None):
    return {"role": role, "name": name, "children": children or [], "ref": ref or f"e_{role}_{name}"}


def test_an_element_with_no_frame_resolves_against_the_document():
    """MERIDIAN has one frame and its name is the empty string. That is an
    implementation detail of the snapshot, not something an artifact should
    have to write down."""
    element = Element(
        description="submit",
        chain=[LocatorRung(strategy="role_name", role="button", name="Sign On", confidence="high")],
    )
    assert element.frame is None
    frames = {"": node("document", "", [node("button", "Sign On", ref="e1")])}
    res = resolver.resolve_element("submit", element, frames, {})
    assert res.resolved and res.node["ref"] == "e1"


def test_a_named_frame_still_resolves_only_within_itself():
    element = Element(
        description="submit",
        frame="content",
        chain=[LocatorRung(strategy="role_name", role="button", name="Submit", confidence="high")],
    )
    frames = {
        "": node("document", "", [node("button", "Submit", ref="top")]),
        "content": node("document", "", [node("button", "Submit", ref="content")]),
    }
    res = resolver.resolve_element("submit", element, frames, {})
    assert res.resolved and res.node["ref"] == "content"


def test_profiles_disagree_about_whether_frames_exist():
    assert load_profile("coreserv").content_frame == "content"
    assert load_profile("meridian").content_frame is None


def test_the_discovery_prompt_only_teaches_frames_where_they_exist():
    """Teaching a frame model to a model driving a frameless app produces
    actions naming a frame the page never had, which surfaces as 'element not
    found' and reads like a perception failure."""
    from discovery.prompts import build_system_prompt, build_tools

    framed = build_system_prompt("g", "u", "/e", ["/a"], ["click"], content_frame="content")
    flat = build_system_prompt("g", "u", "/e", ["/a"], ["click"], content_frame=None)
    assert "uses frames" in framed and "uses frames" not in flat

    click_flat = next(t for t in build_tools(None) if t.name == "click")
    click_framed = next(t for t in build_tools("content") if t.name == "click")
    assert "frame" not in click_flat.parameters["properties"]
    assert "frame" in click_framed.parameters["required"]


# ---------------------------------------------------------------------------
# Origins follow base_url
# ---------------------------------------------------------------------------


def test_origins_are_derived_from_base_url():
    artifact = load_artifact(CAPABILITIES / "member_savings_balance" / "1.0.0.json")
    assert artifact.policy.allowed_origins == ["http://localhost:8800"]


def test_an_overlay_moving_base_url_moves_the_origin_allowlist(tmp_path):
    """The blocking limitation: an overlay could set base_url but not
    allowed_origins, so a repointed artifact failed its own origin check and
    the only way to reach a second host was to edit the base artifact."""
    base = load_artifact(CAPABILITIES / "member_savings_balance" / "1.0.0.json")
    overlay_file = tmp_path / "remote.json"
    overlay_file.write_text(json.dumps({
        "extends": "member_savings_balance@1.0.0",
        "tenant": "remote",
        "target_overrides": {"base_url": "https://web-sample.interface-hiring.com"},
    }))
    resolved = apply_overlay(base, load_overlay(overlay_file))
    assert resolved.target.base_url == "https://web-sample.interface-hiring.com"
    assert resolved.policy.allowed_origins == ["https://web-sample.interface-hiring.com"]

    from replay.executor import PolicyViolation, check_destination

    check_destination(resolved, "https://web-sample.interface-hiring.com/search")
    with pytest.raises(PolicyViolation):
        check_destination(resolved, "http://localhost:8800/search")


def test_an_origin_that_disagrees_with_base_url_is_rejected(tmp_path):
    """Two things must not be able to disagree: where the flow points and
    where it is permitted to point."""
    data = json.loads((CAPABILITIES / "member_savings_balance" / "1.0.0.json").read_text())
    data["policy"]["allowed_origins"] = ["http://elsewhere.test"]
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ArtifactError) as exc:
        load_artifact(path)
    assert "derived from" in str(exc.value)


def test_paths_and_actions_keep_the_narrowing_only_rule(tmp_path):
    from capability.loader import OverlayError

    base = load_artifact(CAPABILITIES / "member_savings_balance" / "1.0.0.json")
    overlay_file = tmp_path / "wide.json"
    overlay_file.write_text(json.dumps({
        "extends": "member_savings_balance@1.0.0",
        "tenant": "wide",
        "policy_overrides": {"allowed_paths": ["/admin"]},
    }))
    with pytest.raises(OverlayError):
        apply_overlay(base, load_overlay(overlay_file))


# ---------------------------------------------------------------------------
# Auth through the element registry
# ---------------------------------------------------------------------------


def test_login_elements_come_from_the_registry_not_from_selectors():
    resolved = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0")
    auth = resolved.target.auth
    assert auth.submit and auth.elements
    for key in list(auth.elements.values()) + [auth.submit]:
        assert key in resolved.elements, f"{key} must exist in the element registry"


def test_meridian_auth_defaults_cover_its_third_sign_on_field():
    """MERIDIAN's sign-on takes a Branch select, which is neither a secret nor
    a caller input. Before AuthSpec.parameters it could only be expressed by
    inventing an environment variable for a non-secret."""
    defaults = load_profile("meridian").auth_defaults
    assert set(defaults.fields) == {"username", "password", "branch"}
    branch = Element.model_validate(defaults.elements[defaults.fields["branch"]])
    assert branch.chain[0].role == "combobox"
    assert branch.frame is None


def test_auth_parameters_are_declared_values_not_credential_refs():
    from capability.schema import AuthSpec, Condition

    spec = AuthSpec(
        mode="form_login",
        path="/signon",
        credentials_ref={"username": "OP_USER", "password": "OP_PASS"},
        parameters={"branch": "MAIN-001"},
        elements={"username": "u", "password": "p", "branch": "b"},
        submit="s",
        success_check=Condition(type="url_matches", pattern="/menu"),
    )
    assert spec.parameters["branch"] == "MAIN-001"
    with pytest.raises(ValueError):
        AuthSpec(
            mode="form_login", path="/",
            credentials_ref={"branch": "MAIN-001"},  # a literal, not a var name
            success_check=Condition(type="url_matches", pattern="/menu"),
        )


def test_an_auth_element_that_does_not_exist_is_rejected(tmp_path):
    data = json.loads((CAPABILITIES / "member_savings_balance" / "1.0.0.json").read_text())
    data["target"]["auth"]["elements"] = {"username": "no_such_element"}
    data["target"]["auth"]["submit"] = "also_missing"
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ArtifactError) as exc:
        load_artifact(path)
    assert "no_such_element" in str(exc.value)


def test_a_credential_with_no_control_to_type_it_into_is_rejected(tmp_path):
    data = json.loads((CAPABILITIES / "member_savings_balance" / "1.0.0.json").read_text())
    data["elements"]["u"] = {
        "description": "u", "frame": None,
        "chain": [{"strategy": "role_name", "role": "textbox", "name": "Username", "confidence": "high"}],
    }
    data["target"]["auth"]["elements"] = {"username": "u"}   # password unmapped
    data["target"]["auth"]["submit"] = "u"
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ArtifactError) as exc:
        load_artifact(path)
    assert "password" in str(exc.value)


# ---------------------------------------------------------------------------
# cell_in_row on a grid with no columnheader
# ---------------------------------------------------------------------------


def td_grid():
    """MERIDIAN's shape: a header row built from <td>, so the accessibility
    tree reports role 'cell' and there is no columnheader anywhere."""
    return node("table", "", [
        node("rowgroup", "", [
            node("row", "", [node("cell", "Share ID"), node("cell", "Type"),
                             node("cell", "Balance"), node("cell", "Status")]),
            node("row", "", [node("cell", "100234-S0001-6"), node("cell", "Regular Shares"),
                             node("cell", "$40.00", ref="bal"), node("cell", "OPEN", ref="st")]),
            node("row", "", [node("cell", "100234-CERT-15"), node("cell", "Certificate"),
                             node("cell", "$500.00"), node("cell", "HOLD")]),
        ]),
    ])


def th_grid():
    return node("table", "", [
        node("rowgroup", "", [
            node("row", "", [node("columnheader", "Account"), node("columnheader", "Balance")]),
            node("row", "", [node("cell", "Savings"), node("cell", "8320.10", ref="th_bal")]),
        ]),
    ])


def test_column_header_resolves_on_a_grid_whose_headers_are_td():
    """Zero columnheader nodes exist on MERIDIAN, so this strategy resolved
    nothing at all there. The fallback is a general improvement: legacy grids
    routinely style a <td> row instead of using <th>."""
    rung = LocatorRung(
        strategy="cell_in_row", scope=Scope(role="row", contains="100234-S0001-6"),
        column_header="Balance", confidence="high",
    )
    matches = resolver.match_rung(td_grid(), rung, {})
    assert len(matches) == 1 and matches[0]["name"] == "$40.00"


def test_the_fallback_reads_the_right_column_not_just_the_first():
    rung = LocatorRung(
        strategy="cell_in_row", scope=Scope(role="row", contains="100234-S0001-6"),
        column_header="Status", confidence="high",
    )
    matches = resolver.match_rung(td_grid(), rung, {})
    assert len(matches) == 1 and matches[0]["name"] == "OPEN"


def test_a_real_columnheader_is_still_preferred():
    rung = LocatorRung(
        strategy="cell_in_row", scope=Scope(role="row", contains="Savings"),
        column_header="Balance", confidence="high",
    )
    matches = resolver.match_rung(th_grid(), rung, {})
    assert len(matches) == 1 and matches[0]["ref"] == "th_bal"


def test_a_table_with_real_headers_does_not_fall_back_to_its_first_row():
    """A table that has headers and simply does not have this one is a miss,
    not an invitation to guess."""
    rung = LocatorRung(
        strategy="cell_in_row", scope=Scope(role="row", contains="Savings"),
        column_header="Status", confidence="high",
    )
    assert resolver.match_rung(th_grid(), rung, {}) == []


def test_an_ambiguous_header_name_is_refused():
    """'Which column is Balance' has no answer if two of them say Balance."""
    grid = node("table", "", [node("rowgroup", "", [
        node("row", "", [node("cell", "Balance"), node("cell", "Balance")]),
        node("row", "", [node("cell", "1"), node("cell", "2")]),
    ])])
    rung = LocatorRung(
        strategy="cell_in_row", scope=Scope(role="row", contains="1"),
        column_header="Balance", confidence="high",
    )
    assert resolver.match_rung(grid, rung, {}) == []


# ---------------------------------------------------------------------------
# The seam itself
# ---------------------------------------------------------------------------

APP_NAMES = ("coreserv", "meridian", "northridge", "cascade")


def _code_lines(path: Path) -> list[tuple[int, str]]:
    """Source lines with comments and docstrings stripped.

    Prose may name an application -- explaining why a rule exists usually
    requires it. Executable code may not.
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            doc_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if i in doc_lines:
            continue
        code = line.split("#", 1)[0]
        if code.strip():
            out.append((i, code))
    return out


@pytest.mark.parametrize("package", ["replay", "perception", "escalation"])
def test_engine_code_names_no_application(package):
    """The seam, asserted structurally.

    If pointing at a new target means editing these packages, the abstraction
    is in the wrong place. Prose is exempt; running code is not.
    """
    offenders = []
    for source in sorted((REPO_ROOT / package).glob("*.py")):
        for lineno, code in _code_lines(source):
            for app in APP_NAMES:
                if app in code.lower():
                    offenders.append(f"{source.relative_to(REPO_ROOT)}:{lineno}: {code.strip()}")
    assert not offenders, "application knowledge in engine code:\n" + "\n".join(offenders)


def test_engine_code_contains_no_app_specific_selectors():
    """Login used to be two CSS selectors and a button:has-text() in the
    engine, which is why sign-on was the one step that could not be
    retargeted without a code change."""
    banned = ('input[name=', 'button:has-text(', "frame.name == \"content\"", "'content'")
    offenders = []
    for source in sorted((REPO_ROOT / "replay").glob("*.py")):
        for lineno, code in _code_lines(source):
            for token in banned:
                if token in code:
                    offenders.append(f"{source.relative_to(REPO_ROOT)}:{lineno}: {code.strip()}")
    assert not offenders, "app-shaped selector in replay/:\n" + "\n".join(offenders)


# ---------------------------------------------------------------------------
# Templated fields
#
# Step.path was the field that carried caller data and was never substituted,
# so an artifact could declare navigate /members/{{member_ref}}, pass
# validation, and request that string literally. The audit found four more in
# the same state. These assert every one of them both substitutes and
# validates, because the defect was the inconsistency, not the one field.
# ---------------------------------------------------------------------------

TEMPLATED_ARTIFACT = {
    "schema_version": "1.0",
    "capability": {"id": "t", "version": "1.0.0", "name": "t", "description": "t"},
    "target": {"surface": "web", "app": "meridian", "app_version": "4.2.1", "tenant": "t",
               "base_url": "https://web-sample.interface-hiring.com", "entry_path": "/menu"},
    "inputs": [{"name": "member_ref", "type": "string", "required": True,
                "description": "member number", "sensitivity": "identifier", "example": "100234"}],
    "outputs": [],
    "elements": {"h": {"description": "heading", "frame": None,
                       "chain": [{"strategy": "role_name", "role": "heading",
                                  "name": "MEMBER RECORD", "confidence": "high"}]}},
    "steps": [{"id": "s1", "action": "navigate", "path": "/members/{{member_ref}}", "risk": "safe"}],
    "outcomes": [],
    "policy": {"allowed_paths": ["/members/*"], "allowed_actions": ["navigate"],
               "risky_action_handling": "flag", "max_steps": 25, "timeout_ms": 120000},
    "provenance": {"source": "hand_written", "discovered_at": "2026-09-03T00:00:00Z",
                   "goal": "t", "steps_attempted": 1, "steps_recorded": 1},
}


def _write(tmp_path, data):
    path = tmp_path / "a.json"
    path.write_text(json.dumps(data))
    return path


def test_a_parameterised_path_substitutes_before_navigating(tmp_path):
    """The bug: this navigated to the literal '/members/{{member_ref}}'."""
    from capability.schema import Artifact
    from replay import resolver

    artifact = Artifact.model_validate(TEMPLATED_ARTIFACT)
    resolved = resolver.substitute(artifact.steps[0].path, {"member_ref": "100234"})
    assert resolved == "/members/100234"


def test_a_path_referencing_an_undeclared_input_fails_at_load(tmp_path):
    data = json.loads(json.dumps(TEMPLATED_ARTIFACT))
    data["steps"][0]["path"] = "/members/{{no_such_input}}"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(_write(tmp_path, data))
    assert "no_such_input" in str(exc.value)
    assert "path" in str(exc.value)


def test_existing_artifacts_with_literal_paths_are_unaffected():
    """Both committed artifacts navigate to literal paths; substitution over a
    string with no template is the identity."""
    from replay import resolver

    for cid in ("member_savings_balance", "member_savings_balance_discovered"):
        artifact = load_resolved(CAPABILITIES, cid, "1.0.0")
        for step in artifact.steps:
            if step.action == "navigate":
                assert resolver.substitute(step.path, {}) == step.path


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["steps"][0].__setitem__("path", "/members/{{nope}}"), "path"),
    (lambda d: d["steps"][0].__setitem__(
        "checkpoint", {"type": "text_present", "text": "Member {{nope}}"}), "text"),
    (lambda d: d["steps"][0].__setitem__(
        "checkpoint", {"type": "url_matches", "pattern": "/members/{{nope}}$"}), "pattern"),
    (lambda d: d["elements"]["h"]["chain"][0].__setitem__("name", "Member {{nope}}"), "rung name"),
])
def test_every_templated_field_is_validated_against_declared_inputs(tmp_path, mutate, needle):
    data = json.loads(json.dumps(TEMPLATED_ARTIFACT))
    mutate(data)
    with pytest.raises(ArtifactError) as exc:
        load_artifact(_write(tmp_path, data))
    assert "nope" in str(exc.value) and needle in str(exc.value)


def test_checkpoint_text_and_pattern_substitute_at_evaluation():
    from replay import checkpoints
    from capability.schema import Artifact, Condition

    artifact = Artifact.model_validate(TEMPLATED_ARTIFACT)
    params = {"member_ref": "100234"}
    frames = {"": node("document")}

    text = Condition(type="text_present", text="Member {{member_ref}}")
    assert checkpoints.evaluate_once(
        text, artifact, frames, "MEMBER RECORD Member 100234", "/", params
    ).satisfied
    assert not checkpoints.evaluate_once(
        text, artifact, frames, "MEMBER RECORD Member 999999", "/", params
    ).satisfied

    url = Condition(type="url_matches", pattern="/members/{{member_ref}}$")
    assert checkpoints.evaluate_once(
        url, artifact, frames, "", "/members/100234", params
    ).satisfied


def test_a_value_substituted_into_a_regex_is_escaped():
    """A caller-supplied value is data, not pattern syntax."""
    from replay import resolver

    assert resolver.substitute_regex("/x/{{p}}$", {"p": "a.b"}) == "/x/a\\.b$"
    assert re.search(resolver.substitute_regex("/x/{{p}}$", {"p": "a.b"}), "/x/a.b")
    assert not re.search(resolver.substitute_regex("/x/{{p}}$", {"p": "a.b"}), "/x/axb")


def test_outcome_detection_substitutes_too():
    from capability.schema import Artifact
    from replay import classify

    data = json.loads(json.dumps(TEMPLATED_ARTIFACT))
    data["outcomes"] = [{"name": "no_shares", "classification": "business_outcome",
                         "detect": {"type": "text_present", "text": "No shares for {{member_ref}}"},
                         "terminal": True, "message": "The member has no shares."}]
    data["steps"][0]["outcomes"] = ["no_shares"]
    artifact = Artifact.model_validate(data)

    hit = classify.detect_artifact_outcomes(
        artifact, ["no_shares"], "No shares for 100234", "/", {"member_ref": "100234"})
    assert hit is not None and hit.name == "no_shares"
    miss = classify.detect_artifact_outcomes(
        artifact, ["no_shares"], "No shares for 999999", "/", {"member_ref": "100234"})
    assert miss is None


def test_scope_name_and_rung_name_substitute():
    from capability.schema import LocatorRung, Scope

    tree = node("table", "", [
        node("row", "Member 100234", [node("link", "Open 100234", ref="want")]),
        node("row", "Member 999999", [node("link", "Open 999999", ref="other")]),
    ])
    rung = LocatorRung(strategy="role_name", role="link", name="Open {{member_ref}}",
                       confidence="high")
    matches = resolver.match_rung(tree, rung, {"member_ref": "100234"})
    assert len(matches) == 1 and matches[0]["ref"] == "want"

    scoped = LocatorRung(strategy="role_name_scoped", role="link", name="Open 100234",
                         scope=Scope(role="row", name="Member {{member_ref}}"),
                         confidence="high")
    assert len(resolver.match_rung(tree, scoped, {"member_ref": "100234"})) == 1


# ---------------------------------------------------------------------------
# Discovery config comes from the profile
# ---------------------------------------------------------------------------


def test_discovery_target_is_built_from_the_profile_not_from_cli_defaults():
    from discovery.run import build_target

    meridian = build_target("https://web-sample.interface-hiring.com", None, "demo",
                            "4.2.1", load_profile("meridian"))
    assert meridian.entry_path == "/menu"
    assert meridian.auth.path == "/signon"
    assert meridian.auth.success_check.pattern == "/menu"
    assert meridian.auth.parameters == {"branch": "MAIN-001"}
    assert set(meridian.auth.credentials_ref) == {"username", "password"}

    coreserv = build_target("http://localhost:8800", None, "northridge", "4.2.1",
                            load_profile("coreserv"))
    assert coreserv.entry_path == "/search"
    assert coreserv.auth.success_check.pattern == "/home|/search"


def test_each_app_has_its_own_discovery_policy():
    """The allowlist stays a separate file from the profile: a profile says
    what the app is, a policy says what the agent may do to it."""
    from discovery.run import default_policy_path, load_policy

    md = load_policy(default_policy_path("meridian"),
                     "https://web-sample.interface-hiring.com", None)
    assert "/settings" not in md.allowed_paths, "the fault console must stay off the allowlist"
    assert md.allowed_origins == ["https://web-sample.interface-hiring.com"]

    cs = load_policy(default_policy_path("coreserv"), "http://localhost:8800", None)
    assert "/_faults" not in cs.allowed_paths


def test_a_name_written_the_other_way_round_is_still_masked():
    """Consoles render names surname-first; prose does not. A discovery run's
    own summary said "member 100234 (Ada Lovelace)" and the scrubber, holding
    only the comma form, let it into evidence. The model's prose restates
    values in shapes nobody enumerated, so it is a redaction channel."""
    scrubber = profile_scrubber(load_profile("meridian"))
    assert "Ada Lovelace" not in scrubber.scrub("member 100234 (Ada Lovelace) is 20")
    assert "Lovelace, Ada" not in scrubber.scrub('cell "Lovelace, Ada"')


def test_name_variants_only_flips_a_comma_form():
    from capability.redaction import name_variants

    assert name_variants("Lovelace, Ada") == ["Ada Lovelace"]
    assert name_variants("22 Harbor Lane, Arlington") == ["Arlington 22 Harbor Lane"]
    assert name_variants("no comma here") == []
