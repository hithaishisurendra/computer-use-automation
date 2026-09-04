"""Deterministic element resolution against the augmented accessibility tree.

No model anywhere in this module. An element is found by walking its
recorded chain in order and taking the first rung that resolves to exactly
one live element.

Two rules that carry most of the weight:

0. **A header row does not have to be marked up as one.** `cell_in_row`
   originally required a `columnheader` node, which only a `<th>` produces.
   Legacy grids routinely style a `<td>` row instead, so a table with obvious
   headers has none in the accessibility tree and the strategy resolved
   nothing at all. It now falls back to the table's own first row, scoped to
   that table and refused when ambiguous.
1. **Ambiguity is a miss, not a pick-the-first.** A rung matching three
   nodes has not identified anything; silently taking [0] is how replay
   ends up clicking the wrong row and reporting success. An ambiguous rung
   falls through to the next rung, and the ambiguity is recorded.
2. **Resolution runs against the augmented tree from perception/, not raw
   DOM.** CoreServ leaves every input, select, radio and checkbox nameless;
   perception/labeling.py infers those names. Resolving against raw DOM
   would mean re-deriving that, and against the *unaugmented* tree half the
   artifact's chains would be unresolvable.

Every attempt is recorded -- which rung fired, its confidence, whether it
was brittle, how many nodes each rung matched, and how long the whole walk
took -- because "it worked, but only via the brittle rung" is a drift
signal worth surfacing on a run that succeeded.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from capability.schema import TEMPLATE_RE, Element, LocatorRung, Scope


@dataclass
class RungAttempt:
    """What one rung of a chain did. Recorded whether or not it fired."""

    index: int
    strategy: str
    confidence: str
    brittle: bool
    match_count: int
    outcome: str  # resolved | no_match | ambiguous
    detail: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        d = {
            "rung": self.index,
            "strategy": self.strategy,
            "confidence": self.confidence,
            "brittle": self.brittle,
            "match_count": self.match_count,
            "outcome": self.outcome,
        }
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class Resolution:
    """The result of walking one element's chain."""

    element_key: str
    resolved: bool
    node: Optional[dict] = None
    frame_name: Optional[str] = None
    rung_index: Optional[int] = None
    rung: Optional[LocatorRung] = None
    attempts: list[RungAttempt] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def used_brittle_rung(self) -> bool:
        return bool(self.rung is not None and self.rung.brittle)

    @property
    def confidence(self) -> Optional[str]:
        return self.rung.confidence if self.rung else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "element": self.element_key,
            "resolved": self.resolved,
            "frame": self.frame_name,
            "rung_index": self.rung_index,
            "strategy": self.rung.strategy if self.rung else None,
            "confidence": self.confidence,
            "brittle": self.used_brittle_rung,
            "duration_ms": round(self.duration_ms, 2),
            "attempts": [a.as_dict() for a in self.attempts],
        }


class ElementUnresolvable(Exception):
    """No rung in the chain resolved to exactly one element."""

    def __init__(self, resolution: Resolution):
        self.resolution = resolution
        tried = ", ".join(
            f"{a.strategy}({a.outcome}, {a.match_count} match(es))" for a in resolution.attempts
        )
        super().__init__(
            f"element {resolution.element_key!r} could not be resolved; tried: {tried or '<no rungs>'}"
        )


# ---------------------------------------------------------------------------
# tree walking helpers
# ---------------------------------------------------------------------------


def substitute(text: Optional[str], params: dict[str, Any]) -> Optional[str]:
    """Substitute {{param}} references in a scope's `contains` clause.

    This is what makes "the View link in the row for member 10001"
    expressible instead of "the third View link".
    """
    if text is None:
        return None

    def repl(match: re.Match) -> str:
        name = match.group(1)
        if name not in params:
            raise KeyError(f"no value supplied for template parameter {name!r}")
        return str(params[name])

    return TEMPLATE_RE.sub(repl, text)


def iter_nodes(node: Optional[dict]):
    if node is None:
        return
    yield node
    for child in node.get("children") or []:
        yield from iter_nodes(child)


def node_text(node: Optional[dict]) -> str:
    """All text in a node's subtree: its own name plus every descendant's.

    Used for scope `contains` matching, which is why it is subtree-wide
    rather than the node's own name only -- "the row containing 10001"
    means the row one of whose cells reads 10001.
    """
    if node is None:
        return ""
    parts = []
    for n in iter_nodes(node):
        name = (n.get("name") or "").strip()
        if name:
            parts.append(name)
    return " ".join(parts)


def frame_key(frame: Optional[str]) -> str:
    """Snapshot key for a declared frame name. None -> the document."""
    return frame if frame is not None else ""


def _role_of(node: dict) -> str:
    return (node.get("role") or "").strip().lower()


def _name_of(node: dict) -> str:
    return (node.get("name") or "").strip()


def find_scopes(root: Optional[dict], scope: Scope, params: dict[str, Any]) -> list[dict]:
    """Find containers matching a scope, keeping only the INNERMOST matches.

    The innermost rule is not a nicety. CoreServ nests tables three deep, so
    an outer wrapper row's subtree text transitively contains every inner
    row's text -- without this, `{"role": "row", "contains": "10001"}`
    matches the page shell as well as the data row, and the whole scoping
    mechanism collapses into ambiguity. (The a11y diagnostic hit exactly
    this with an XPath before it was scoped the same way.)
    """
    wanted_role = scope.role.strip().lower()
    contains = substitute(scope.contains, params)
    wanted_name = scope.name

    matches: list[dict] = []
    for node in iter_nodes(root):
        if _role_of(node) != wanted_role:
            continue
        if contains and contains not in node_text(node):
            continue
        if wanted_name and _name_of(node) != wanted_name:
            continue
        matches.append(node)

    # Drop any match that contains another match: keep only the innermost.
    innermost = []
    for candidate in matches:
        descendants = {id(n) for n in iter_nodes(candidate) if n is not candidate}
        if not any(id(other) in descendants for other in matches):
            innermost.append(candidate)
    return innermost


def _row_cells(row: dict) -> list[dict]:
    """Direct cell children of a row, in document order."""
    return [
        child
        for child in (row.get("children") or [])
        if _role_of(child) in ("cell", "columnheader", "rowheader")
    ]


def _find_column_index(root: Optional[dict], row: dict, column_header: str) -> Optional[int]:
    """Find which column position a named header occupies in the row's table.

    Two passes, in order of confidence.

    1. A real `columnheader` node anywhere with the wanted name. This is what
       a `<th>` produces and it is unambiguous.
    2. Failing that, the first row of the target row's own table, read as a
       header row. Legacy table markup very often builds a header row from
       styled `<td>` rather than `<th>` -- MERIDIAN does it on every grid, so
       it has no `columnheader` node at all and pass 1 finds nothing on a
       table that plainly has headers.

    The fallback is scoped to the row's own table rather than the whole
    document, and it declines when the name matches more than one column,
    because "which column is Balance" has no answer if two say Balance.
    """
    for candidate in iter_nodes(root):
        if _role_of(candidate) != "row":
            continue
        cells = _row_cells(candidate)
        for i, cell in enumerate(cells):
            if _role_of(cell) == "columnheader" and _name_of(cell) == column_header:
                # Only meaningful if the target row has at least this many cells.
                if len(_row_cells(row)) > i:
                    return i

    return _header_row_index(root, row, column_header)


def _rows_of(container: Optional[dict]) -> list[dict]:
    return [n for n in iter_nodes(container) if _role_of(n) == "row"]


def _owning_table(root: Optional[dict], row: dict) -> Optional[dict]:
    """The innermost table containing this row.

    Innermost for the same reason scopes are: legacy pages nest layout
    tables around data tables, and the outer one's first row is page
    furniture, not headers.
    """
    owner = None
    for node in iter_nodes(root):
        if _role_of(node) != "table":
            continue
        if any(candidate is row for candidate in _rows_of(node)):
            owner = node  # later matches are deeper in the walk
    return owner


def _header_row_index(root: Optional[dict], row: dict, column_header: str) -> Optional[int]:
    """Treat a table's first row as headers when it declares none.

    Only fires when the table contains no `columnheader` at all: a table that
    has real headers and simply does not have this one is a miss, not an
    invitation to guess from its first data row.
    """
    table = _owning_table(root, row)
    if table is None:
        return None
    if any(_role_of(n) == "columnheader" for n in iter_nodes(table)):
        return None

    rows = _rows_of(table)
    if len(rows) < 2 or rows[0] is row:
        return None

    header_cells = _row_cells(rows[0])
    matches = [i for i, cell in enumerate(header_cells) if _name_of(cell) == column_header]
    if len(matches) != 1:
        return None
    index = matches[0]
    return index if len(_row_cells(row)) > index else None


# ---------------------------------------------------------------------------
# per-strategy matching
# ---------------------------------------------------------------------------


def _match_role_name(root: Optional[dict], rung: LocatorRung) -> list[dict]:
    wanted_role = (rung.role or "").strip().lower()
    return [
        n for n in iter_nodes(root) if _role_of(n) == wanted_role and _name_of(n) == rung.name
    ]


def _match_role_name_scoped(
    root: Optional[dict], rung: LocatorRung, params: dict[str, Any]
) -> list[dict]:
    wanted_role = (rung.role or "").strip().lower()
    found: list[dict] = []
    for container in find_scopes(root, rung.scope, params):
        found.extend(
            n
            for n in iter_nodes(container)
            if n is not container and _role_of(n) == wanted_role and _name_of(n) == rung.name
        )
    return found


def _match_cell_in_row(
    root: Optional[dict], rung: LocatorRung, params: dict[str, Any]
) -> list[dict]:
    found: list[dict] = []
    for row in find_scopes(root, rung.scope, params):
        cells = _row_cells(row)
        if rung.column_index is not None:
            if 0 <= rung.column_index < len(cells):
                found.append(cells[rung.column_index])
            continue
        index = _find_column_index(root, row, rung.column_header)
        if index is not None and 0 <= index < len(cells):
            found.append(cells[index])
    return found


def _match_role_ordinal(root: Optional[dict], rung: LocatorRung) -> list[dict]:
    wanted_role = (rung.role or "").strip().lower()
    candidates = [n for n in iter_nodes(root) if _role_of(n) == wanted_role]
    if 0 <= rung.index < len(candidates):
        return [candidates[rung.index]]
    return []


def match_rung(root: Optional[dict], rung: LocatorRung, params: dict[str, Any]) -> list[dict]:
    if rung.strategy == "role_name":
        return _match_role_name(root, rung)
    if rung.strategy == "role_name_scoped":
        return _match_role_name_scoped(root, rung, params)
    if rung.strategy == "cell_in_row":
        return _match_cell_in_row(root, rung, params)
    if rung.strategy == "role_ordinal":
        return _match_role_ordinal(root, rung)
    raise ValueError(f"unknown locator strategy {rung.strategy!r}")


# ---------------------------------------------------------------------------
# chain walking
# ---------------------------------------------------------------------------


def resolve_element(
    element_key: str,
    element: Element,
    frames: dict[str, Optional[dict]],
    params: dict[str, Any],
) -> Resolution:
    """Walk an element's chain and return the first unambiguous resolution.

    `frames` maps frame name -> that frame's augmented, *unfiltered* tree.
    The element declares which frame it lives in; resolution never searches
    across frames, because two frames legitimately hold identically named
    controls (CoreServ's nav and content frames both have a "Submit"
    button) and the frame is what disambiguates them.
    """
    started = time.perf_counter()
    resolution = Resolution(element_key=element_key, resolved=False, frame_name=element.frame)

    # An element that names no frame lives in the document itself. Playwright
    # reports the main frame's name as the empty string, so that is the key it
    # arrives under -- but "" is an implementation detail of the snapshot, not
    # something an artifact should have to write down.
    root = frames.get(frame_key(element.frame))
    if root is None:
        resolution.duration_ms = (time.perf_counter() - started) * 1000
        resolution.attempts.append(
            RungAttempt(
                index=-1,
                strategy="<frame lookup>",
                confidence="n/a",
                brittle=False,
                match_count=0,
                outcome="no_match",
                detail=f"frame {element.frame!r} not present; have {sorted(frames)}",
            )
        )
        return resolution

    for i, rung in enumerate(element.chain):
        try:
            matches = match_rung(root, rung, params)
        except KeyError as exc:
            resolution.attempts.append(
                RungAttempt(
                    index=i,
                    strategy=rung.strategy,
                    confidence=rung.confidence,
                    brittle=rung.brittle,
                    match_count=0,
                    outcome="no_match",
                    detail=str(exc),
                )
            )
            continue

        if len(matches) == 1:
            resolution.attempts.append(
                RungAttempt(
                    index=i,
                    strategy=rung.strategy,
                    confidence=rung.confidence,
                    brittle=rung.brittle,
                    match_count=1,
                    outcome="resolved",
                )
            )
            resolution.resolved = True
            resolution.node = matches[0]
            resolution.rung_index = i
            resolution.rung = rung
            break

        # Zero matches is a miss. So is more than one: an ambiguous rung has
        # not identified anything, and picking [0] would be a guess dressed
        # up as a resolution.
        resolution.attempts.append(
            RungAttempt(
                index=i,
                strategy=rung.strategy,
                confidence=rung.confidence,
                brittle=rung.brittle,
                match_count=len(matches),
                outcome="no_match" if not matches else "ambiguous",
                detail=(
                    None
                    if not matches
                    else f"matched {len(matches)} nodes; ambiguity falls through to the next rung"
                ),
            )
        )

    resolution.duration_ms = (time.perf_counter() - started) * 1000
    return resolution


def require_element(
    element_key: str,
    element: Element,
    frames: dict[str, Optional[dict]],
    params: dict[str, Any],
) -> Resolution:
    resolution = resolve_element(element_key, element, frames, params)
    if not resolution.resolved:
        raise ElementUnresolvable(resolution)
    return resolution
