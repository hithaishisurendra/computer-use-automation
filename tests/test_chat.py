"""The chatbot.

A thin driver over the capability API standing in for the AI agent. Two claims
under test: it cannot reach past the API, and its reporting preserves the
result contract the rest of the system is careful about.

The second is the one that matters. A chat layer that renders everything
non-success as "sorry, something went wrong" throws away the distinction the
brief calls the most common design mistake -- and it does so in the surface a
demo viewer actually reads.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.chat import Chat, capability_tools, describe  # noqa: E402
from api.service import create_app  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"


class FakeClient:
    """Stands in for a provider. The model is not what is under test."""

    provider = "fake"
    model = "fake"

    def __init__(self, calls=None, text=""):
        self._calls = calls or []
        self._text = text
        self.seen: list = []

    def complete(self, system, messages, tools):
        from discovery.model import ModelResponse, ToolCall

        self.seen.append({"system": system, "messages": messages, "tools": tools})
        return ModelResponse(
            text=self._text,
            calls=[ToolCall(id="c1", name=n, arguments=a) for n, a in self._calls],
            usage={"input_tokens": 10, "output_tokens": 5},
        )


# ---------------------------------------------------------------------------
# The reporting
# ---------------------------------------------------------------------------


def test_success_states_the_outputs_plainly():
    reply = describe({"classification": "success",
                      "outputs": {"share_balance": "18015.00"}}, "c", {})
    assert "18015.00" in reply
    assert "share balance" in reply


def test_a_business_outcome_is_never_phrased_as_a_failure():
    """The load-bearing test. "No such member" is an ANSWER: the application
    was asked a question and gave one."""
    reply = describe({
        "classification": "business_outcome", "outcome": "member_not_found",
        "message": "No member exists with the supplied identifier."}, "c", {})
    assert reply == "No member exists with the supplied identifier."
    for failure_word in ("error", "fail", "sorry", "problem", "unable", "could not",
                         "went wrong", "unfortunately"):
        assert failure_word not in reply.lower(), failure_word


@pytest.mark.parametrize("message", [
    "No member exists with the supplied identifier.",
    "The source share is on hold and cannot be debited.",
    "The signed-on operator lacks the privilege for this function; a supervisor "
    "must perform it.",
    "The application rejected the request as invalid.",
])
def test_every_meridian_business_outcome_reads_as_an_answer(message):
    """Each declared outcome, through the reporter. A phrasing regression on
    any of them turns a legitimate result into an apparent crash."""
    reply = describe({"classification": "business_outcome", "message": message}, "c", {})
    assert reply == message
    assert not reply.lower().startswith(("sorry", "error", "failed"))


def test_escalation_says_which_step_and_what_a_human_must_do():
    reply = describe({
        "classification": "escalation_required",
        "escalation": {"step_id": "s11",
                       "expected_on_resume": "text 'TRANSFER POSTED' present"}}, "c", {})
    assert "s11" in reply
    assert "irreversible" in reply
    assert "TRANSFER POSTED" in reply
    # And it points at where the work is picked up.
    assert "Interventions" in reply
    # It must not claim the transfer happened.
    assert "done" not in reply.lower().split(".")[0]


def test_caller_error_says_what_was_wrong_with_the_input():
    reply = describe({
        "classification": "caller_error",
        "violations": [{"input": "member_ref", "code": "pattern_mismatch",
                        "message": "value ***pe does not match required pattern"}]}, "c", {})
    assert "member_ref" in reply and "pattern" in reply


def test_auth_failure_is_explicitly_not_the_user_s_fault():
    """Telling an operator to check their input when the system's own
    credentials are wrong sends them somewhere unhelpful."""
    reply = describe({"classification": "auth_failure",
                      "message": "unset environment variable(s) ['MERIDIAN_PASSWORD']"}, "c", {})
    assert "not anything you did" in reply
    assert "system" in reply.lower()


def test_hard_failure_reports_step_expected_and_observed():
    reply = describe({
        "classification": "hard_failure", "run_id": "run_abc",
        "failure": {"step_id": "s4", "expected": "element 'select_link' present",
                    "observed": "not found"}}, "c", {})
    assert "s4" in reply and "select_link" in reply and "not found" in reply
    assert "run_abc" in reply


def test_the_classifications_are_all_covered():
    """A classification nobody wrote a branch for falls through to the
    hard-failure wording, which would describe a success as a failure."""
    from api.service import ESCALATION_REQUIRED, STATUS

    message = "The application said something specific."
    for classification in list(STATUS) + [ESCALATION_REQUIRED]:
        reply = describe({"classification": classification, "message": message,
                          "outputs": {}}, "c", {})
        assert reply and reply.strip(), classification
        # A branch that fell through to the hard-failure wording would
        # describe a success as a failure.
        if classification in ("success", "business_outcome"):
            assert "failed at step" not in reply, classification


# ---------------------------------------------------------------------------
# The mapping
# ---------------------------------------------------------------------------


def test_the_tools_are_built_from_the_catalogue():
    from api import catalog as catalog_mod

    tools = capability_tools(catalog_mod.catalog(CAPABILITIES))
    names = {t.name for t in tools}
    assert "member_share_balance" in names

    transfer = next(t for t in tools if t.name == "member_funds_transfer")
    assert set(transfer.parameters["properties"]) == {
        "member_ref", "from_share", "to_share", "amount", "memo"}
    # Facts that change whether a caller should pick it at all.
    assert "irreversible" in transfer.description


def test_a_capability_needing_a_supervisor_says_so_in_its_tool_description():
    from api import catalog as catalog_mod

    tools = capability_tools(catalog_mod.catalog(CAPABILITIES))
    hold = next((t for t in tools if t.name == "member_place_hold"), None)
    assert hold is not None
    assert "supervisor" in hold.description.lower()


def test_an_uninvocable_capability_is_not_offered():
    """There is nothing to call, so the model must not see it."""
    tools = capability_tools([
        {"id": "broken", "invocable": False, "status": "unloadable"},
        {"id": "fine", "invocable": True, "inputs": [], "description": "d"},
    ])
    assert {t.name for t in tools} == {"fine"}


def test_declining_lists_what_is_available():
    """If the model cannot confidently map a request, saying so and listing
    the options beats guessing at a capability that moves money."""
    chat = Chat(client=FakeClient(calls=[], text="I don't have a capability for that."))
    result = chat.choose("delete everything", [
        {"id": "member_share_balance", "invocable": True, "inputs": [], "description": "d"}])
    assert "capability" not in result
    assert result["available"] == ["member_share_balance"]


def test_the_prompt_forbids_inventing_argument_values():
    from api.chat import SYSTEM_PROMPT

    assert "Never invent" in SYSTEM_PROMPT
    assert "call no tool" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# It cannot bypass anything
# ---------------------------------------------------------------------------


def test_the_chat_request_accepts_only_a_message():
    """A field naming the capability, its inputs or a policy would make this
    a second invoke path with its own chance to skip a check."""
    import ast

    tree = ast.parse((REPO_ROOT / "api" / "service.py").read_text(encoding="utf-8"))
    chat_model = next(n for n in ast.walk(tree)
                      if isinstance(n, ast.ClassDef) and n.name == "ChatRequest")
    fields = {n.target.id for n in chat_model.body if isinstance(n, ast.AnnAssign)}
    assert fields == {"message"}


def test_a_policy_override_on_the_chat_endpoint_is_refused(tmp_path):
    client = TestClient(create_app(CAPABILITIES, evidence_root=tmp_path / "ev"))
    r = client.post("/chat", json={"message": "hello",
                                   "policy": {"allowed_origins": ["http://evil.test"]}})
    assert r.status_code == 422


def test_the_chat_module_never_reaches_the_engine():
    """Structural. It is an API caller; a path here that loaded an artifact or
    drove a page would be a second way to reach the engine."""
    import ast

    tree = ast.parse((REPO_ROOT / "api" / "chat.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    for banned in ("playwright", "replay", "perception", "capability", "escalation"):
        assert not any(m.split(".")[0] == banned for m in imported), banned


def test_the_chat_endpoint_invokes_through_the_same_path_as_any_caller():
    source = (REPO_ROOT / "api" / "service.py").read_text()
    assert "invoked = invoke(" in source, (
        "chat must call the invoke endpoint rather than constructing an engine")
    assert "ReplayEngine(" not in source.split("def chat(")[1].split("@app.get")[0]


def test_only_one_model_client_exists():
    """The brief: use the existing provider interface, do not add a second."""
    import ast

    tree = ast.parse((REPO_ROOT / "api" / "chat.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    assert "discovery.model" in imported
    for sdk in ("anthropic", "openai", "google", "cohere"):
        assert not any(m.split(".")[0] == sdk for m in imported), sdk


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_the_chat_response_goes_through_a_sink(tmp_path):
    source = (REPO_ROOT / "api" / "service.py").read_text()
    chat_body = source.split("def chat(")[1].split("@app.get")[0]
    for returned in chat_body.split("JSONResponse(")[1:]:
        assert "sink" in returned.split(")")[0] or "null_sink()" in returned, (
            "a chat response body must go through the redaction sink")


def test_a_redacted_input_stays_redacted_in_the_reply(tmp_path):
    """What the chat surface renders comes from the API response, which was
    masked before it got there."""
    chat = Chat(client=FakeClient(calls=[("member_share_balance", {"member_ref": "100234"})]))
    result = chat.choose("balance for 100234", [
        {"id": "member_share_balance", "invocable": True, "description": "d",
         "inputs": [{"name": "member_ref", "type": "string", "required": True,
                     "description": "the member"}]}])
    # The chat layer passes the argument through; the API masks it on the way
    # back, which the live test confirms. Here: it does not add one of its own.
    assert result["inputs"] == {"member_ref": "100234"}
    reply = describe({"classification": "success", "inputs": {"member_ref": "****34"},
                      "outputs": {"share_balance": "56.00"}}, "member_share_balance", {})
    assert "100234" not in reply
