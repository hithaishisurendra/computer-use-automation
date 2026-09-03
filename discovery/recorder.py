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

4. **Irreversible-looking steps are marked risky.** A click whose resolved
   control name matches a post-like verb from the app profile is recorded as
   `risk: "risky"`, which makes replay refuse to perform it unattended. This
   is a first guess, not a determination -- verb matching cannot reliably
   detect irreversibility in a legacy UI -- so every decision AND every
   near-miss is written into the artifact and the evidence log, where the
   draft -> approved review can see what the heuristic weighed.

Everything is emitted as `status: "draft"`. Discovery does not get to approve
its own output.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from typing import Any, Callable, Optional

from capability.schema import SCHEMA_VERSION
from discovery.loop import Cycle, DiscoveryOutcome
from replay import resolver

@dataclass(frozen=True)
class RiskRules:
    """Per-app vocabulary for the risky-step heuristic.

    Loaded from the app profile rather than hardcoded here: which words mean
    "commit" is knowledge about a target, not about recording.
    """

    app: str = "default"
    post_like_verbs: tuple[str, ...] = ()
    near_miss_verbs: tuple[str, ...] = ()
    parameter_aliases: tuple[tuple[str, str], ...] = ()

    def alias_for(self, label: str) -> Optional[str]:
        for candidate, name in self.parameter_aliases:
            if candidate.strip().lower() == (label or "").strip().lower():
                return name
        return None

    def match(self, control_name: str) -> Optional[str]:
        """The post-like verb this control name matches, if any."""
        return _first_word_match(control_name, self.post_like_verbs)

    def near_miss(self, control_name: str) -> Optional[str]:
        """A verb that was considered and deliberately not treated as risky."""
        return _first_word_match(control_name, self.near_miss_verbs)


def _first_word_match(text: str, verbs: tuple[str, ...]) -> Optional[str]:
    """Whole-word, case-insensitive. Word boundaries matter: a substring test
    would match 'Post' inside 'Postal Address' and 'Save' inside 'Saved
    Searches', neither of which is a commit."""
    haystack = text or ""
    for verb in verbs:
        if re.search(rf"\b{re.escape(verb)}\b", haystack, re.IGNORECASE):
            return verb
    return None


def risk_rules_from_profile(profile) -> RiskRules:
    """The risky-step vocabulary, from the app profile.

    Lived in discovery/app_profiles.json, which was the right idea in the
    wrong place: it is the same kind of per-app knowledge as the error
    markers and the recovery actions, and splitting it across two config
    files meant adding an app touched two.
    """
    return RiskRules(
        app=profile.name,
        post_like_verbs=tuple(profile.risk_verbs),
        near_miss_verbs=tuple(profile.near_miss_verbs),
        parameter_aliases=tuple(profile.parameter_aliases.items()),
    )


DEFAULT_RISK_RULES = RiskRules(
    post_like_verbs=("Post", "Confirm", "Transfer", "Save", "Delete"),
    near_miss_verbs=("Submit", "Apply", "Send", "Update", "Continue", "Accept",
                     "Approve", "Authorize", "Remove", "Void", "Reverse", "OK"),
)

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
    is_sensitive: Optional[Callable[[str], bool]] = None,
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
    if is_extraction and scope_text and scope_text in (target.get("name") or ""):
        # Circular: the row was identified by the value being read out of it,
        # so the locator would only find the balance while the balance still
        # has the value it had during discovery. The model reaches for this
        # when a better scope was ambiguous, which makes it exactly the case
        # worth catching rather than trusting.
        scope_text = None
    # Prefer a scope keyed on a parameter: "the row for {{member_ref}}"
    # generalises across invocations, where a literal from this one run does
    # not. This is the difference between a reusable capability and a
    # recording that only works for the record it was discovered on.
    param_scope = _parameterise(scope_text, params)
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
        cells = _row_cells(row)
        index = cells.index(target) if target in cells else None

        # Every way this row could be named, most robust first. Each is
        # probed against the live tree below and kept only if it resolves to
        # exactly this cell, so proposing several costs nothing but gives the
        # chain somewhere to fall to.
        #
        # cell_equals comes first because it is the only one immune to the
        # prefix problem: `contains: "100234-S0001"` matches nine rows on a
        # member whose shares are numbered with suffixes, and an ambiguous
        # rung is discarded, which is how a flow ends up with no chain at all.
        scopes: list[tuple[dict, str]] = []
        for sibling in cells:
            if sibling is target:
                continue
            text = (sibling.get("name") or "").strip()
            if not text:
                continue
            scopes.append(({"role": "row", "cell_equals": _parameterise(text, params) or text},
                           "high"))
        for text in filter(None, [scope_value, _row_scope_text(row, target)]):
            scopes.append(({"role": "row", "contains": text}, "medium"))

        for scope, confidence in scopes:
            if header:
                candidates.append(
                    ("cell_in_row",
                     {"strategy": "cell_in_row", "scope": scope,
                      "column_header": header, "confidence": confidence})
                )
            if index is not None:
                candidates.append(
                    ("cell_in_row",
                     {"strategy": "cell_in_row", "scope": scope,
                      "column_index": index,
                      "confidence": "medium" if confidence == "high" else "low"})
                )

    # Positional rungs, SCOPED to a container the target sits in. Counting
    # position across the whole document is the dangerous form: any control
    # inserted anywhere earlier shifts the index, and the rung still resolves
    # -- to a stranger. Counted inside "the row labelled Amount" it breaks
    # when that row goes, which a run reports instead of acting on.
    if role and row is not None:
        for text in _scope_texts(row, target, params, is_sensitive):
            scoped_index = _index_within(row, target, role)
            if scoped_index is None:
                continue
            candidates.append(
                (
                    "role_ordinal",
                    {
                        "strategy": "role_ordinal",
                        "role": role,
                        "index": scoped_index,
                        "scope": {"role": "row", "contains": text},
                        "confidence": "low",
                        "brittle": True,
                        "notes": (
                            "Positional, but scoped to its own row: if that row goes this "
                            "fails rather than selecting whatever moved into the position."
                        ),
                    },
                )
            )

    chain = _assemble(tree, candidates, params, target)
    if chain:
        return chain

    # Nothing identified this element except where it sits in the document.
    # A document-wide ordinal is never APPENDED to a chain that already has a
    # real rung -- it would only ever fire when that rung failed, which is
    # exactly when position is least trustworthy. As the SOLE rung it is the
    # difference between a flawed recording and no recording at all, so it is
    # kept and flagged loudly for review.
    if role:
        same_role = [n for n, _ in _iter_with_parents(tree) if (n.get("role") or "").lower() == role]
        if target in same_role:
            last_resort = {
                "strategy": "role_ordinal",
                "role": role,
                "index": same_role.index(target),
                "confidence": "low",
                "brittle": True,
                "notes": (
                    "DOCUMENT-WIDE POSITIONAL, AND THE ONLY RUNG. Nothing else "
                    "identified this control uniquely. This counts position across the "
                    "whole page, so any control added before it shifts the index and "
                    "this resolves to the wrong element without failing. Review before "
                    "approving: give the control an accessible name, or find a "
                    "container to scope it to."
                ),
            }
            if _resolves_uniquely(tree, last_resort, params, target):
                return [last_resort]
    return []


def _assemble(tree, candidates, params, target) -> list[dict[str, Any]]:
    """Keep the candidates that uniquely resolve, most robust strategy first."""
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    for strategy in STRATEGY_ORDER:
        for candidate_strategy, rung in candidates:
            if candidate_strategy != strategy:
                continue
            fingerprint = json.dumps(rung, sort_keys=True, default=str)
            if fingerprint in seen:
                continue
            if _resolves_uniquely(tree, rung, params, target):
                seen.add(fingerprint)
                chain.append(rung)
    return chain


def _scope_texts(
    row: dict,
    target: dict,
    params: dict[str, Any],
    is_sensitive: Optional[Callable[[str], bool]] = None,
) -> list[str]:
    """Texts that might identify this row, safest first.

    Two filters, both learned the hard way.

    A scope keyed on a PARAMETER generalises across invocations; a literal
    from this one run does not. So when any cell in the row parameterises,
    only the parameterised forms are kept -- the alternatives would be
    strictly worse locators recorded alongside a better one.

    That rule also happens to be the privacy rule, and this is not a
    coincidence: the cells that carry a record's identity are the ones that
    carry its personal data. A results row holds the member number AND the
    member's name, and the first tightened recording scoped a locator on
    "Lovelace, Ada" -- a member's name, in an artifact bound for a repo. The
    scrubber's own declaration decides what counts as sensitive, rather than
    this module guessing.
    """
    literals: list[str] = []
    parameterised: list[str] = []
    for cell in _row_cells(row):
        if cell is target:
            continue
        text = (cell.get("name") or "").strip()
        if not text:
            continue
        if is_sensitive is not None and is_sensitive(text):
            continue
        templated = _parameterise(text, params)
        target_list = parameterised if templated else literals
        candidate = templated or text
        if candidate not in target_list:
            target_list.append(candidate)
    return (parameterised or literals)[:3]


def _index_within(container: dict, target: dict, role: str) -> Optional[int]:
    """Position of the target among same-role nodes inside the container."""
    same = [
        n for n, _ in _iter_with_parents(container)
        if n is not container and (n.get("role") or "").lower() == role
    ]
    return same.index(target) if target in same else None


def _output_name(raw: str, taken: set[str]) -> str:
    """Name an output after what it is, not after the record it was found on.

    Same rule as `_derive_capability_id`: a token containing a digit is a
    value from this one run, not a name. The model asked for
    `balance_100234_s0001`, which reads as a different output for every member
    the capability is invoked with -- and an output name is the public
    contract a calling agent binds to, so it must not carry the identity the
    parameter already carries.

    The model's own name is kept when stripping would leave nothing, or would
    collide with an output already declared: a confusing name is better than
    a wrong one or a lost value.
    """
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", raw or "") if p]
    kept = [p for p in parts if not any(c.isdigit() for c in p)]
    candidate = "_".join(kept).lower()
    if not candidate or candidate in taken:
        return raw
    return candidate


# A currency amount inside a recorded value is the signature of display text
# that leaked in where a stable identifier belonged. MERIDIAN renders share
# options as "100234-S0001-6 - Regular Shares ($40.00)", so recording the
# label embeds a balance -- and the very run that records it may change that
# balance, which is how a capability invalidates its own locator on the way
# out. Matches "$1,234.56", "1,234.56" and "$40.00".
_CURRENCY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?|\b\d{1,3}(?:,\d{3})+(?:\.\d{2})?\b")


def suspect_value(action: str, value: str) -> Optional[str]:
    """Why a recorded value looks like observed page data rather than intent.

    Same reasoning as suppressing name-based rungs for extraction targets: a
    locator or a value derived from what the page currently displays is
    circular, and it resolves during discovery precisely because the page
    still shows what discovery saw.

    Scoped to `select` on purpose. A fill value containing currency is
    normal -- an amount of "5.00" is exactly what the caller typed. A select
    value containing currency cannot be caller intent, because the caller did
    not compose it: the page did.
    """
    if action != "select" or not value:
        return None
    match = _CURRENCY_RE.search(value)
    if not match:
        return None
    return (
        f"contains the currency amount {match.group(0)!r}, which is live page data "
        "from the option's display label rather than a stable identifier"
    )


def _parameterise(text: Optional[str], params: dict[str, Any]) -> Optional[str]:
    """Replace a discovered literal inside a scope with its {{param}}.

    Substring rather than whole-string, which matters for compound
    identifiers: a share id of `100234-S0001` becomes `{{member_ref}}-S0001`
    and travels to the next member, where a whole-string comparison would
    have left the discovered member baked in. Longest example first so a
    short value that is a substring of a longer one cannot corrupt it.
    """
    if not text:
        return None
    out = text
    for name, example in sorted(params.items(), key=lambda kv: -len(str(kv[1]))):
        example = str(example)
        if example and example in out:
            out = out.replace(example, f"{{{{{name}}}}}")
    return out if out != text else None


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


def _select_value(cycle: Cycle) -> str:
    """The option a select step should record.

    The browser's read-back of the element after selecting, which is the
    option's `value` attribute. Falls back to whatever the model passed only
    when that read-back is unavailable -- and the model passes whichever of
    value or label it saw in the snapshot, which for a legacy console is
    usually the label.
    """
    if cycle.selected_value:
        return cycle.selected_value
    return cycle.tool_input.get("value", "") or ""


# A value that starts with a digit and is otherwise identifier-shaped: an
# account or share number. Excludes decimals, so a currency amount typed into
# a field is not mistaken for an identifier.
_IDENTIFIER_SHAPE = re.compile(r"^[0-9][0-9A-Za-z_\-]*$")


def _infer_select_input(value: str, label: str, alias: Optional[str] = None) -> dict[str, Any]:
    """Declare a typed input for an option the model chose.

    Named from the field's label without the `_ref` suffix that identifier
    text fields get. That convention exists because tenants relabel identifier
    *fields* ("Member ID" vs "Account Number") while meaning the same entity;
    a select's label names its role in the flow ("From Share"), which is
    stable, so `from_share` is both accurate and what a caller would guess.

    No regex pattern: the shape of a share id is a per-tenant fact, and a
    pattern learned from one member's shares would reject another tenant's.
    """
    identifier = bool(_IDENTIFIER_SHAPE.match(value or ""))
    return {
        "name": alias or (_slug(label) or "option"),
        "type": "string",
        "required": True,
        "description": (
            f"Option selected in {label!r}. Must be valid for the record this "
            "capability is invoked against."
            if identifier
            else f"Option selected in {label!r}."
        ),
        "sensitivity": "identifier" if identifier else "public",
        "example": value,
    }


def _infer_input(value: str, label: str, alias: Optional[str] = None) -> dict[str, Any]:
    """Declare a typed input for a literal the model typed.

    `alias` overrides the label-derived name where the app profile says the
    label is too generic to name a public contract after.
    """
    is_identifier = value.isdigit()
    spec: dict[str, Any] = {
        "name": alias or _parameter_name(label, is_identifier),
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
    risk_rules: "RiskRules" = DEFAULT_RISK_RULES,
    log: Optional[Callable[[str, dict[str, Any]], None]] = None,
    default_frame: Optional[str] = None,
    is_sensitive: Optional[Callable[[str], bool]] = None,
) -> dict[str, Any]:
    """Build the artifact dict from a successful discovery run.

    `risk_rules` is the app profile's commit vocabulary (see load_risk_rules).
    `log` is the discovery loop's evidence logger, if one is available; every
    risk decision and every near-miss goes through it so the heuristic's
    reasoning is in the run's evidence, not only in the artifact.
    """
    if not outcome.recordable:
        raise ValueError(f"cannot record an artifact from a {outcome.status!r} run")

    # A run stopped by the risk gate still has a flow worth keeping. The
    # blocked step is recorded so the artifact describes the whole capability
    # including the action nobody performed -- otherwise the gate would mean
    # irreversible capabilities can never be recorded, and the safe thing
    # would be the thing that makes the system useless.
    blocked = outcome.blocked_cycle if outcome.status == "risk_blocked" else None

    def emit(event: str, payload: dict[str, Any]) -> None:
        if log is not None:
            log(event, payload)

    # The page state that followed each action, used to give a terminal risky
    # click a checkpoint. Read off the NEXT cycle's observation, including the
    # goal_reached cycle -- which is the only observation of the page a final
    # post produced.
    after_url: dict[int, Optional[str]] = {}
    for i, c in enumerate(outcome.cycles):
        nxt = outcome.cycles[i + 1] if i + 1 < len(outcome.cycles) else None
        after_url[id(c)] = nxt.url if nxt else None

    # Only the path that worked. Failed and retried cycles are dropped here,
    # which is the whole point of the artifact being separate from the
    # transcript.
    acted = [c for c in outcome.cycles if c.status == "ok" and c.tool_name]
    if blocked is not None and blocked.tool_name:
        # It never ran, but it was resolved: the model named a control, the
        # resolver found it, and the gate refused before the click. That is
        # enough to record the step and build its locator chain.
        acted = acted + [blocked]

    inputs: dict[str, dict[str, Any]] = {}
    suspect: list[str] = []
    for cycle in acted:
        if cycle.tool_name == "fill":
            value = cycle.tool_input.get("value", "")
            label = cycle.tool_input.get("name") or "value"
            if value and value in goal:
                spec = _infer_input(value, label, risk_rules.alias_for(label))
                inputs[spec["name"]] = spec
        elif cycle.tool_name == "select":
            # EVERY select becomes a parameter, not only the ones whose
            # options vary per record. Telling those apart needs a heuristic
            # ("does the value contain the member id?") that works for share
            # ids and nothing else, and the fixed-vocabulary selects -- share
            # type, reason code -- are exactly what a caller varies anyway.
            # Over-parameterising costs a required input: visible and safe.
            # Under-parameterising bakes in a value that silently does the
            # wrong thing.
            value = _select_value(cycle)
            label = cycle.tool_input.get("name") or "option"
            reason = suspect_value("select", value)
            if reason:
                suspect.append(f"select {label!r}: recorded value {value!r} {reason}")
                emit("suspect_value", {"action": "select", "label": label,
                                       "value": value, "reason": reason})
            spec = _infer_select_input(value, label, risk_rules.alias_for(label))
            inputs[spec["name"]] = spec

    params = {spec["name"]: spec["example"] for spec in inputs.values()}

    elements: dict[str, dict[str, Any]] = {}
    steps: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    unrecordable: list[str] = []
    risk_notes: list[str] = []
    positional_only: list[str] = []

    for position, cycle in enumerate(acted, start=1):
        step_id = f"s{position}"
        action = cycle.tool_name
        frame = cycle.tool_input.get("frame") or default_frame

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

        # Snapshots are keyed by frame NAME, and the main frame's name is the
        # empty string. An element that declares no frame is None here, and
        # `.get(None)` misses -- so on a frameless app every chain was built
        # against a tree of None and nothing could ever resolve uniquely. The
        # resolver already normalises this; the recorder has to as well.
        tree = (cycle.frames_before or {}).get(resolver.frame_key(frame))
        node = cycle.acted_node or _find_acted_node(cycle, tree)
        if node is None:
            unrecordable.append(f"{step_id}: could not re-locate the element that was acted on")
            continue

        chain = build_chain(tree, node, cycle.tool_input, params,
                            is_extraction=(action == "extract"), is_sensitive=is_sensitive)
        if len(chain) == 1 and "DOCUMENT-WIDE POSITIONAL" in (chain[0].get("notes") or ""):
            positional_only.append(
                f"{step_id} ({cycle.element_key!r}): identified only by its position in the "
                f"document ({chain[0]['role']} index {chain[0]['index']})"
            )
            emit("positional_only_element", {
                "step_id": step_id, "element": cycle.element_key,
                "role": chain[0]["role"], "index": chain[0]["index"],
            })
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

        # Risk classification. Only clicks: a fill or a select changes a form,
        # the click is what sends it. The resolved control's own accessible
        # name is authoritative over what the model said it was targeting.
        control_name = (node.get("name") or cycle.tool_input.get("name") or "").strip()
        risk = "safe"
        step_note: Optional[str] = None
        if action == "click":
            control_role = (node.get("role") or cycle.tool_input.get("role") or "").strip().lower()
            matched = risk_rules.match(control_name)
            if matched and control_role != "button":
                # Navigation links share the commit vocabulary. Only a
                # submit-type control can commit, and only <button> and
                # <input type=submit> carry role "button"; an <a> is "link".
                risk_notes.append(
                    f"{step_id} near-miss: {control_name!r} matched {matched!r} but is a "
                    f"{control_role or 'non-button'}, not a submit control"
                )
                emit("risk_classified", {
                    "step_id": step_id, "control": control_name, "role": control_role,
                    "decision": "safe", "near_miss_verb": matched,
                    "why": "matched a commit verb but is not a submit-type control",
                })
                matched = None
            near = None if matched else risk_rules.near_miss(control_name)
            if matched:
                risk = "risky"
                step_note = (
                    f"Marked risky by the recorder: the control name {control_name!r} "
                    f"matched the post-like verb {matched!r} in app profile "
                    f"{risk_rules.app!r}. This is a heuristic first guess, not a "
                    "determination -- confirm it during the draft -> approved review."
                )
                risk_notes.append(f"{step_id} risky: {control_name!r} via {matched!r}")
                emit("risk_classified", {
                    "step_id": step_id, "control": control_name,
                    "decision": "risky", "matched_verb": matched, "profile": risk_rules.app,
                })
            elif near:
                risk_notes.append(
                    f"{step_id} near-miss: {control_name!r} contains {near!r}, "
                    "which the profile does not treat as a commit"
                )
                emit("risk_classified", {
                    "step_id": step_id, "control": control_name,
                    "decision": "safe", "near_miss_verb": near, "profile": risk_rules.app,
                })

        step: dict[str, Any] = {
            "id": step_id,
            "action": action,
            "element": key,
            "risk": risk,
        }
        if step_note:
            step["notes"] = step_note
        step["_after_url"] = after_url.get(id(cycle))
        if action in ("fill", "select"):
            value = _select_value(cycle) if action == "select" else cycle.tool_input.get("value", "")
            for param, example in params.items():
                if str(example) == str(value):
                    value = f"{{{{{param}}}}}"
                    break
            step["value"] = value
        if action == "extract":
            output_name = _output_name(
                cycle.tool_input["output_name"], {o["name"] for o in outputs}
            )
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

    steps = _ensure_opening_navigate(steps, acted, target, default_frame)
    steps = _renumber(steps)
    steps, checkpoint_problems = _add_checkpoints(steps, elements, params)
    unrecordable.extend(checkpoint_problems)

    used_actions = sorted({s["action"] for s in steps})
    incomplete_note = ""
    if blocked is not None:
        incomplete_note = (
            " THE FLOW WAS NOT COMPLETED. Discovery stopped at the last step because "
            "it is irreversible and this run's policy requires a person to perform it. "
            "That step is recorded and was NEVER EXECUTED, so nothing after it was "
            "observed: no confirmation screen, no outputs, and its checkpoint is the "
            "best guess available rather than something the run verified. Approving "
            "this artifact means approving a step no one has seen succeed."
        )

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
            # Never inherited from the discovery run. Recording an
            # irreversible capability is an attended act by an engineer who
            # may deliberately relax the gate to walk the flow once; replay is
            # unattended production. The emitted artifact always takes the
            # conservative posture, so a relaxed recording session cannot
            # produce a capability that posts without a human.
            "risky_action_handling": "require_confirmation",
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
            "flow_completed": blocked is None,
            "notes": (
                "Emitted by discovery/recorder.py. Locator chains were built by probing "
                "which strategies uniquely resolved each element at the moment it was acted "
                "on; ambiguous strategies were discarded rather than recorded. "
                "outcomes[] is empty because a happy-path run observes no business outcomes."
                + incomplete_note
                + (
                    " SUSPECT RECORDED VALUES -- these look like live page data rather "
                    "than caller intent and will go stale: " + "; ".join(suspect) + "."
                    if suspect
                    else ""
                )
                + (
                    " POSITIONALLY IDENTIFIED ELEMENTS -- these resolve only by where they "
                    "sit in the document and will silently target the wrong control after a "
                    "layout change: " + "; ".join(positional_only) + "."
                    if positional_only
                    else ""
                )
                + _risk_summary(risk_rules, risk_notes)
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


def _ensure_opening_navigate(
    steps: list[dict[str, Any]], acted: list[Cycle], target: Any,
    default_frame: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Make the artifact state its own starting precondition.

    A discovery session is already somewhere when the model takes its first
    action -- here, CoreServ's frameset had already loaded `/search` into the
    content frame, so the model never navigated and no navigate step was
    recorded. Replay then worked only because that frameset default happened
    to coincide with `entry_path`. That is the environment supplying a
    precondition the artifact never declared: it would break the moment a
    flow's entry differed from wherever the surface happens to open, and it
    would break silently, resolving against the wrong page.

    Fixing it here rather than by pressuring the model to navigate is
    deliberate. The model behaved correctly -- it was already on the right
    page and navigating would have been a wasted action. What was missing is
    a property of the *artifact*, not of the run, so the recorder is what
    owes it.
    """
    entry_path = getattr(target, "entry_path", None)
    if not entry_path:
        return steps

    opens_at_entry = (
        steps
        and steps[0]["action"] == "navigate"
        and steps[0].get("path") == entry_path
    )
    if opens_at_entry:
        return steps

    # Frame the flow actually works in, so the opening navigate loads the
    # content region rather than replacing a frameset.
    frame = next(
        (c.tool_input.get("frame") for c in acted if c.tool_input.get("frame")), default_frame
    )
    first_url = next((c.url for c in acted), None)
    observed = urlparse(first_url).path if first_url else None

    opening = {
        "id": "s0",
        "action": "navigate",
        "path": entry_path,
        "frame": frame,
        "risk": "safe",
        "notes": (
            "Added at record time: the session was already at "
            f"{observed or 'the entry page'} when the first action ran, so no navigation "
            "was observed. Recorded explicitly so the flow declares where it starts "
            "instead of inheriting it from wherever the surface happened to open."
        ),
    }
    return [opening, *steps]


def _renumber(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sequential ids after any insertion. Checkpoints reference element keys
    rather than step ids, so renumbering cannot break a reference."""
    for position, step in enumerate(steps, start=1):
        step["id"] = f"s{position}"
    return steps


def _risk_summary(risk_rules: "RiskRules", notes: list[str]) -> str:
    """What the risk heuristic decided, in the artifact a reviewer reads.

    Written even when nothing matched, so "the heuristic found nothing" is
    distinguishable from "the heuristic did not run".
    """
    profile = f"app profile {risk_rules.app!r}"
    if not notes:
        return (
            f" Risk heuristic ({profile}): no clicked control matched or neared a "
            "post-like verb."
        )
    return (
        f" Risk heuristic ({profile}; a first guess for review, not a determination): "
        + "; ".join(notes)
        + "."
    )


def _url_checkpoint(after: Optional[str], params: dict[str, Any]) -> Optional[dict[str, Any]]:
    """A checkpoint asserting the flow landed where this action took it.

    Used for a step that has no following control to assert -- which is
    exactly the shape of a terminal post: click, confirmation screen, done.
    The URL is what discovery actually observed afterwards, and it is
    parameterised so the checkpoint travels to the next member the capability
    is invoked for rather than pinning the one it was discovered on.

    URL rather than page text on purpose. A server-rendered console tends to
    put a live clock and a session id in its status bar, so text observed
    after an action is not reliably the same text on the next run; the path
    is.
    """
    if not after:
        return None
    path = urlparse(after).path
    if not path:
        return None

    # Parameterise whole path segments only. A blunt substring replace would
    # rewrite any occurrence of the value -- a two-character example would
    # perforate half the path -- so a segment is substituted only when it IS
    # the discovered value, not when it merely contains it.
    examples = {str(v) for v in params.values() if v not in (None, "")}
    segments = [
        "[^/]+" if segment in examples else re.escape(segment)
        for segment in path.split("/")
    ]
    return {"type": "url_matches", "pattern": "/".join(segments) + "$", "timeout_ms": 8000}


def _add_checkpoints(
    steps: list[dict[str, Any]],
    elements: dict[str, dict[str, Any]],
    params: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Assert, after each navigating step, that the next step's target exists.

    A click that silently does nothing is the most common failure in UI
    automation, and this is the strongest checkpoint discovery can justify
    from what it actually observed: the run did reach a state where the next
    control was present.

    A step marked risky must end up with a checkpoint or the artifact will not
    load -- the schema rejects an unverifiable irreversible step, because the
    escalation model depends on being able to confirm that a human's manual
    action landed. The rule above cannot supply one for a terminal post, which
    by definition has no following control, so such a step falls back to the
    URL the action was observed to produce. Returns any step it still could
    not give one, so the caller can report it rather than emit an artifact
    that fails validation for reasons nobody wrote down.
    """
    params = params or {}
    problems: list[str] = []

    for i, step in enumerate(steps):
        after = step.pop("_after_url", None)
        if step["action"] not in ("navigate", "click"):
            continue
        following = next((s for s in steps[i + 1 :] if s.get("element")), None)
        if following is not None:
            step["checkpoint"] = {
                "type": "element_present",
                "element": following["element"],
                "timeout_ms": 8000,
            }
            continue
        if step.get("risk") != "risky":
            continue
        fallback = _url_checkpoint(after, params)
        if fallback is None:
            problems.append(
                f"{step['id']}: marked risky but no checkpoint could be derived -- no "
                "following control to assert and no post-action URL was observed. The "
                "artifact will fail validation, which is the correct outcome: an "
                "unverifiable irreversible step must not be recorded as if it were fine."
            )
            continue
        step["checkpoint"] = fallback

    # Steps that never reached the loop body's pop (navigate steps added by
    # _ensure_opening_navigate carry no _after_url) are cleaned here so the
    # private key can never survive into the artifact.
    for step in steps:
        step.pop("_after_url", None)

    return steps, problems
