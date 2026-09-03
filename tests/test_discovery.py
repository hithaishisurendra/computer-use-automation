"""Tests for the discovery loop's recorder and its safety seams.

The model call itself is not tested here -- a genuine model-driven run is
evidence, not a unit test, and lives in evidence/discovery/. What *is* tested
is everything that decides whether a discovered run becomes a trustworthy
artifact: which locator rungs get recorded, what gets dropped, what becomes a
parameter, and that discovery cannot act outside the policy layer.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from capability.loader import load_artifact
from capability.profile import load_profile
from discovery.loop import Cycle, DiscoveryOutcome, _element_from_tool_input, _element_key
from discovery.prompts import ACTION_TOOLS, TERMINAL_TOOLS, TOOLS, build_system_prompt
from discovery.recorder import build_chain, record
from discovery.run import (
    DEFAULT_POLICY_PATH,
    PolicyWidened,
    build_target,
    default_policy_path,
    load_policy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


def node(role, name="", children=None, ref=None):
    return {
        "role": role,
        "name": name,
        "children": children or [],
        "ref": ref or f"r{abs(hash((role, name))) % 9999}",
    }


def find(tree, ref):
    stack = [tree]
    while stack:
        current = stack.pop()
        if current.get("ref") == ref:
            return current
        stack.extend(current.get("children") or [])
    return None


@pytest.fixture
def results_tree():
    def row(member_id, name, status):
        return node(
            "row",
            "",
            [
                node("cell", member_id),
                node("cell", name),
                node("cell", status),
                node("cell", "", [node("link", "View", ref=f"v_{member_id}")]),
            ],
        )

    grid = node(
        "table",
        "",
        [
            node(
                "row",
                "",
                [node("columnheader", "Member ID"), node("columnheader", "Name"), node("columnheader", "Status"), node("columnheader", "")],
            ),
            row("10001", "John Smith", "active"),
            row("10007", "Michael Davis", "closed"),
        ],
    )
    # The wrapper nesting CoreServ actually emits.
    return node("document", "", [node("table", "", [node("row", "", [node("cell", "", [grid])])])])


@pytest.fixture
def accounts_tree():
    return node(
        "document",
        "",
        [
            node(
                "table",
                "",
                [
                    node(
                        "row",
                        "",
                        [
                            node("columnheader", "Account Number"),
                            node("columnheader", "Type"),
                            node("columnheader", "Balance"),
                        ],
                    ),
                    node("row", "", [node("cell", "4471820019"), node("cell", "Checking"), node("cell", "2140.55", ref="chk")]),
                    node("row", "", [node("cell", "4471820020"), node("cell", "Savings"), node("cell", "8320.10", ref="sav")]),
                ],
            )
        ],
    )


def make_cycle(index, tool, tool_input, frames, ref=None, extracted=None, status="ok"):
    cycle = Cycle(
        index=index, url="/x", observation="", reasoning="", tool_name=tool, tool_input=tool_input, status=status
    )
    cycle.frames_before = frames
    if ref:
        cycle.acted_node = find(frames["content"], ref)
    cycle.extracted = extracted
    if tool != "navigate":
        cycle.element_key = _element_key(tool_input, tool)
    return cycle


# ---------------------------------------------------------------------------
# Closed action vocabulary
# ---------------------------------------------------------------------------


def test_tool_vocabulary_matches_the_artifact_actions_exactly():
    """Discovery cannot record an action replay cannot execute, because the
    two vocabularies are the same set."""
    from capability.schema import Action
    from typing import get_args

    assert ACTION_TOOLS == set(get_args(Action))


def test_every_declared_tool_is_action_or_terminal():
    declared = {t.name for t in TOOLS}
    assert declared == ACTION_TOOLS | TERMINAL_TOOLS


def test_terminal_tools_exist_so_finishing_is_explicit():
    """Without an explicit terminal call, 'the model finished' and 'the model
    stopped emitting tool calls' are indistinguishable."""
    assert TERMINAL_TOOLS == {"goal_reached", "stuck"}


def test_system_prompt_states_goal_allowlist_and_terminal_requirement():
    prompt = build_system_prompt(
        goal="Look up member 10001",
        base_url="http://localhost:8800",
        entry_path="/search",
        allowed_paths=["/search", "/member/*"],
        allowed_actions=["navigate", "click"],
    )
    assert "Look up member 10001" in prompt
    assert "/member/*" in prompt
    assert "goal_reached" in prompt and "stuck" in prompt
    assert "/_faults" not in prompt


def test_declared_policy_excludes_the_fault_control_endpoint():
    policy = load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None)
    assert "/_faults" not in policy.allowed_paths
    assert not any(p.startswith("/_") for p in policy.allowed_paths)


def test_allow_path_may_only_narrow_the_declared_policy():
    """An allowlist a caller can extend at invocation time is not an
    allowlist. --allow-path is held to the same narrowing rule as a tenant
    overlay, using the same predicate."""
    with pytest.raises(PolicyWidened) as exc:
        load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", ["/search", "/_faults"])
    assert "/_faults" in str(exc.value)
    assert "narrow" in str(exc.value).lower()


def test_allow_path_narrowing_is_accepted():
    policy = load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", ["/search", "/member/10001"])
    assert policy.allowed_paths == ["/search", "/member/10001"]


def test_allow_path_narrowing_uses_the_same_predicate_as_overlays():
    from capability.loader import widening_paths

    declared = load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None).allowed_paths
    assert widening_paths(declared, ["/member/10001"]) == []
    assert widening_paths(declared, ["/admin"]) == ["/admin"]


def test_omitting_allow_path_uses_the_declared_policy_unchanged():
    policy = load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None)
    assert "/member/*" in policy.allowed_paths
    assert "/search/results" in policy.allowed_paths


# ---------------------------------------------------------------------------
# Discovery is not allowed its own executor
# ---------------------------------------------------------------------------


def test_discovery_imports_the_replay_executor_rather_than_duplicating_it():
    source = (REPO_ROOT / "discovery" / "loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module
    }
    assert "replay.executor" in imported


def test_discovery_defines_no_policy_check_of_its_own():
    """A second policy implementation is how discovery quietly becomes more
    permissive than replay."""
    source = (REPO_ROOT / "discovery" / "loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert not {"check_destination", "check_action", "check_risk"} & defined


# ---------------------------------------------------------------------------
# Chain building
# ---------------------------------------------------------------------------


def test_ambiguous_strategy_is_never_recorded(results_tree):
    """Two rows both hold a link named 'View'. role_name resolves to both, so
    it must not appear in the chain at all."""
    target = find(results_tree, "v_10001")
    chain = build_chain(
        results_tree, target, {"role": "link", "name": "View", "row_contains": "10001"}, {"member_id": "10001"}
    )
    strategies = [r["strategy"] for r in chain]
    assert "role_name" not in strategies
    assert strategies[0] == "role_name_scoped"


def test_row_scope_is_parameterised_when_it_matches_an_input(results_tree):
    """'the row for {{member_id}}' generalises; 'the row for 10001' does not."""
    target = find(results_tree, "v_10001")
    chain = build_chain(
        results_tree, target, {"role": "link", "name": "View", "row_contains": "10001"}, {"member_id": "10001"}
    )
    scoped = next(r for r in chain if r["strategy"] == "role_name_scoped")
    assert scoped["scope"]["contains"] == "{{member_id}}"
    assert scoped["confidence"] == "high"


def test_ordinal_rung_is_last_and_always_brittle(results_tree):
    target = find(results_tree, "v_10001")
    chain = build_chain(
        results_tree, target, {"role": "link", "name": "View", "row_contains": "10001"}, {"member_id": "10001"}
    )
    assert chain[-1]["strategy"] == "role_ordinal"
    assert chain[-1]["brittle"] is True
    assert chain[-1]["confidence"] == "low"


def test_extraction_never_identifies_a_cell_by_the_value_it_reads(accounts_tree):
    """role_name(cell, "8320.10") resolves uniquely on the recorded page and is
    still wrong: it finds the balance only while the balance is unchanged.
    Uniqueness cannot catch this, so extraction suppresses name-based rungs."""
    target = find(accounts_tree, "sav")
    chain = build_chain(
        accounts_tree,
        target,
        {"role": "cell", "row_contains": "Savings", "column_header": "Balance"},
        {},
        is_extraction=True,
    )
    assert chain, "expected a usable chain"
    assert all(r.get("name") != "8320.10" for r in chain)
    assert all(r["strategy"] in ("cell_in_row", "role_ordinal") for r in chain)
    assert chain[0]["strategy"] == "cell_in_row"
    assert chain[0]["column_header"] == "Balance"


def test_every_recorded_rung_actually_resolves(results_tree):
    """The chain is measured, not asserted: replay must be able to use each
    rung the recorder wrote."""
    from capability.schema import Element, LocatorRung
    from replay import resolver

    target = find(results_tree, "v_10001")
    chain = build_chain(
        results_tree, target, {"role": "link", "name": "View", "row_contains": "10001"}, {"member_id": "10001"}
    )
    for rung in chain:
        element = Element(description="x", frame="content", chain=[LocatorRung(**rung)])
        resolution = resolver.resolve_element("x", element, {"content": results_tree}, {"member_id": "10001"})
        assert resolution.resolved, rung
        assert resolution.node is target, rung


# ---------------------------------------------------------------------------
# Recording a run
# ---------------------------------------------------------------------------


@pytest.fixture
def recorded(results_tree, accounts_tree):
    search = node(
        "document",
        "",
        [
            node(
                "table",
                "",
                [
                    node("row", "", [node("cell", "Member ID"), node("cell", "", [node("textbox", "Member ID", ref="t_id")])]),
                    node("row", "", [node("cell", "", [node("button", "Submit", ref="b_go")])]),
                ],
            )
        ],
    )
    cycles = [
        make_cycle(1, "navigate", {"path": "/search", "frame": "content"}, {"content": search}),
        make_cycle(2, "fill", {"frame": "content", "role": "textbox", "name": "Member ID", "value": "10001"}, {"content": search}, "t_id"),
        make_cycle(3, "click", {"frame": "content", "role": "button", "name": "Nope"}, {"content": search}, None, status="failed"),
        make_cycle(4, "click", {"frame": "content", "role": "button", "name": "Submit"}, {"content": search}, "b_go"),
        make_cycle(5, "click", {"frame": "content", "role": "link", "name": "View", "row_contains": "10001"}, {"content": results_tree}, "v_10001"),
        make_cycle(
            6,
            "extract",
            {"frame": "content", "role": "cell", "row_contains": "Savings", "column_header": "Balance", "output_name": "savings_balance", "output_type": "money"},
            {"content": accounts_tree},
            "sav",
            extracted="8320.10",
        ),
    ]
    outcome = DiscoveryOutcome(
        status="goal_reached",
        run_id="disc_test",
        goal="Look up member 10001 and read their current savings balance",
        cycles=cycles,
        outputs={"savings_balance": "8320.10"},
        steps_attempted=6,
    )
    return record(
        outcome,
        "member_savings_balance_discovered",
        "1.0.0",
        build_target("http://localhost:8800", "/search", "northridge", "4.2.1", load_profile("coreserv")),
        load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None),
        outcome.goal,
        "claude-sonnet-5",
    )


def test_recorded_artifact_loads_through_the_loader(recorded, tmp_path):
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(recorded), encoding="utf-8")
    artifact = load_artifact(path)
    assert artifact.capability.status == "draft"


def test_failed_cycles_are_dropped_from_the_recorded_path(recorded):
    assert len(recorded["steps"]) == 5
    assert recorded["provenance"]["steps_attempted"] == 6
    assert recorded["provenance"]["steps_recorded"] == 5


def test_literal_from_the_goal_becomes_a_typed_parameter(recorded):
    """The literal 10001 must not survive anywhere in the artifact as a value."""
    spec = recorded["inputs"][0]
    assert spec["name"] == "member_ref"
    assert spec["pattern"] == "^[0-9]{5}$"
    assert spec["sensitivity"] == "identifier"
    assert spec["example"] == "10001"

    fill = next(s for s in recorded["steps"] if s["action"] == "fill")
    assert fill["value"] == "{{member_ref}}"

    # The row scope must be parameterised too -- a scope pinned to the literal
    # would find "the row for 10001" no matter who the caller asked about.
    scopes = [
        rung["scope"]["contains"]
        for element in recorded["elements"].values()
        for rung in element["chain"]
        if rung.get("scope", {}).get("contains")
    ]
    assert "{{member_ref}}" in scopes
    assert "10001" not in scopes


def test_no_step_or_locator_hardcodes_the_discovered_literal(recorded):
    """Everything except `example` must be free of the value discovery ran
    with; otherwise the capability only works for the record it was found on."""
    steps_and_elements = json.dumps({"steps": recorded["steps"], "elements": recorded["elements"]})
    assert "10001" not in steps_and_elements


@pytest.mark.parametrize(
    "label,expected",
    [
        ("Member ID", "member_ref"),
        ("Account Number", "account_ref"),
        ("Member  Id", "member_ref"),
        ("Customer Code", "customer_ref"),
    ],
)
def test_identifier_parameters_are_named_after_the_entity_not_the_label(label, expected):
    """The label is the part that varies between tenants running the same
    product; the entity is not. Naming the parameter after the label would
    force a fork where an overlay should have sufficed."""
    from discovery.recorder import _parameter_name

    assert _parameter_name(label, is_identifier=True) == expected


def test_outputs_are_declared_with_the_type_the_model_chose(recorded):
    output = recorded["outputs"][0]
    assert output["name"] == "savings_balance"
    assert output["type"] == "money"


def test_status_is_always_draft(recorded):
    """Discovery does not approve its own output."""
    assert recorded["capability"]["status"] == "draft"


def test_provenance_records_the_model_and_run(recorded):
    provenance = recorded["provenance"]
    assert provenance["source"] == "discovery"
    assert provenance["model"] == "claude-sonnet-5"
    assert provenance["discovery_run_id"] == "disc_test"


def test_allowed_actions_narrowed_to_what_was_used(recorded):
    assert set(recorded["policy"]["allowed_actions"]) == {"navigate", "click", "fill", "extract"}


def test_checkpoints_are_synthesised_for_navigating_steps(recorded):
    for step in recorded["steps"]:
        if step["action"] in ("navigate", "click"):
            assert "checkpoint" in step, step["id"]
            assert step["checkpoint"]["type"] == "element_present"


def test_outcomes_are_empty_and_that_is_deliberate(recorded):
    """A happy-path run observes no business outcomes. Inventing them would be
    the artifact asserting something discovery never established."""
    assert recorded["outcomes"] == []
    assert "outcomes[] is empty" in recorded["provenance"]["notes"]


# ---------------------------------------------------------------------------
# Risk heuristic
#
# A first guess, not a determination. The tests assert both halves of that:
# that it fires on a plainly irreversible control, and that it declines to
# fire on the generic form-submit verb that would otherwise mark every
# read-only search as risky.
# ---------------------------------------------------------------------------


def _click_run(control_name, results_tree, next_url="/members/100234/transfer/post",
               risk_rules=None, log=None):
    """Record a one-click flow and return the resulting step."""
    from discovery.recorder import DEFAULT_RISK_RULES

    page = node(
        "document", "",
        [node("table", "", [node("row", "", [node("cell", "", [
            node("button", control_name, ref="b_act")])])])],
    )
    cycles = [
        make_cycle(1, "click", {"frame": "content", "role": "button", "name": control_name},
                   {"content": page}, "b_act"),
        # The observation that followed the click. Terminal cycles carry one
        # too, which is the only view of the page a final post produces.
        Cycle(index=2, url=next_url, observation="", reasoning="",
              tool_name="goal_reached", tool_input={}, status="terminal"),
    ]
    outcome = DiscoveryOutcome(
        status="goal_reached", run_id="disc_risk", goal="Move money for member 100234",
        cycles=cycles, steps_attempted=2,
    )
    artifact = record(
        outcome, "cap", "1.0.0",
        build_target("http://localhost:8800", "/start", "northridge", "4.2.1", load_profile("coreserv")),
        load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None),
        outcome.goal, "claude-sonnet-5",
        risk_rules=risk_rules or DEFAULT_RISK_RULES,
        log=log,
    )
    # steps[0] is the opening navigate the recorder always adds.
    return artifact, next(s for s in artifact["steps"] if s["action"] == "click")


def test_recorder_marks_a_post_transfer_click_as_risky(results_tree):
    artifact, step = _click_run("Post Transfer", results_tree)
    assert step["risk"] == "risky"
    assert "Post" in step["notes"]
    assert "heuristic first guess" in step["notes"]
    assert "risky" in artifact["provenance"]["notes"]


def test_recorder_marks_a_view_click_as_safe(results_tree):
    _, step = _click_run("View", results_tree)
    assert step["risk"] == "safe"
    assert "notes" not in step


def test_submit_is_a_near_miss_not_a_commit(results_tree):
    """Submit sends any form, including a search. Treating it as a commit
    would mark read-only lookups risky and block them under
    require_confirmation -- so it is recorded as considered-and-rejected
    rather than silently ignored."""
    artifact, step = _click_run("Submit", results_tree)
    assert step["risk"] == "safe"
    assert "near-miss" in artifact["provenance"]["notes"]
    assert "Submit" in artifact["provenance"]["notes"]


def test_risk_decisions_and_near_misses_are_logged(results_tree):
    events = []
    _click_run("Post Transfer", results_tree, log=lambda e, p: events.append((e, p)))
    _click_run("Submit", results_tree, log=lambda e, p: events.append((e, p)))

    decisions = [p for e, p in events if e == "risk_classified"]
    assert {d["decision"] for d in decisions} == {"risky", "safe"}
    assert any(d.get("matched_verb") == "Post" for d in decisions)
    assert any(d.get("near_miss_verb") == "Submit" for d in decisions)


def test_verb_vocabulary_comes_from_the_app_profile(results_tree):
    """Which words mean commit is per-app knowledge, so it is configuration."""
    from capability.profile import load_profile
    from discovery.recorder import RiskRules, risk_rules_from_profile

    default = risk_rules_from_profile(load_profile("coreserv"))
    assert "Post" in default.post_like_verbs
    assert "Submit" in default.near_miss_verbs

    _, step = _click_run(
        "Submit", results_tree,
        risk_rules=RiskRules(app="other", post_like_verbs=("Submit",)),
    )
    assert step["risk"] == "risky", "an app where Submit commits can say so"


def test_verb_matching_is_whole_word(results_tree):
    """A substring test would match Post inside 'Postal Address'."""
    _, step = _click_run("Edit Postal Address", results_tree)
    assert step["risk"] == "safe"


def test_a_terminal_risky_click_still_gets_a_checkpoint(results_tree):
    """The recorder's usual rule asserts the NEXT step's control, and a post
    has no next step. Without a fallback the recorder would emit an artifact
    that fails validation."""
    artifact, step = _click_run("Post Transfer", results_tree)
    assert step["checkpoint"]["type"] == "url_matches"
    assert "transfer/post" in step["checkpoint"]["pattern"]


def test_the_terminal_checkpoint_is_parameterised(results_tree):
    """The URL observed during discovery names one member; the checkpoint has
    to travel to the next member the capability is invoked for."""
    page = node("document", "", [node("table", "", [node("row", "", [
        node("cell", "", [node("textbox", "Member ID", ref="t_id")]),
        node("cell", "", [node("button", "Post Transfer", ref="b_act")])])])])
    cycles = [
        make_cycle(1, "fill", {"frame": "content", "role": "textbox",
                               "name": "Member ID", "value": "100234"},
                   {"content": page}, "t_id"),
        make_cycle(2, "click", {"frame": "content", "role": "button",
                                "name": "Post Transfer"}, {"content": page}, "b_act"),
        Cycle(index=3, url="/members/100234/transfer/post", observation="", reasoning="",
              tool_name="goal_reached", tool_input={}, status="terminal"),
    ]
    outcome = DiscoveryOutcome(
        status="goal_reached", run_id="d", goal="Transfer funds for member 100234",
        cycles=cycles, steps_attempted=3,
    )
    artifact = record(
        outcome, "cap", "1.0.0",
        build_target("http://localhost:8800", "/start", "northridge", "4.2.1", load_profile("coreserv")),
        load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None),
        outcome.goal, "m",
    )
    step = next(s for s in artifact["steps"] if s["action"] == "click")
    pattern = step["checkpoint"]["pattern"]
    assert "100234" not in pattern, "the discovered member must not be pinned"
    assert re.search(pattern, "/members/999999/transfer/post")


def test_an_artifact_with_a_risky_step_still_loads(results_tree, tmp_path):
    """The whole point of the checkpoint fallback: the schema now rejects a
    risky step without one, so the recorder has to satisfy its own rule."""
    artifact, step = _click_run("Post Transfer", results_tree)
    assert step["risk"] == "risky"
    path = tmp_path / "a.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    loaded = load_artifact(path)
    assert any(s.risk == "risky" and s.checkpoint is not None for s in loaded.steps)


def test_no_private_key_survives_into_the_artifact(results_tree):
    """_after_url is bookkeeping; the schema forbids unknown keys."""
    artifact, _ = _click_run("Post Transfer", results_tree)
    assert all("_after_url" not in s for s in artifact["steps"])


def test_recording_a_failed_run_is_refused():
    outcome = DiscoveryOutcome(status="stuck", run_id="x", goal="g", cycles=[])
    with pytest.raises(ValueError):
        record(outcome, "x", "1.0.0", None, None, "g", "claude-sonnet-5")


# ---------------------------------------------------------------------------
# Provisional element construction
# ---------------------------------------------------------------------------


def test_row_contains_selects_the_scoped_strategy():
    element = _element_from_tool_input(
        {"frame": "content", "role": "link", "name": "View", "row_contains": "10001"}, "click"
    )
    assert element.chain[0].strategy == "role_name_scoped"
    assert element.chain[0].scope.contains == "10001"


def test_column_header_selects_cell_in_row():
    element = _element_from_tool_input(
        {"frame": "content", "role": "cell", "row_contains": "Savings", "column_header": "Balance"}, "extract"
    )
    assert element.chain[0].strategy == "cell_in_row"
    assert element.chain[0].column_header == "Balance"


# ---------------------------------------------------------------------------
# Provider seam
# ---------------------------------------------------------------------------

PROVIDER_MODULES = {
    "anthropic",
    "google",
    "google.genai",
    "openai",
    "cohere",
    "mistralai",
    "litellm",
}


def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_recorder_imports_no_provider_client():
    """The recorder turns a run into an artifact. Which model produced the run
    is not its business, and an import would make it its business."""
    imported = _imported_modules(REPO_ROOT / "discovery" / "recorder.py")
    offenders = {n for n in imported if n.split(".")[0] in PROVIDER_MODULES}
    assert not offenders, f"recorder imports provider client(s): {offenders}"


@pytest.mark.parametrize(
    "module",
    ["discovery/recorder.py", "discovery/prompts.py", "discovery/loop.py"],
)
def test_only_model_py_names_a_provider_sdk(module):
    """Every provider import is confined to discovery/model.py, which is what
    makes the provider a config change rather than a code change."""
    imported = _imported_modules(REPO_ROOT / module)
    offenders = {n for n in imported if n.split(".")[0] in PROVIDER_MODULES}
    assert not offenders, f"{module} imports provider client(s): {offenders}"


@pytest.mark.parametrize("package", ["capability", "replay", "perception"])
def test_downstream_packages_never_import_a_provider(package):
    """Nothing downstream of discovery changed when the provider changed."""
    offenders = {}
    for source in sorted((REPO_ROOT / package).glob("*.py")):
        found = {n for n in _imported_modules(source) if n.split(".")[0] in PROVIDER_MODULES}
        if found:
            offenders[source.name] = sorted(found)
    assert not offenders


def test_both_providers_are_registered_behind_the_interface():
    from discovery.model import PROVIDERS, AnthropicClient, GeminiClient, ModelClient

    assert set(PROVIDERS) == {"anthropic", "gemini"}
    for client in PROVIDERS.values():
        assert issubclass(client, ModelClient)
    assert PROVIDERS["anthropic"] is AnthropicClient
    assert PROVIDERS["gemini"] is GeminiClient


def test_interface_has_exactly_one_abstract_method():
    """'A single method that takes messages plus tool definitions and returns
    tool calls' -- if the interface grows, the seam has leaked."""
    from discovery.model import ModelClient

    assert ModelClient.__abstractmethods__ == frozenset({"complete"})


def test_tools_are_provider_neutral_json_schema():
    from discovery.model import ToolSpec

    assert all(isinstance(t, ToolSpec) for t in TOOLS)
    for tool in TOOLS:
        assert tool.parameters["type"] == "object"
        assert "properties" in tool.parameters
        # Neither provider's wire vocabulary may appear in the neutral spec.
        assert "input_schema" not in tool.parameters
        assert "function_declarations" not in tool.parameters


def test_unknown_provider_is_refused():
    from discovery.model import build_client

    with pytest.raises(ValueError) as exc:
        build_client("definitely-not-a-provider")
    assert "unknown provider" in str(exc.value)


def test_both_clients_translate_the_same_transcript():
    """The neutral transcript must survive two genuinely different wire
    formats. This is what the second implementation exists to prove."""
    from discovery.model import (
        ActionResult,
        AnthropicClient,
        AssistantAction,
        GeminiClient,
        Observation,
    )

    transcript = [
        Observation(text="link \"View\" present"),
        AssistantAction(call_id="c1", name="click", arguments={"frame": "content"}, text="clicking"),
        ActionResult(call_id="c1", name="click", text="clicked", is_error=False),
    ]

    anthropic_wire = AnthropicClient._to_wire(object.__new__(AnthropicClient), transcript)
    assert [m["role"] for m in anthropic_wire] == ["user", "assistant", "user"]
    assert anthropic_wire[1]["content"][-1]["type"] == "tool_use"
    assert anthropic_wire[2]["content"][0]["type"] == "tool_result"

    gemini = object.__new__(GeminiClient)
    from google.genai import types as genai_types

    gemini._types = genai_types
    gemini_wire = gemini._to_wire(transcript)
    assert [c.role for c in gemini_wire] == ["user", "model", "user"]
    assert gemini_wire[1].parts[-1].function_call.name == "click"
    assert gemini_wire[2].parts[0].function_response.name == "click"


def test_backoff_retries_rate_limits_and_logs_every_one():
    """Free-tier rate limiting is the normal case, so retries must be both
    bounded and visible in evidence."""
    from discovery.model import ModelClient, RateLimited

    class Flaky(ModelClient):
        provider = "test"

        def complete(self, system, messages, tools):
            raise NotImplementedError

    logged: list[dict] = []
    client = Flaky("m", on_retry=logged.append)
    attempts = {"n": 0}

    def call():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RateLimited("429 RESOURCE_EXHAUSTED")
        return "ok"

    import discovery.model as model_module

    original, model_module.BASE_BACKOFF_S = model_module.BASE_BACKOFF_S, 0.01
    try:
        assert client._with_backoff(call) == "ok"
    finally:
        model_module.BASE_BACKOFF_S = original

    assert attempts["n"] == 3
    assert len(logged) == 2
    assert [e["attempt"] for e in logged] == [1, 2]
    assert all(e["provider"] == "test" for e in logged)
    assert all("429" in e["reason"] for e in logged)
    # Exponential, not fixed: the second wait is longer than the first.
    assert logged[1]["sleep_s"] > logged[0]["sleep_s"]


def test_backoff_gives_up_rather_than_hanging():
    from discovery.model import MAX_RETRIES, ModelClient, RateLimited

    class Always(ModelClient):
        provider = "test"

        def complete(self, system, messages, tools):
            raise NotImplementedError

    logged: list[dict] = []
    client = Always("m", on_retry=logged.append)

    import discovery.model as model_module

    original, model_module.BASE_BACKOFF_S = model_module.BASE_BACKOFF_S, 0.001
    try:
        with pytest.raises(RateLimited):
            client._with_backoff(lambda: (_ for _ in ()).throw(RateLimited("429")))
    finally:
        model_module.BASE_BACKOFF_S = original

    assert len(logged) == MAX_RETRIES


# ---------------------------------------------------------------------------
# Capability id derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("Look up member 10001 and read their current savings balance", "member_savings_balance"),
        ("Look up member 99999 and read their current savings balance", "member_savings_balance"),
        ("Find account 4471820019 balance", "account_balance"),
        # Word order follows the goal; reordering would be arbitrary.
        ("Retrieve the current balance for member 12345", "balance_member"),
    ],
)
def test_capability_id_drops_the_record_it_was_discovered_on(goal, expected):
    """The flow is not about the member the goal happened to name -- the
    parameter carries that. Two runs differing only in member id must derive
    the same capability, or every invocation looks like a different one."""
    from discovery.run import _derive_capability_id

    assert _derive_capability_id(goal) == expected


def test_capability_id_is_stable_across_different_members():
    from discovery.run import _derive_capability_id

    a = _derive_capability_id("Look up member 10001 and read their current savings balance")
    b = _derive_capability_id("Look up member 10003 and read their current savings balance")
    assert a == b


def test_capability_id_never_contains_a_digit():
    from discovery.run import _derive_capability_id

    derived = _derive_capability_id("Open sub-account 55 for member 10001 at branch 7")
    assert not any(c.isdigit() for c in derived)


# ---------------------------------------------------------------------------
# Opening navigate step
# ---------------------------------------------------------------------------


def _record_without_navigate(results_tree, accounts_tree, entry="/search"):
    """A session already sitting on the entry page: the model never navigates."""
    search = node(
        "document",
        "",
        [
            node(
                "table",
                "",
                [
                    node("row", "", [node("cell", "Member ID"), node("cell", "", [node("textbox", "Member ID", ref="t_id")])]),
                    node("row", "", [node("cell", "", [node("button", "Submit", ref="b_go")])]),
                ],
            )
        ],
    )
    cycles = [
        make_cycle(1, "fill", {"frame": "content", "role": "textbox", "name": "Member ID", "value": "10001"}, {"content": search}, "t_id"),
        make_cycle(2, "click", {"frame": "content", "role": "button", "name": "Submit"}, {"content": search}, "b_go"),
        make_cycle(3, "click", {"frame": "content", "role": "link", "name": "View", "row_contains": "10001"}, {"content": results_tree}, "v_10001"),
        make_cycle(
            4,
            "extract",
            {"frame": "content", "role": "cell", "row_contains": "Savings", "column_header": "Balance", "output_name": "savings_balance", "output_type": "money"},
            {"content": accounts_tree},
            "sav",
            extracted="8320.10",
        ),
    ]
    # The session is already on the entry page -- the coincidence that made
    # the original artifact replay by luck.
    for cycle in cycles:
        cycle.url = f"http://localhost:8800{entry}"

    outcome = DiscoveryOutcome(
        status="goal_reached",
        run_id="disc_nonav",
        goal="Look up member 10001 and read their current savings balance",
        cycles=cycles,
        steps_attempted=4,
    )
    return record(
        outcome,
        "member_savings_balance",
        "1.0.0",
        build_target("http://localhost:8800", entry, "northridge", "4.2.1", load_profile("coreserv")),
        load_policy(DEFAULT_POLICY_PATH, "http://localhost:8800", None),
        outcome.goal,
        "gemini:gemini-3.5-flash",
    )


def test_artifact_from_a_session_already_on_the_entry_page_still_opens_with_navigate(
    results_tree, accounts_tree
):
    """The model correctly never navigated -- it was already there. The
    artifact must still declare where it starts, rather than inheriting that
    from wherever the surface happened to open."""
    artifact = _record_without_navigate(results_tree, accounts_tree)

    first = artifact["steps"][0]
    assert first["action"] == "navigate"
    assert first["path"] == "/search"
    assert first["frame"] == "content"
    assert first["notes"]


def test_opening_navigate_is_added_even_though_no_navigation_was_observed(
    results_tree, accounts_tree
):
    """steps_recorded counts the added precondition, and provenance stays
    honest about the run having produced four actions."""
    artifact = _record_without_navigate(results_tree, accounts_tree)

    assert artifact["provenance"]["steps_attempted"] == 4
    assert artifact["provenance"]["steps_recorded"] == 5
    assert [s["action"] for s in artifact["steps"]] == ["navigate", "fill", "click", "click", "extract"]


def test_step_ids_are_renumbered_after_the_insertion(results_tree, accounts_tree):
    artifact = _record_without_navigate(results_tree, accounts_tree)
    assert [s["id"] for s in artifact["steps"]] == ["s1", "s2", "s3", "s4", "s5"]


def test_opening_navigate_targets_entry_path_not_wherever_the_session_sat(
    results_tree, accounts_tree
):
    """If the frameset default and entry_path diverge, the artifact must
    follow entry_path -- that divergence is the bug this guards."""
    artifact = _record_without_navigate(results_tree, accounts_tree, entry="/search")
    assert artifact["steps"][0]["path"] == "/search"

    artifact = _record_without_navigate(results_tree, accounts_tree, entry="/member/lookup")
    assert artifact["steps"][0]["path"] == "/member/lookup"


def test_no_duplicate_navigate_when_the_model_already_navigated(recorded):
    """The earlier fixture's model did navigate to /search itself; the
    recorder must not prepend a second one."""
    navigates = [s for s in recorded["steps"] if s["action"] == "navigate"]
    assert len(navigates) == 1
    assert recorded["steps"][0]["action"] == "navigate"


def test_artifact_with_added_navigate_still_loads(results_tree, accounts_tree, tmp_path):
    artifact = _record_without_navigate(results_tree, accounts_tree)
    path = tmp_path / "artifact.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    loaded = load_artifact(path)
    assert loaded.steps[0].action == "navigate"
    assert "navigate" in loaded.policy.allowed_actions


# ---------------------------------------------------------------------------
# Findings from the first MERIDIAN recording
# ---------------------------------------------------------------------------


def test_chains_are_built_against_the_frameless_document_tree():
    """The bug that made the first MERIDIAN recording emit one step and no
    elements: snapshots are keyed by frame NAME, the main frame's name is the
    empty string, and an element declaring no frame is None -- so the
    recorder's `.get(None)` missed and every chain was probed against a tree
    of None."""
    page = node("document", "", [node("table", "", [node("row", "", [
        node("cell", "", [node("button", "Search", ref="b")])])])])
    cycles = [
        make_cycle(1, "click", {"role": "button", "name": "Search"}, {"": page}, None),
        Cycle(index=2, url="/members", observation="", reasoning="",
              tool_name="goal_reached", tool_input={}, status="terminal"),
    ]
    cycles[0].acted_node = find(page, "b")
    cycles[0].element_key = "search_button"
    outcome = DiscoveryOutcome(status="goal_reached", run_id="d", goal="g",
                               cycles=cycles, steps_attempted=2)
    artifact = record(
        outcome, "cap", "1.0.0",
        build_target("https://x.test", "/menu", "demo", "1.0.0", load_profile("meridian")),
        load_policy(default_policy_path("meridian"), "https://x.test", None),
        "g", "m", default_frame=None,
    )
    assert artifact["elements"], "no element was recordable; the tree lookup missed"
    assert artifact["elements"]["search_button"]["frame"] is None
    assert "Unrecordable" not in artifact["provenance"]["notes"]


def test_a_row_scope_is_parameterised_inside_a_compound_identifier():
    """A share id of 100234-S0001 has to become {{member_ref}}-S0001 or the
    capability only ever works for the member it was discovered on. Whole
    string comparison would have left it literal."""
    from discovery.recorder import _parameterise

    assert _parameterise("100234-S0001", {"member_ref": "100234"}) == "{{member_ref}}-S0001"
    assert _parameterise("Regular Shares", {"member_ref": "100234"}) is None


def test_an_extraction_is_never_scoped_on_the_value_it_reads():
    """Observed live: after an ambiguous scope failed, the model retried with
    row_contains set to the balance itself. That resolves during discovery and
    is circular -- it finds the balance only while the balance is unchanged."""
    row = node("row", "", [node("cell", "100234-S0001"), node("cell", "$2,499.00", ref="bal")])
    tree = node("table", "", [node("rowgroup", "", [
        node("row", "", [node("cell", "Share ID"), node("cell", "Balance")]), row])])
    target = find(tree, "bal")
    chain = build_chain(tree, target, {"row_contains": "2,499.00", "column_header": "Balance"},
                        {"member_ref": "100234"}, is_extraction=True)
    for rung in chain:
        scope = rung.get("scope") or {}
        assert "2,499.00" not in str(scope.get("contains") or "")
        assert "2,499.00" not in str(scope.get("cell_equals") or "")


def test_an_output_is_not_named_after_the_record_it_was_discovered_on():
    """balance_100234_s0001 reads as a different output for every member, and
    an output name is the contract a calling agent binds to."""
    from discovery.recorder import _output_name

    assert _output_name("balance_100234_s0001", set()) == "balance"
    assert _output_name("share_balance", set()) == "share_balance"
    # A collision would lose a value, so the model's name survives instead.
    assert _output_name("balance_100234", {"balance"}) == "balance_100234"


def test_an_ambiguous_target_is_reported_as_ambiguous_not_missing():
    """The two arrive as one exception, and the advice differs completely. A
    model told to 'target something that is actually present' re-sends the
    same ambiguous target, because from where it sits the target is correct."""
    from discovery.loop import _unresolvable_advice
    from replay.resolver import ElementUnresolvable, Resolution, RungAttempt

    ambiguous = Resolution(element_key="x", resolved=False, attempts=[
        RungAttempt(0, "cell_in_row", "high", False, 9, "ambiguous")])
    advice = _unresolvable_advice(ElementUnresolvable(ambiguous))
    assert "AMBIGUOUS" in advice and "9" in advice and "Narrow it" in advice

    missing = Resolution(element_key="x", resolved=False, attempts=[
        RungAttempt(0, "role_name", "high", False, 0, "no_match")])
    assert "AMBIGUOUS" not in _unresolvable_advice(ElementUnresolvable(missing))


# ---------------------------------------------------------------------------
# The discovery risk gate
#
# Shared code was not shared enforcement: the executor's risk gate reads
# Step.risk, and discovery built every Step without one, so check_risk never
# fired. Observed live -- a run clicked "Post Transfer" and moved money.
# ---------------------------------------------------------------------------


def test_discovery_classifies_risk_with_the_same_rules_as_the_recorder():
    from capability.profile import load_profile
    from discovery.recorder import risk_rules_from_profile

    rules = risk_rules_from_profile(load_profile("meridian"))
    assert rules.match("Post Transfer") == "Post"
    assert rules.match("Search") is None
    # A fill or select is not the commit; the click that sends it is.
    assert rules.match("Amount") is None


def test_a_step_discovery_performs_is_a_step_the_artifact_calls_safe():
    """The invariant the gate exists to hold: what discovery was willing to
    do and what the artifact says is risky must be the same judgement."""
    from capability.profile import load_profile
    from discovery.loop import DiscoveryLoop
    from discovery.recorder import risk_rules_from_profile

    profile = load_profile("meridian")
    rules = risk_rules_from_profile(profile)
    loop = DiscoveryLoop.__new__(DiscoveryLoop)
    loop.risk_rules = rules
    loop.log = lambda *a, **k: None

    for name, expected in [("Post Transfer", "risky"), ("Search", "safe"),
                           ("Select", "safe"), ("Save Changes", "risky")]:
        assert loop._classify_risk("click", {"name": name}, None) == expected
        assert loop._classify_risk("fill", {"name": name}, None) == "safe"


def test_a_risk_blocked_run_is_recordable_but_not_successful():
    """The gate must not make irreversible capabilities unrecordable -- the
    safe thing would then be the thing that makes the system useless."""
    outcome = DiscoveryOutcome(status="risk_blocked", run_id="d", goal="g",
                               cycles=[], steps_attempted=3)
    assert outcome.recordable and not outcome.succeeded
    assert DiscoveryOutcome(status="stuck", run_id="d", goal="g", cycles=[]).recordable is False


def test_the_blocked_step_is_recorded_but_marked_never_executed(results_tree):
    page = node("document", "", [node("table", "", [node("row", "", [
        node("cell", "", [node("textbox", "Amount", ref="amt")]),
        node("cell", "", [node("button", "Post Transfer", ref="post")])])])])
    filled = make_cycle(1, "fill", {"role": "textbox", "name": "Amount", "value": "5.00"},
                        {"": page}, None)
    filled.acted_node = find(page, "amt")
    filled.element_key = "amount_field"

    blocked = make_cycle(2, "click", {"role": "button", "name": "Post Transfer"},
                         {"": page}, None, status="blocked")
    blocked.acted_node = find(page, "post")
    blocked.element_key = "post_transfer_button"

    outcome = DiscoveryOutcome(
        status="risk_blocked", run_id="d",
        goal="Transfer 5.00 and reach the confirmation screen",
        cycles=[filled, blocked], steps_attempted=2, blocked_cycle=blocked)

    artifact = record(
        outcome, "cap", "1.0.0",
        build_target("https://x.test", "/menu", "demo", "1.0.0", load_profile("meridian")),
        load_policy(default_policy_path("meridian"), "https://x.test", None),
        outcome.goal, "m", default_frame=None,
        risk_rules=__import__("discovery.recorder", fromlist=["x"]).risk_rules_from_profile(
            load_profile("meridian")),
    )

    clicks = [s for s in artifact["steps"] if s["action"] == "click"]
    assert clicks and clicks[-1]["risk"] == "risky"
    assert artifact["capability"]["status"] == "draft"
    notes = artifact["provenance"]["notes"]
    assert "THE FLOW WAS NOT COMPLETED" in notes
    assert "NEVER EXECUTED" in notes


def test_a_completed_run_says_nothing_about_being_incomplete(recorded):
    assert "NOT COMPLETED" not in recorded["provenance"]["notes"]
