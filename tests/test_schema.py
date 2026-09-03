"""Tests for the capability artifact schema, loader and pre-flight validation.

These cover the contract boundaries that matter before any replay engine
exists: that a good artifact loads, that a bad one is rejected with an error
naming the actual problem, that caller input is checked before a browser
opens, and that a tenant overlay can specialise a capability without being
able to quietly change what it does.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from capability.loader import (
    ArtifactError,
    OverlayError,
    apply_overlay,
    load_artifact,
    load_overlay,
    load_resolved,
)
from capability.validate import (
    AuthConfigError,
    CallerInputError,
    describe_credentials,
    redact,
    resolve_credentials,
    validate_inputs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"
BASE_PATH = CAPABILITIES / "member_savings_balance" / "1.0.0.json"
CASCADE_PATH = CAPABILITIES / "member_savings_balance" / "tenants" / "cascade.json"


@pytest.fixture
def base_data() -> dict:
    return json.loads(BASE_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def base_artifact():
    return load_artifact(BASE_PATH)


def write(tmp_path: Path, data: dict, name: str = "artifact.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# valid artifact loads
# ---------------------------------------------------------------------------


def test_base_artifact_loads_and_resolves(base_artifact):
    assert base_artifact.capability.id == "member_savings_balance"
    assert base_artifact.capability.version == "1.0.0"
    assert base_artifact.capability.status == "draft"
    assert base_artifact.target.tenant == "northridge"
    assert base_artifact.target.app_version == "4.2.1"
    assert [s.id for s in base_artifact.steps] == ["s1", "s2", "s3", "s4", "s5", "s6"]
    assert set(base_artifact.output_map) == {"savings_balance", "member_name"}


def test_every_step_and_checkpoint_reference_resolves(base_artifact):
    """The cross-block validator should have already guaranteed this; asserting
    it here documents the invariant the replay engine will rely on."""
    element_keys = set(base_artifact.elements)
    for step in base_artifact.steps:
        if step.element:
            assert step.element in element_keys
        for name in step.outcomes:
            assert name in base_artifact.outcome_map


def test_element_names_match_what_perception_reports(base_artifact):
    """Guards against the most likely regression in a hand-written artifact:
    an accessible name that was guessed rather than read off the diagnostic."""
    chains = {k: v.chain[0] for k, v in base_artifact.elements.items()}
    assert (chains["member_ref_field"].role, chains["member_ref_field"].name) == (
        "textbox",
        "Member ID",
    )
    assert (chains["search_submit"].role, chains["search_submit"].name) == ("button", "Submit")
    assert (chains["results_grid"].role, chains["results_grid"].name) == (
        "columnheader",
        "Member ID",
    )
    assert (chains["results_view_link"].role, chains["results_view_link"].name) == ("link", "View")
    # Rendered as a table cell, not a heading -- CoreServ has no landmarks.
    assert (chains["member_detail_heading"].role, chains["member_detail_heading"].name) == (
        "cell",
        "Member Detail",
    )


def test_read_only_capability_declares_every_step_safe(base_artifact):
    assert all(step.risk == "safe" for step in base_artifact.steps)


def test_fault_control_endpoint_is_not_in_the_allowlist(base_artifact):
    """The agent must not be able to manipulate the app's fault state."""
    assert "/_faults" not in base_artifact.policy.allowed_paths
    assert not any(p.startswith("/_") for p in base_artifact.policy.allowed_paths)


# ---------------------------------------------------------------------------
# invalid artifacts rejected with a clear error
# ---------------------------------------------------------------------------


def test_step_referencing_unknown_element_is_rejected(tmp_path, base_data):
    base_data["steps"][1]["element"] = "no_such_element"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "no_such_element" in str(exc.value)
    assert "s2" in str(exc.value)


def test_extract_into_undeclared_output_is_rejected(tmp_path, base_data):
    base_data["steps"][4]["into"] = "not_an_output"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "not_an_output" in str(exc.value)


def test_risky_step_without_a_checkpoint_is_rejected(tmp_path, base_data):
    """An irreversible action with no checkpoint is unverifiable, and the
    escalation model depends on verification: a human performs the step and
    the checkpoint is what confirms it landed. Caught at load time so it fails
    before a browser opens, rather than mid-run after a person has acted."""
    step = base_data["steps"][4]          # s5, an extract with no checkpoint
    step["risk"] = "risky"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    message = str(exc.value)
    assert "s5" in message
    assert "checkpoint" in message
    assert "cannot be verified" in message


def test_risky_step_with_a_checkpoint_loads(tmp_path, base_data):
    """The rule is about verifiability, not about forbidding risky steps."""
    step = base_data["steps"][4]
    step["risk"] = "risky"
    step["checkpoint"] = {
        "type": "text_present", "text": "Transfer posted", "timeout_ms": 5000,
    }
    artifact = load_artifact(write(tmp_path, base_data))
    assert artifact.steps[4].risk == "risky"


def test_template_param_with_no_matching_input_is_rejected(tmp_path, base_data):
    base_data["steps"][1]["value"] = "{{nonexistent_param}}"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "nonexistent_param" in str(exc.value)


def test_step_action_outside_policy_allowlist_is_rejected(tmp_path, base_data):
    base_data["policy"]["allowed_actions"] = ["navigate", "click", "extract"]
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "fill" in str(exc.value)
    assert "allowed_actions" in str(exc.value)


def test_required_output_with_no_extract_step_is_rejected(tmp_path, base_data):
    base_data["steps"] = [s for s in base_data["steps"] if s["id"] != "s5"]
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "savings_balance" in str(exc.value)


def test_role_ordinal_must_be_marked_brittle(tmp_path, base_data):
    base_data["elements"]["member_ref_field"]["chain"][1]["brittle"] = False
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "brittle" in str(exc.value)


def test_cell_in_row_requires_exactly_one_column_selector(tmp_path, base_data):
    base_data["elements"]["savings_balance_cell"]["chain"][0]["column_index"] = 2
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "column_header" in str(exc.value)


def test_unknown_field_is_rejected_rather_than_silently_dropped(tmp_path, base_data):
    base_data["steps"][0]["hallucinated_field"] = "surprise"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "hallucinated_field" in str(exc.value)


def test_unsupported_schema_version_is_rejected(tmp_path, base_data):
    base_data["schema_version"] = "2.0"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "schema_version" in str(exc.value)


def test_credentials_ref_holding_a_literal_value_is_rejected(tmp_path, base_data):
    """The artifact is committed to a public repo; a pasted password must not
    validate."""
    base_data["target"]["auth"]["credentials_ref"]["password"] = "hunter2"
    with pytest.raises(ArtifactError) as exc:
        load_artifact(write(tmp_path, base_data))
    assert "ENVIRONMENT VARIABLE NAME" in str(exc.value)


def test_missing_file_reports_the_path(tmp_path):
    with pytest.raises(ArtifactError) as exc:
        load_artifact(tmp_path / "nope.json")
    assert "nope.json" in str(exc.value)


def test_malformed_json_reports_the_path(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ArtifactError) as exc:
        load_artifact(path)
    assert "not valid JSON" in str(exc.value)


# ---------------------------------------------------------------------------
# input validation (caller error, before any browser opens)
# ---------------------------------------------------------------------------


def test_valid_member_ref_passes(base_artifact):
    validated = validate_inputs(base_artifact, {"member_ref": "10001"})
    assert validated.values == {"member_ref": "10001"}


def test_malformed_member_ref_is_a_caller_error(base_artifact):
    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {"member_ref": "abc"})
    result = exc.value.as_result()
    assert result["classification"] == "caller_error"
    assert result["violations"][0]["input"] == "member_ref"
    assert result["violations"][0]["code"] == "pattern_mismatch"


@pytest.mark.parametrize("bad", ["1234", "123456", "", "1000a", " 10001"])
def test_member_ref_pattern_is_anchored(base_artifact, bad):
    """A pattern that matched a substring would let '123456' through as if it
    were a five-digit id."""
    with pytest.raises(CallerInputError):
        validate_inputs(base_artifact, {"member_ref": bad})


def test_missing_required_input_is_a_caller_error(base_artifact):
    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {})
    assert exc.value.violations[0].code == "missing_required"


def test_unknown_input_is_a_caller_error(base_artifact):
    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {"member_ref": "10001", "sneaky": "x"})
    assert any(v.code == "unknown_input" for v in exc.value.violations)


def test_wrong_type_is_a_caller_error(base_artifact):
    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {"member_ref": 10001})
    assert exc.value.violations[0].code == "type_mismatch"


def test_all_violations_reported_together(base_artifact):
    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {"sneaky": "x"})
    codes = {v.code for v in exc.value.violations}
    assert codes == {"missing_required", "unknown_input"}


def test_identifier_values_are_masked_in_errors(base_artifact):
    """member_ref is declared sensitivity=identifier, so a rejection message
    must not echo it in full."""
    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {"member_ref": "9999999"})
    message = str(exc.value)
    assert "9999999" not in message
    assert "99" in message


def test_redact_respects_declared_sensitivity():
    assert redact("412-88-2201", "pii") == "<redacted>"
    assert redact("hunter2", "secret") == "<redacted>"
    assert redact("10001", "identifier") == "***01"
    assert redact("Savings", "public") == "Savings"


def test_validated_inputs_carry_a_log_safe_rendering(base_artifact):
    validated = validate_inputs(base_artifact, {"member_ref": "10001"})
    assert validated.redacted["member_ref"] == "***01"


# ---------------------------------------------------------------------------
# auth: credentials resolve before any browser action
# ---------------------------------------------------------------------------


def test_missing_env_var_is_an_auth_failure(base_artifact):
    with pytest.raises(AuthConfigError) as exc:
        resolve_credentials(base_artifact, env={})
    result = exc.value.as_result()
    assert result["classification"] == "auth_failure"
    assert set(result["missing_env_vars"]) == {"CORESERV_USERNAME", "CORESERV_PASSWORD"}
    assert "not retryable" in result["message"]


def test_partial_credentials_still_an_auth_failure(base_artifact):
    with pytest.raises(AuthConfigError) as exc:
        resolve_credentials(base_artifact, env={"CORESERV_USERNAME": "operator"})
    assert exc.value.missing == ["CORESERV_PASSWORD"]


def test_credentials_resolve_from_env(base_artifact):
    creds = resolve_credentials(
        base_artifact, env={"CORESERV_USERNAME": "operator", "CORESERV_PASSWORD": "s3cret"}
    )
    assert creds == {"username": "operator", "password": "s3cret"}


def test_no_credential_value_appears_in_the_artifact_file():
    """The artifact is committed to a public repo. It may name variables; it
    may never contain values."""
    raw = BASE_PATH.read_text(encoding="utf-8")
    artifact = load_artifact(BASE_PATH)
    for var_name in artifact.target.auth.credentials_ref.values():
        assert var_name in raw
    for leaked in ("password=", "hunter2", "s3cret", "CORESERV_PASSWORD=", "secret_value"):
        assert leaked not in raw


def test_describe_credentials_is_log_safe(base_artifact):
    described = describe_credentials(base_artifact)
    rendered = json.dumps(described)
    assert described["auth_required"] is True
    assert described["mode"] == "form_login"
    assert described["env_vars"]["password"]["name"] == "CORESERV_PASSWORD"
    # Only names and resolution booleans -- never a value.
    assert "s3cret" not in rendered
    assert set(described["env_vars"]["password"]) == {"name", "resolved"}


def test_capability_layer_never_touches_a_browser():
    """The strongest available proof that input and auth validation happen
    'before any browser opens': the whole capability package loads, parses an
    artifact and rejects bad credentials with playwright unimportable.

    Run in a subprocess deliberately -- blocking a module in-process would
    require reloading, and a reload swaps the exception classes other tests
    hold references to.
    """
    program = """
import sys
for name in ("playwright", "playwright.sync_api", "playwright.async_api"):
    sys.modules[name] = None  # any import of these now raises ImportError
sys.path.insert(0, %r)
from capability.loader import load_artifact
from capability.validate import validate_inputs, resolve_credentials, CallerInputError, AuthConfigError
artifact = load_artifact(%r)
validate_inputs(artifact, {"member_ref": "10001"})
try:
    resolve_credentials(artifact, env={})
except AuthConfigError as exc:
    assert exc.as_result()["classification"] == "auth_failure"
else:
    raise AssertionError("expected AuthConfigError")
print("ok")
""" % (str(REPO_ROOT), str(BASE_PATH))

    proc = subprocess.run(
        [sys.executable, "-c", program], capture_output=True, text=True, cwd=REPO_ROOT
    )
    assert proc.returncode == 0, proc.stderr
    assert "ok" in proc.stdout


def test_auth_failure_is_raised_without_reading_the_ambient_environment(base_artifact, monkeypatch):
    """Passing `env` explicitly must not silently fall through to os.environ --
    otherwise a developer's own shell could mask a missing-credential bug."""
    monkeypatch.setenv("CORESERV_USERNAME", "operator")
    monkeypatch.setenv("CORESERV_PASSWORD", "s3cret")
    with pytest.raises(AuthConfigError):
        resolve_credentials(base_artifact, env={})


# ---------------------------------------------------------------------------
# tenant overlay
# ---------------------------------------------------------------------------


def test_cascade_overlay_overrides_only_the_intended_elements(base_artifact):
    resolved = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="cascade")

    assert resolved.elements["member_ref_field"].chain[0].name == "Account Number"
    assert resolved.elements["results_grid"].chain[0].name == "Account Number"

    untouched = [
        "search_submit",
        "results_view_link",
        "member_detail_heading",
        "savings_balance_cell",
        "member_name_cell",
    ]
    for key in untouched:
        assert resolved.elements[key] == base_artifact.elements[key], key

    # Flow, outputs and identity are identical -- only the how-to-find differs.
    assert resolved.steps == base_artifact.steps
    assert resolved.outputs == base_artifact.outputs
    assert resolved.outcomes == base_artifact.outcomes
    assert resolved.capability == base_artifact.capability


def test_cascade_overlay_updates_tenant_and_drift_signal():
    resolved = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="cascade")
    assert resolved.target.tenant == "cascade"
    assert resolved.target.app_version == "4.2.3"


def test_overlay_preserves_the_element_description_it_did_not_override(base_artifact):
    """A chain-only override must merge, not replace the whole element."""
    resolved = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="cascade")
    assert (
        resolved.elements["member_ref_field"].description
        == base_artifact.elements["member_ref_field"].description
    )
    assert resolved.elements["member_ref_field"].notes is not None


def test_cascade_accepts_a_ten_digit_ref_and_northridge_does_not(base_artifact):
    cascade = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="cascade")

    validated = validate_inputs(cascade, {"member_ref": "4471820019"})
    assert validated.values["member_ref"] == "4471820019"

    with pytest.raises(CallerInputError) as exc:
        validate_inputs(base_artifact, {"member_ref": "4471820019"})
    assert exc.value.violations[0].code == "pattern_mismatch"


def test_northridge_ref_is_rejected_by_the_cascade_contract():
    cascade = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="cascade")
    with pytest.raises(CallerInputError):
        validate_inputs(cascade, {"member_ref": "10001"})


def test_row_scope_template_is_shared_by_both_tenants(base_artifact):
    """The reason results_view_link needs no cascade override: each tenant's
    grid displays whichever identifier that tenant searches by."""
    cascade = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="cascade")
    for artifact in (base_artifact, cascade):
        scope = artifact.elements["results_view_link"].chain[0].scope
        assert scope.contains == "{{member_ref}}"


def test_tenant_with_no_overlay_file_loads_the_base_unmodified(base_artifact):
    """A tenant with no overlay runs the base flow.

    Compared against the base with the same profile defaults applied, since
    resolution now also fills in the login elements an artifact recorded
    before the auth seam existed does not declare. The flow itself -- steps,
    outputs, outcomes, policy -- must be untouched.
    """
    from capability.loader import apply_profile_defaults

    resolved = load_resolved(CAPABILITIES, "member_savings_balance", "1.0.0", tenant="northridge")
    assert resolved == apply_profile_defaults(base_artifact)
    assert resolved.steps == base_artifact.steps
    assert resolved.outputs == base_artifact.outputs
    assert resolved.outcomes == base_artifact.outcomes


# -- what an overlay may NOT do ---------------------------------------------


def test_overlay_changing_steps_is_rejected(tmp_path):
    overlay = {
        "extends": "member_savings_balance@1.0.0",
        "tenant": "rogue",
        "steps": [{"id": "s1", "action": "navigate", "path": "/elsewhere", "risk": "safe"}],
    }
    with pytest.raises(OverlayError) as exc:
        load_overlay(write(tmp_path, overlay, "rogue.json"))
    assert "steps" in str(exc.value)
    assert "fork" in str(exc.value).lower()


def test_overlay_changing_outputs_is_rejected(tmp_path):
    overlay = {
        "extends": "member_savings_balance@1.0.0",
        "tenant": "rogue",
        "outputs": [{"name": "something_else", "type": "string", "description": "x"}],
    }
    with pytest.raises(OverlayError) as exc:
        load_overlay(write(tmp_path, overlay, "rogue.json"))
    assert "outputs" in str(exc.value)


def test_overlay_widening_allowed_paths_is_rejected(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {
            "extends": "member_savings_balance@1.0.0",
            "tenant": "rogue",
            "policy_overrides": {"allowed_paths": ["/search", "/_faults"]},
        },
        "rogue.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "/_faults" in str(exc.value)
    assert "narrow" in str(exc.value).lower()


def test_overlay_widening_allowed_actions_is_rejected(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {
            "extends": "member_savings_balance@1.0.0",
            "tenant": "rogue",
            "policy_overrides": {"allowed_actions": ["navigate", "click", "fill", "extract", "select"]},
        },
        "rogue.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "select" in str(exc.value)


def test_overlay_narrowing_the_allowlist_is_allowed(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {
            "extends": "member_savings_balance@1.0.0",
            "tenant": "narrow",
            "policy_overrides": {"allowed_paths": ["/", "/search", "/search/results", "/member/10001"]},
        },
        "narrow.json",
    )
    resolved = apply_overlay(base_artifact, load_overlay(overlay_path))
    assert resolved.policy.allowed_paths == ["/", "/search", "/search/results", "/member/10001"]


@pytest.mark.parametrize(
    "field,value",
    [("name", "account_ref"), ("type", "integer"), ("required", False), ("sensitivity", "public")],
)
def test_overlay_cannot_change_an_inputs_contract(tmp_path, field, value):
    """Pattern/description/example may vary per tenant. Anything that changes
    the parameter's public contract may not, and the error has to point at
    forking rather than leaving a generic 'extra field' message."""
    overlay = {
        "extends": "member_savings_balance@1.0.0",
        "tenant": "rogue",
        "input_overrides": {"member_ref": {field: value}},
    }
    with pytest.raises(OverlayError) as exc:
        load_overlay(write(tmp_path, overlay, "rogue.json"))
    message = str(exc.value)
    assert f"input_overrides.{field}" in message
    assert "fork" in message.lower()


@pytest.mark.parametrize("field,value", [("entry_path", "/elsewhere"), ("surface", "desktop")])
def test_overlay_cannot_change_flow_logic_via_target(tmp_path, field, value):
    overlay = {
        "extends": "member_savings_balance@1.0.0",
        "tenant": "rogue",
        "target_overrides": {field: value},
    }
    with pytest.raises(OverlayError) as exc:
        load_overlay(write(tmp_path, overlay, "rogue.json"))
    assert f"target_overrides.{field}" in str(exc.value)
    assert "fork" in str(exc.value).lower()


def test_overlay_cannot_introduce_a_new_input(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {
            "extends": "member_savings_balance@1.0.0",
            "tenant": "rogue",
            "input_overrides": {"branch_code": {"pattern": "^[0-9]{3}$"}},
        },
        "rogue.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "branch_code" in str(exc.value)


def test_overlay_cannot_introduce_a_new_element(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {
            "extends": "member_savings_balance@1.0.0",
            "tenant": "rogue",
            "element_overrides": {
                "brand_new": {
                    "chain": [
                        {"strategy": "role_name", "role": "button", "name": "X", "confidence": "high"}
                    ]
                }
            },
        },
        "rogue.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "brand_new" in str(exc.value)


def test_overlay_for_a_different_capability_is_rejected(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {"extends": "some_other_capability@1.0.0", "tenant": "cascade"},
        "mismatch.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "some_other_capability" in str(exc.value)


def test_overlay_for_a_different_version_is_rejected(tmp_path, base_artifact):
    overlay_path = write(
        tmp_path,
        {"extends": "member_savings_balance@2.0.0", "tenant": "cascade"},
        "mismatch.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "2.0.0" in str(exc.value)


def test_overlay_producing_an_invalid_artifact_is_rejected(tmp_path, base_artifact):
    """An override that is individually well-formed but breaks a cross-block
    rule must still fail -- here, a chain whose scope names an input that
    does not exist."""
    overlay_path = write(
        tmp_path,
        {
            "extends": "member_savings_balance@1.0.0",
            "tenant": "rogue",
            "element_overrides": {
                "results_view_link": {
                    "chain": [
                        {
                            "strategy": "role_name_scoped",
                            "role": "link",
                            "name": "View",
                            "scope": {"role": "row", "contains": "{{ghost_param}}"},
                            "confidence": "high",
                        }
                    ]
                }
            },
        },
        "rogue.json",
    )
    with pytest.raises(OverlayError) as exc:
        apply_overlay(base_artifact, load_overlay(overlay_path))
    assert "ghost_param" in str(exc.value)


def test_the_shipped_cascade_overlay_declares_only_permitted_keys():
    raw = json.loads(CASCADE_PATH.read_text(encoding="utf-8"))
    permitted = {
        "extends",
        "tenant",
        "input_overrides",
        "element_overrides",
        "target_overrides",
        "policy_overrides",
        "notes",
    }
    assert set(raw) <= permitted
