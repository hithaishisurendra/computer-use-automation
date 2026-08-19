"""Accessibility-tree perception.

Shared by discovery (feeds an LLM a compact snapshot to decide the next
action) and replay (resolves a saved role+name locator back to a live
element). Neither imports the other; both import this.

Perception strategy: role + accessible name, not DOM position or CSS
selectors. CoreServ's markup is deliberately hostile at the structural layer
(nested tables, rotating ids, meaningless classes) but keeps every control a
genuine HTML element with visible text, so the accessibility tree is the
one representation that should survive the hostility. See
docs/decisions.md for the full reasoning; evidence/a11y_diagnostic/REPORT.txt
is where that bet gets checked against a live run rather than assumed.

Implementation note: Playwright removed `page.accessibility.snapshot()`
(the CDP Accessibility-domain API) some versions back; the installed
version here (1.62) doesn't have it at all. The supported replacement is
`Locator.aria_snapshot()`, which returns a YAML-like text tree rather than a
JSON node dict. `mode="ai"` is used here over the default mode because it
gives every node a `[ref=...]` id and, critically, does NOT roll a node's
descendant text up into every ancestor's name the way the default mode does
(the default-mode login-page snapshot showed a `row` whose name was the
literal concatenation of every cell below it -- duplicated at every level,
which is the opposite of what a token-budget-conscious perception layer
wants). This module parses that YAML-ish text back into the same
role/name/children node shape a JSON accessibility snapshot would have had,
so the rest of the pipeline (filter_tree, to_compact_text, count_nodes)
doesn't need to know which underlying API produced it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

# Roles worth acting on: something an agent could click, type into, or select.
INTERACTIVE_ROLES = {
    "link",
    "button",
    "textbox",
    "combobox",
    "radio",
    "checkbox",
    "menuitem",
    "option",
}

# Roles worth reading even though you can't act on them directly: they carry
# the text content and table/row structure an agent needs to ground a
# decision (e.g. "which row has member 10004").
TEXT_ROLES = {
    "heading",
    "cell",
    "row",
    "rowheader",
    "columnheader",
    "text",
    "list",
    "listitem",
}

# Accessibility-node state keys worth surfacing in the compact rendering.
# (Chosen because they change what an action means: a checked checkbox vs
# unchecked, a disabled control an agent shouldn't try to click, etc.)
STATE_KEYS = (
    "checked",
    "selected",
    "expanded",
    "disabled",
    "readonly",
    "required",
    "pressed",
    "value",
    "level",
)


@dataclass
class FrameSnapshot:
    frame_name: str
    frame_url: str
    tree: Optional[dict]
    error: Optional[str] = None


async def snapshot_all_frames(page) -> list[FrameSnapshot]:
    """Snapshot the accessibility tree of every frame on the page.

    A framed app like CoreServ (root frameset -> nav frame + content frame)
    needs one perception unit per frame: the nav frame persists across the
    whole session while the content frame is what actually changes as the
    agent acts, and a caller (discovery or replay) needs to know which frame
    a given control lives in to act on it. So this iterates page.frames and
    snapshots each one independently, rooted at that frame's own <html>,
    rather than taking one whole-page snapshot.

    Note: calling aria_snapshot(mode="ai") on a frame that itself contains
    nested frames (the top frameset document does) auto-recurses into their
    content too -- confirmed empirically against CoreServ's own frameset.
    Since that content is already captured by this function's own separate
    call for the child frame, any node with role "iframe" (Playwright's role
    for both <iframe> and frameset <frame> elements) has its children
    dropped here rather than double-counted across two frames' evidence.
    """
    results: list[FrameSnapshot] = []
    for frame in page.frames:
        try:
            raw_text = await frame.locator("html").aria_snapshot(mode="ai")
        except Exception as exc:  # detached frame, empty document, etc.
            results.append(
                FrameSnapshot(
                    frame_name=frame.name or "",
                    frame_url=frame.url or "",
                    tree=None,
                    error=f"aria_snapshot failed: {exc}",
                )
            )
            continue

        tree = _parse_aria_yaml(raw_text)
        _drop_nested_frame_content(tree)
        _tag_frame(tree, frame.name or "", frame.url or "")
        results.append(FrameSnapshot(frame_name=frame.name or "", frame_url=frame.url or "", tree=tree))

    return results


# ---------------------------------------------------------------------------
# aria_snapshot(mode="ai") text -> node dict
# ---------------------------------------------------------------------------

_ROLE_RE = re.compile(r'^([^\s"\[:]+)')
_QUOTED_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"')
_BRACKET_RE = re.compile(r"^\[([^\]]*)\]")


def _unescape(s: str) -> str:
    return s.replace('\\"', '"').replace("\\\\", "\\")


def _unwrap_single_quoted(content: str) -> str:
    """Strip a YAML single-quoted-scalar wrapper the aria_snapshot emitter
    adds around an *entire* node prefix (role, quoted name, brackets) when
    that node's computed accessible name itself contains a colon -- e.g.
    `'cell "Reference Number: SA-1234 ..." [ref=e16]':`. Without this, the
    line's own role/name parsing below would see a leading stray `'`
    character and misparse the role token. YAML escapes a literal `'`
    inside a single-quoted scalar as `''`, which is unescaped here too.
    """
    if not content.startswith("'"):
        return content
    buf: list[str] = []
    i = 1
    while i < len(content):
        ch = content[i]
        if ch == "'":
            if i + 1 < len(content) and content[i + 1] == "'":
                buf.append("'")
                i += 2
                continue
            return "".join(buf) + content[i + 1:]
        buf.append(ch)
        i += 1
    return content  # no closing quote found; malformed, leave as-is


def _parse_content(content: str) -> dict:
    """Parse one line's content (after stripping the leading '- ') into a
    node dict. Handles the shapes actually observed in CoreServ's own
    output:

        role [ref=e1]:                          -- container, children follow
        cell "Northridge Credit Union" [ref=e5]  -- leaf with a name
        textbox [ref=e27]: "50"                  -- leaf with a value, not a name
        text: Paper                              -- leaf text content, no quotes
        radio [checked] [active] [ref=e36]       -- multiple bracket groups
        /url: /search                            -- property line, not a real node
    """
    node: dict[str, Any] = {"role": "", "name": "", "value": None, "children": []}

    m = _ROLE_RE.match(content)
    role = m.group(1) if m else content.strip()
    node["role"] = role
    rest = content[len(role):].lstrip()

    qm = _QUOTED_RE.match(rest)
    if qm:
        node["name"] = _unescape(qm.group(1))
        rest = rest[qm.end():].lstrip()

    while rest.startswith("["):
        bm = _BRACKET_RE.match(rest)
        if not bm:
            break
        group = bm.group(1)
        if "=" in group:
            k, v = group.split("=", 1)
            k = k.strip()
            if k != "ref":
                node[k] = v.strip()
            else:
                node["ref"] = v.strip()
        else:
            node[group.strip()] = True
        rest = rest[bm.end():].lstrip()

    if rest.startswith(":"):
        rest = rest[1:].strip()
        if rest:
            if rest.startswith('"') and rest.endswith('"') and len(rest) >= 2:
                node["value"] = _unescape(rest[1:-1])
            else:
                node["value"] = rest

    # Non-interactive leaves (plain "text" nodes, but also e.g. "listitem"
    # for a validation-error <li>) carry their content as a same-line
    # scalar value ("text: Paper", "listitem: Initial deposit must be at
    # least 25.00.") rather than a quoted name -- fold it into `name` so
    # downstream role-based filtering treats it like any other named leaf.
    # Interactive roles are excluded: a textbox's same-line value
    # ("textbox: 50") is the text currently typed into it, which is state,
    # not the control's accessible name, and must not be conflated with one.
    if node["role"] not in INTERACTIVE_ROLES and not node["name"] and node["value"]:
        node["name"] = node["value"]
        node["value"] = None

    return node


def _parse_aria_yaml(text: str) -> Optional[dict]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    top_level: list[dict] = []
    stack: list[tuple[int, dict]] = []  # (indent_level, node)

    for line in lines:
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        level = indent // 2
        if not stripped.startswith("- "):
            continue
        content = _unwrap_single_quoted(stripped[2:])

        while stack and stack[-1][0] >= level:
            stack.pop()
        parent = stack[-1][1] if stack else None

        if content.startswith("/"):
            # Property line (e.g. "/url: /search") describing the parent
            # node, not a node of its own -- attach and move on.
            prop = _parse_content(content)
            propname = prop["role"][1:]
            propvalue = prop["value"] if prop["value"] is not None else prop["name"]
            if parent is not None:
                parent.setdefault("props", {})[propname] = propvalue
            continue

        node = _parse_content(content)
        if parent is not None:
            parent["children"].append(node)
        else:
            top_level.append(node)
        stack.append((level, node))

    if len(top_level) == 1:
        return top_level[0]
    if top_level:
        return {"role": "root", "name": "", "value": None, "children": top_level}
    return None


def _drop_nested_frame_content(node: Optional[dict]) -> None:
    if node is None:
        return
    if node.get("role") == "iframe":
        node["children"] = []
        return
    for child in node.get("children") or []:
        _drop_nested_frame_content(child)


def _tag_frame(node: Optional[dict], frame_name: str, frame_url: str) -> None:
    """Recursively stamp every node with the frame it came from, so once
    nodes are flattened or compared across frames the origin travels with
    the node instead of living only in the outer FrameSnapshot wrapper."""
    if node is None:
        return
    node["frame_name"] = frame_name
    node["frame_url"] = frame_url
    for child in node.get("children") or []:
        _tag_frame(child, frame_name, frame_url)


# ---------------------------------------------------------------------------
# Filtering, rendering, token estimation
# ---------------------------------------------------------------------------


def count_nodes(node: Optional[dict]) -> int:
    if node is None:
        return 0
    total = 1
    for child in node.get("children") or []:
        total += count_nodes(child)
    return total


def filter_tree(node: Optional[dict]) -> Optional[dict]:
    """Prune a raw accessibility tree down to what an agent should look at.

    Keep: interactive controls (link/button/textbox/combobox/radio/checkbox/
    menuitem/option) and text-bearing nodes (heading/cell/row/rowheader/
    columnheader/text) that have a non-empty name or value.

    Drop: purely structural nodes (generic containers, groups, unnamed
    tables/rows) that carry no name of their own.

    Ancestry is preserved implicitly rather than by special-casing "row" or
    "table" roles: a node survives if it is itself interesting OR if any
    descendant survived. That means every ancestor of a kept node -- the row
    it's in, the table that row is in -- stays in the pruned tree even when
    that ancestor itself has no name, which is exactly what's needed to
    still answer "which row is this button in" after filtering.
    """
    if node is None:
        return None

    children = node.get("children") or []
    filtered_children = [c for c in (filter_tree(child) for child in children) if c is not None]

    role = (node.get("role") or "").strip().lower()
    name = (node.get("name") or "").strip()
    value = str(node.get("value") or "").strip()

    is_interactive = role in INTERACTIVE_ROLES
    is_text_bearing = role in TEXT_ROLES and bool(name or value)
    keep_self = is_interactive or is_text_bearing or bool(filtered_children)

    if not keep_self:
        return None

    pruned = dict(node)
    pruned["children"] = filtered_children
    return pruned


def to_compact_text(node: Optional[dict], indent: int = 0) -> str:
    """Render a (filtered) tree as compact indented text for an LLM prompt.

    One line per node: role, quoted name, then any state that changes what
    an action against the node would mean (checked, disabled, value, ...),
    plus href for links (pulled from the /url property line) since that's
    often the cheapest way to disambiguate two links with the same visible
    text and role.
    """
    if node is None:
        return ""
    lines: list[str] = []
    _render_node(node, indent, lines)
    return "\n".join(lines)


def _render_node(node: dict, indent: int, lines: list[str]) -> None:
    role = node.get("role") or "unknown"
    name = (node.get("name") or "").strip()

    parts = [f"{role}"]
    if name:
        parts.append(f'"{name}"')

    state_bits = []
    for key in STATE_KEYS:
        if key in node and node[key] not in (None, False, ""):
            state_bits.append(f"{key}={node[key]}")
    href = (node.get("props") or {}).get("url")
    if href:
        state_bits.append(f"href={href}")
    if state_bits:
        parts.append(f"[{', '.join(state_bits)}]")

    lines.append("  " * indent + " ".join(parts))

    for child in node.get("children") or []:
        _render_node(child, indent + 1, lines)


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token, the standard OpenAI/Anthropic
    rule of thumb for English text). Not a real tokenizer count -- good
    enough to compare raw vs filtered payload size, not for billing."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def raw_json_text(node: Optional[dict]) -> str:
    """What you'd actually ship an LLM if you didn't filter at all: the
    parsed (but unfiltered) accessibility tree as JSON. Used as the "raw"
    side of the raw-vs-filtered token comparison in the diagnostic."""
    if node is None:
        return ""
    return json.dumps(node, indent=2, default=str)
