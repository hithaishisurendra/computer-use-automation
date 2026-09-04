"""The capability API.

The claims under test: an agent can find a capability, read its contract, and
invoke it by name with typed arguments -- and the guardrails, the result
contract and the redaction chokepoint all survive the new surface. That last
one is the point: an HTTP caller is not a more trusted destination than a
file, and a wrapper that quietly became a way around the engine would be the
easiest possible regression to ship.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import catalog as catalog_mod  # noqa: E402
from api.service import create_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(CAPABILITIES, evidence_root=tmp_path / "evidence"))


# ---------------------------------------------------------------------------
# The catalogue is a contract, not a file listing
# ---------------------------------------------------------------------------


def test_the_catalogue_lists_every_capability(client):
    body = client.get("/capabilities").json()
    ids = {c["id"] for c in body["capabilities"]}
    assert {"member_share_balance", "member_funds_transfer"} <= ids
    assert body["count"] == len(body["capabilities"])


def test_a_contract_describes_arguments_without_mentioning_the_ui():
    """A calling agent has no business knowing the capability is driven
    through a browser."""
    artifact = catalog_mod.load("member_funds_transfer", "1.0.0", root=CAPABILITIES)
    described = json.dumps(catalog_mod.describe(artifact)).lower()
    for leak in ("locator", "xpath", "selector", "role_name", "cell_in_row",
                 "click", "frame", "chain", "aria"):
        assert leak not in described, f"the contract leaks {leak!r}"


def test_a_contract_declares_typed_inputs_and_outputs(client):
    body = client.get("/capabilities/member_funds_transfer/1.0.0").json()
    names = {i["name"] for i in body["inputs"]}
    assert {"member_ref", "from_share", "to_share", "amount", "memo"} == names
    assert all("type" in i and "required" in i for i in body["inputs"])


def test_a_capability_with_an_irreversible_step_says_so_up_front(client):
    """An agent deciding whether to call something needs to know in advance
    that it cannot complete unattended -- finding out from a failed
    invocation is worse for the caller and worse for the audit trail."""
    risky = client.get("/capabilities/member_funds_transfer/1.0.0").json()
    safe = client.get("/capabilities/member_share_balance/1.0.0").json()
    assert risky["requires_human"] is True and risky["risky_steps"] == ["s11"]
    assert safe["requires_human"] is False and safe["risky_steps"] == []


def test_an_unknown_capability_is_a_404(client):
    assert client.get("/capabilities/no_such_thing/1.0.0").status_code == 404


def test_an_unloadable_artifact_appears_in_the_catalogue_with_its_error(tmp_path):
    """A catalogue that quietly shrinks is how you find out in production."""
    broken = tmp_path / "caps" / "broken"
    broken.mkdir(parents=True)
    (broken / "1.0.0.json").write_text('{"schema_version": "1.0"}')
    entries = catalog_mod.catalog(tmp_path / "caps")
    assert entries[0]["status"] == "unloadable"
    assert entries[0]["invocable"] is False and entries[0]["error"]


# ---------------------------------------------------------------------------
# Invocation: the result contract survives the HTTP boundary
# ---------------------------------------------------------------------------


def test_bad_arguments_are_a_400_with_the_violations(client):
    """A caller error is not a failed run: no browser was ever opened."""
    r = client.post("/capabilities/member_share_balance/1.0.0/invoke",
                    json={"inputs": {"member_ref": "not-an-id"}})
    assert r.status_code == 400
    body = r.json()
    assert body["classification"] == "caller_error"
    assert body["violations"][0]["code"] == "pattern_mismatch"


def test_a_missing_required_argument_is_a_400(client):
    r = client.post("/capabilities/member_share_balance/1.0.0/invoke", json={"inputs": {}})
    assert r.status_code == 400
    assert r.json()["violations"][0]["code"] == "missing_required"


def test_an_incomplete_recording_cannot_be_invoked(tmp_path):
    """A capability whose flow never completed is a record for a human to
    finish, not something to run."""
    src = json.loads((CAPABILITIES / "member_share_balance" / "1.0.0.json").read_text())
    src["provenance"]["flow_completed"] = False
    d = tmp_path / "caps" / "member_share_balance"
    d.mkdir(parents=True)
    (d / "1.0.0.json").write_text(json.dumps(src))
    client = TestClient(create_app(tmp_path / "caps", evidence_root=tmp_path / "ev"))
    r = client.post("/capabilities/member_share_balance/1.0.0/invoke",
                    json={"inputs": {"member_ref": "100234"}})
    assert r.status_code == 409
    assert "did not complete" in r.json()["message"]


def test_status_codes_separate_answers_from_failures():
    """The mapping is the result contract made visible over HTTP. A business
    outcome is 200 because the caller asked a question and got an answer."""
    from api.service import ESCALATION_REQUIRED, STATUS

    assert STATUS["success"] == 200
    assert STATUS["business_outcome"] == 200
    assert STATUS[ESCALATION_REQUIRED] == 202
    assert STATUS["caller_error"] == 400
    assert STATUS["hard_failure"] == 502 and STATUS["auth_failure"] == 502


# ---------------------------------------------------------------------------
# The guardrails came with it
# ---------------------------------------------------------------------------


def test_the_api_never_drives_a_page_itself():
    """Structural. Every invocation must go through ReplayEngine -- if this
    surface resolved elements or drove a browser, the guardrails would have a
    second implementation to be absent from."""
    import ast

    for source in sorted((REPO_ROOT / "api").glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imported = {
            n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
        }
        for banned in ("playwright", "perception.tree", "replay.resolver", "replay.executor"):
            assert not any(m.startswith(banned) for m in imported), (
                f"{source.name} imports {banned}: the API must be a wrapper, not an engine"
            )


def test_an_api_invocation_is_unattended_by_construction():
    """There is no operator behind an HTTP request, and offering one would be
    a lie the audit trail keeps."""
    source = (REPO_ROOT / "api" / "service.py").read_text()
    assert "escalate=False" in source


def test_every_response_goes_through_the_run_s_sink():
    source = (REPO_ROOT / "api" / "service.py").read_text()
    assert "engine.sink.payload(payload)" in source


def test_declared_sensitive_inputs_are_masked_in_the_response(client):
    """member_ref is declared `identifier`. A caller gets a correlatable
    suffix, not the value they sent back in full."""
    r = client.post("/capabilities/member_share_balance/1.0.0/invoke",
                    json={"inputs": {"member_ref": "not-an-id"}})
    assert "not-an-id" not in r.text


# ---------------------------------------------------------------------------
# Run history
# ---------------------------------------------------------------------------


def test_an_unknown_run_is_a_404(client):
    assert client.get("/runs/run_nope").status_code == 404


def test_healthz(client):
    assert client.get("/healthz").json() == {"status": "ok"}
