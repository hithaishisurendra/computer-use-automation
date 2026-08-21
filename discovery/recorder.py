"""Turn a successful discovery run into a capability artifact.

The artifact is not a transcript. Three things happen here that make it a
reusable capability rather than a macro recording:

1. **Only the path that worked is recorded.** Failed and retried actions are
   dropped. `provenance.steps_attempted` versus `steps_recorded` keeps the
   difference honest instead of hiding it.

2. **Fallback chains are measured, not assumed.** For each element the model
   acted on, every strategy is *tried against the page as it was at that
   moment*, and only strategies that resolve to exactly one element are
   recorded. A rung that would be ambiguous is never written -- recording it
   would hand replay a locator that silently picks the wrong row. This is why
   the chain is built here from the captured tree rather than from whatever
   the model happened to say.

3. **Literals become parameters.** A value the model typed that came from the
   goal is replaced with a `{{param}}` reference and declared as a typed
   input, so the capability is callable with a different member rather than
   hardcoded to the one it was discovered with.

Everything is emitted as `status: "draft"`. Discovery does not get to approve
its own output.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional

from capability.schema import SCHEMA_VERSION
from discovery.loop import Cycle, DiscoveryOutcome
from replay import resolver

# Strategy order: most robust first. role_name is preferred when it is
# unambiguous because it survives the row a record happens to occupy;
# role_ordinal is last and always brittle because it survives nothing.
STRATEGY_ORDER = ["role_name", "role_name_scoped", "cell_in_row", "role_ordinal"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_")


def _iter_with_parents(node: Optional[dict], parent: Optional[dict] = None):
    if node is None:
        return
    yield node, parent
    for child in node.get("children") or []:
        yield from _iter_with_parents(child, node)


def _ancestor_row(tree: Optional[dict], target: dict) -> Optional[dict]:
    """Innermost row containing the target node.

    Innermost matters for the same reason it does in the resolver: CoreServ
    nests tables, so several ancestor rows contain the node and only the
    closest one describes the record it belongs to.
    """
    parents: dict[int, dict] = {}
    for node, parent in _iter_with_parents(tree):
        if parent is not None:
            parents[id(node)] = parent

    current = target
    while id(current) in parents:
        current = parents[id(current)]
        if (current.get("role") or "").lower() == "row":
            return current
    return None


def _row_cells(row: dict) -> list[dict]:
    return [
        c
        for c in (row.get("children") or [])
        if (c.get("role") or "").lower() in ("cell", "columnheader", "rowheader")
    ]


def _column_header_for(tree: Optional[dict], row: dict, target: dict) -> Optional[str]:
    """The column header naming the target cell's position in its row."""
    cells = _row_cells(row)
    try:
        index = next(i for i, c in enumerate(cells) if c is target)
    except StopIteration:
        return None
    for candidate, _ in _iter_with_parents(tree):
        if (candidate.get("role") or "").lower() != "row":
            continue
        headers = _row_cells(candidate)
        if len(headers) > index and (headers[index].get("role") or "").lower() == "columnheader":
            return (headers[index].get("name") or "").strip() or None
    return None


def _resolves_uniquely(tree, rung_dict: dict, params: dict, target: dict) -> bool:
    """A candidate rung is only recorded if it finds exactly this element."""
    from capability.schema import LocatorRung

    try:
        rung = LocatorRung(**rung_dict)
        matches = resolver.match_rung(tree, rung, params)
    except Exception:
        return False
    return len(matches) == 1 and matches[0] is target


def build_chain(
    tree: Optional[dict],
    target: dict,
    declared: dict[str, Any],
    params: dict[str, Any],
    is_extraction: bool = False,
) -> list[dict[str, Any]]:
    """Probe every strategy against the live tree; keep the ones that work.

    `declared` is what the model said it was targeting (role/name/row_contains
    /column_header). It seeds the candidates but does not decide the outcome:
    each candidate has to actually resolve uniquely to be recorded.

    `is_extraction` suppresses every name-based strategy. For a cell being
    read, the accessible name *is the value* -- so `role_name(cell,
    "8320.10")` resolves uniquely during discovery and is nonetheless
    circular: it finds the balance only while the balance is still 8320.10,
    which is never true for the next member the capability is called with.
    Uniqueness alone cannot catch this, because such a rung is genuinely
    unique on the page it was recorded from. An extraction target has to be
    identified by its position within a content-matched row instead.
    """
    role = (target.get("role") or "").strip().lower()
    name = (target.get("name") or "").strip()
    candidates: list[tuple[str, dict[str, Any]]] = []

    if role and name and not is_extraction:
        candidates.append(
            ("role_name", {"strategy": "role_name", "role": role, "name": name, "confidence": "high"})
        )

    row = _ancestor_row(tree, target)
    scope_text = declared.get("row_contains")
    # Prefer a scope keyed on a parameter: "the row for {{member_ref}}"
    # generalises across invocations, where a literal from this one run does
    # not. This is the difference between a reusable capability and a
    # recording that only works for the record it was discovered on.
    param_scope = next(
        (f"{{{{{p}}}}}" for p, v in params.items() if scope_text and str(v) == str(scope_text)),
        None,
    )
    scope_value = param_scope or scope_text

    if row is not None and role and name and not is_extraction:
        for text in filter(None, [scope_value, scope_text]):
            candidates.append(
                (
                    "role_name_scoped",
                    {
                        "strategy": "role_name_scoped",
                        "role": role,
                        "name": name,
                        "scope": {"role": "row", "contains": text},
                        "confidence": "high" if text == param_scope else "medium",
                    },
                )
            )
            break

    if row is not None and role in ("cell", "columnheader", "rowheader"):
        header = declared.get("column_header") or _column_header_for(tree, row, target)
        row_text = scope_value or _row_scope_text(row, target)
        if row_text:
            if header:
                candidates.append(
                    (
                        "cell_in_row",
                        {
                            "strategy": "cell_in_row",
                            "scope": {"role": "row", "contains": row_text},
                            "column_header": header,
                            "confidence": "high",
                        },
                    )
                )
            cells = _row_cells(row)
            if target in cells:
                candidates.append(
                    (
                        "cell_in_row",
                        {
                            "strategy": "cell_in_row",
                            "scope": {"role": "row", "contains": row_text},
                            "column_index": cells.index(target),
                            "confidence": "medium",
                        },
                    )
                )

    if role:
        same_role = [n for n, _ in _iter_with_parents(tree) if (n.get("role") or "").lower() == role]
        if target in same_role:
            candidates.append(
                (
                    "role_ordinal",
                    {
                        "strategy": "role_ordinal",
                        "role": role,
                        "index": same_role.index(target),
                        "confidence": "low",
                        "brittle": True,
                        "notes": "Positional fallback; recorded only because it resolved uniquely.",
                    },
                )
            )

    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    for strategy in STRATEGY_ORDER:
        for candidate_strategy, rung in candidates:
            if candidate_strategy != strategy:
                continue
            fingerprint = repr(sorted(rung.items(), key=lambda kv: kv[0]))
            if fingerprint in seen:
                continue
            if _resolves_uniquely(tree, rung, params, target):
                seen.add(fingerprint)
                chain.append(rung)
    return chain


def _row_scope_text(row: dict, target: dict) -> Optional[str]:
    """A distinguishing text from the row, excluding the target's own value.

    Scoping a cell on its own contents would be circular -- the locator would
    only find the value it already knew.
    """
    for cell in _row_cells(row):
        if cell is target:
            continue
        text = (cell.get("name") or "").strip()
        if text:
            return text
    return None


# Tokens naming the *format* of an identifier rather than the entity it
# identifies. Stripped when deriving a parameter name -- see _infer_input.
# "account" is deliberately NOT here: it names an entity ("Account Number" ->
# account_ref), and treating it as a format word would strip the only
# meaningful part of the label.
_IDENTIFIER_TOKENS = {"id", "ids", "no", "num", "number", "code", "ref"}


def _parameter_name(label: str, is_identifier: bool) -> str:
    """Name a parameter after the entity, not after this tenant's label.

    A field labelled "Member ID" on one tenant is labelled "Account Number"
    on another running the same product, and the two search different
    columns. Naming the parameter `member_id` would bake one tenant's
    vocabulary into the capability's public contract, so every other tenant
    would need a fork rather than an overlay -- an overlay may specialise a
    parameter's pattern but never rename it.

    So an identifier field becomes `{entity}_ref`: "Member ID" -> member_ref,
    "Account Number" -> account_ref. The entity survives across tenants; the
    label does not. Non-identifier fields keep their label-derived name,
    where no such divergence applies.
    """
    slug = _slug(label) or "param"
    if not is_identifier:
        return slug

    parts = [p for p in slug.split("_") if p]
    entity = [p for p in parts if p not in _IDENTIFIER_TOKENS]
    stem = "_".join(entity) if entity else "_".join(parts)
    return f"{stem}_ref" if stem else "param_ref"


def _infer_input(value: str, label: str) -> dict[str, Any]:
    """Declare a typed input for a literal the model typed."""
    is_identifier = value.isdigit()
    spec: dict[str, Any] = {
        "name": _parameter_name(label, is_identifier),
        "type": "string",
        "required": True,
        "description": (
            f"The identifier used to locate the record, entered into {label!r}."
            if is_identifier
            else f"Value supplied for {label!r}."
        ),
        "example": value,
    }
    if is_identifier:
        spec["pattern"] = f"^[0-9]{{{len(value)}}}$"
        spec["sensitivity"] = "identifier"
    else:
        spec["pattern"] = None
        spec["sensitivity"] = "public"
    return {k: v for k, v in spec.items() if v is not None}


def record(
    outcome: DiscoveryOutcome,
    capability_id: str,
    version: str,
    target: Any,
    policy: Any,
    goal: str,
    model: str,
) -> dict[str, Any]:
    """Build the artifact dict from a successful discovery run."""
    if not outcome.succeeded:
        raise ValueError(f"cannot record an artifact from a {outcome.status!r} run")

    # Only the path that worked. Failed and retried cycles are dropped here,
    # which is the whole point of the artifact being separate from the
    # transcript.
    acted = [c for c in outcome.cycles if c.status == "ok" and c.tool_name]

    inputs: dict[str, dict[str, Any]] = {}
    for cycle in acted:
        if cycle.tool_name == "fill":
            value = cycle.tool_input.get("value", "")
            label = cycle.tool_input.get("name") or "value"
            if value and value in goal:
                spec = _infer_input(value, label)
                inputs[spec["name"]] = spec

    params = {spec["name"]: spec["example"] for spec in inputs.values()}

    elements: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    unrecordable: list[str] = []

    for position, cycle in enumerate(acted, start=1):
        step_id = f"s{position}"
        action = cycle.tool_name
        frame = cycle.tool_input.get("frame") or "content"

        if action == "navigate":
            steps.append(
                {
                    "id": step_id,
                    "action": "navigate",
                    "path": cycle.tool_input["path"],
                    "frame": frame,
                    "risk": "safe",
                }
            )
            continue

        tree = (cycle.frames_before or {}).get(frame)
        node = cycle.acted_node or _find_acted_node(cycle, tree)
        if node is None:
            unrecordable.append(f"{step_id}: could not re-locate the element that was acted on")
            continue

        chain = build_chain(tree, node, cycle.tool_input, params, is_extraction=(action == "extract"))
        if not chain:
            unrecordable.append(
                f"{step_id}: no strategy uniquely identified {cycle.element_key!r}"
            )
            continue

        key = cycle.element_key or f"element_{position}"
        elements[key] = {
            "description": (
                f"{cycle.tool_input.get('role') or 'element'} "
                f"{cycle.tool_input.get('name') or ''}".strip()
            ),
            "frame": frame,
            "chain": chain,
            "notes": "Chain built by probing which strategies uniquely resolved this element during discovery.",
        }

        step: dict[str, Any] = {
            "id": step_id,
            "action": action,
            "element": key,
            "risk": "safe",
        }
        if action in ("fill", "select"):
            value = cycle.tool_input.get("value", "")
            for param, example in params.items():
                if str(example) == str(value):
                    value = f"{{{{{param}}}}}"
                    break
            step["value"] = value
        if action == "extract":
            output_name = cycle.tool_input["output_name"]
            step["into"] = output_name
            outputs.append(
                {
                    "name": output_name,
                    "type": cycle.tool_input.get("output_type", "string"),
                    "required": True,
                    "description": f"Value read from {cycle.tool_input.get('name') or 'the page'}.",
                    "sensitivity": "public",
                }
            )
        steps.append(step)

    steps = _add_checkpoints(steps, elements)

    used_actions = sorted({s["action"] for s in steps})
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "capability": {
            "id": capability_id,
            "version": version,
            "name": goal if len(goal) < 80 else goal[:77] + "...",
            "description": goal,
            # Discovery never approves its own output. A human (or a replay
            # confidence signal) promotes draft -> approved.
            "status": "draft",
        },
        "target": target.model_dump(mode="json", exclude_none=True),
        "inputs": list(inputs.values()),
        "outputs": outputs,
        "elements": elements,
        "steps": steps,
        # Empty on purpose. A single happy-path run cannot observe what "no
        # such member" looks like, and inventing an outcome the run never saw
        # would be the artifact asserting something discovery did not
        # establish. Declaring them is review work -- which is what draft
        # status is for.
        "outcomes": [],
        "policy": {
            **policy.model_dump(mode="json"),
            "allowed_actions": used_actions,
        },
        "provenance": {
            "source": "discovery",
            "discovered_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "goal": goal,
            "model": model,
            "discovery_run_id": outcome.run_id,
            "steps_attempted": outcome.steps_attempted,
            "steps_recorded": len(steps),
            "human_interventions": 0,
            "notes": (
                "Emitted by discovery/recorder.py. Locator chains were built by probing "
                "which strategies uniquely resolved each element at the moment it was acted "
                "on; ambiguous strategies were discarded rather than recorded. "
                "outcomes[] is empty because a happy-path run observes no business outcomes."
                + (f" Unrecordable: {'; '.join(unrecordable)}" if unrecordable else "")
            ),
        },
    }
    return artifact


def _find_acted_node(cycle: Cycle, tree: Optional[dict]) -> Optional[dict]:
    """Re-locate the node the executor acted on, inside the captured tree.

    The executor resolved it against a *fresh* snapshot, so identity cannot be
    carried over; it is found again by the `ref` the resolution recorded,
    which is stable within one page state.
    """
    ref = (cycle.resolution or {}).get("ref")
    if ref:
        for node, _ in _iter_with_parents(tree):
            if node.get("ref") == ref:
                return node

    role = (cycle.tool_input.get("role") or "").strip().lower()
    name = (cycle.tool_input.get("name") or "").strip()
    matches = [
        n
        for n, _ in _iter_with_parents(tree)
        if (n.get("role") or "").lower() == role and (n.get("name") or "").strip() == name
    ]
    return matches[0] if len(matches) == 1 else None


def _add_checkpoints(
    steps: list[dict[str, Any]], elements: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Assert, after each navigating step, that the next step's target exists.

    A click that silently does nothing is the most common failure in UI
    automation, and this is the strongest checkpoint discovery can justify
    from what it actually observed: the run did reach a state where the next
    control was present.
    """
    for i, step in enumerate(steps):
        if step["action"] not in ("navigate", "click"):
            continue
        following = next((s for s in steps[i + 1 :] if s.get("element")), None)
        if following is None:
            continue
        step["checkpoint"] = {
            "type": "element_present",
            "element": following["element"],
            "timeout_ms": 8000,
        }
    return steps
