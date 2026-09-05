"""Label augmentation: the perception layer's compensation for surfaces that
under-label their own controls.

The a11y diagnostic (evidence/a11y_diagnostic/REPORT.txt) found that on
CoreServ every link and button gets a correct accessible name (their own
visible text is the name, no association step needed) but every text
input, select, radio and checkbox comes back nameless -- CoreServ labels
fields with an adjacent table cell, not <label for>/aria-label, which
doesn't establish an accessibility-tree name relationship.

We are not fixing CoreServ's templates for this. The perception layer's
contract with its callers (discovery, replay) is "hand back a well-named
tree" -- so when the platform doesn't produce a name, this module infers
one directly from the DOM, the same way production RPA/computer-use tools
do when driving software they don't control. Every resolved name records
which rule produced it, and a control that resists every rule is marked
unnamed rather than silently dropped -- an agent (or a human reviewing a
capability) needs to know "this control has no discoverable name" is a
real, different state from "this control doesn't exist."

Resolution requires going back to the live DOM, which is why this is wired
into snapshot_all_frames() rather than being a pure function over the
already-parsed tree: it needs the frame the snapshot came from, resolving
each nameless node's aria_snapshot(mode="ai") `ref` back to a live element
via Playwright's `aria-ref=` locator engine. That resolution is only valid
for as long as the page hasn't navigated since the snapshot was taken,
which holds here since augmentation runs immediately after the snapshot,
before anything else touches the page.
"""

from __future__ import annotations

import re
from typing import Optional

# Roles CoreServ's markup is known to under-label. Links and buttons are
# deliberately excluded: their own text is always their name, so there is
# nothing for a DOM-side rule chain to add, and running one against a
# button/link that's merely *structurally* nameless (e.g. an icon-only
# button, which CoreServ's spec forbids -- "every control has visible text")
# would just be doing extra work to confirm what's already known.
AUGMENTABLE_ROLES = {"textbox", "combobox", "radio", "checkbox"}

# Controls whose activation submits a form. For these, and only these, the
# augmentation pass also records where that submission GOES.
#
# This is the one place perception reads something that is not an
# accessibility property, and it needs justifying rather than sneaking in.
# The safety model turns on knowing whether a click commits, and the
# accessibility tree cannot answer that: a link exposes its href as a `/url`
# property, but a submit button exposes nothing at all -- its destination
# lives on the enclosing <form>. So a control that can commit is the exact
# control whose target is invisible, and the gap is not incidental.
#
# The alternative was a DOM read in the executor, which would put page
# inspection in the layer that is supposed to act on what perception reports,
# and would need duplicating in the recorder. One attribute, read in the pass
# that already resolves nodes to live elements, keeps perception the single
# surface through which the page is seen.
SUBMIT_ROLES = {"button"}

_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_COLON_RE = re.compile(r"\s*:+\s*$")


def normalize_name(raw: str) -> str:
    """Strip whitespace, collapse internal whitespace runs, drop a trailing
    colon. CoreServ emits "Member ID :", "member id:", "Member  Id" for the
    same field -- this is what makes those the same normalized name (note:
    it does *not* fold case; "Member ID" and "member id" normalize to
    different strings, since the task scope here is whitespace/punctuation,
    not a semantic-equivalence judgement)."""
    if not raw:
        return ""
    collapsed = _WHITESPACE_RE.sub(" ", raw).strip()
    collapsed = _TRAILING_COLON_RE.sub("", collapsed)
    return collapsed.strip()


# Executed in-page against the live DOM element an accessibility node
# resolved to. Returns {name, source} for the first matching rule, or null
# if nothing matched (rule 7: caller marks the node unnamed).
_RESOLVE_JS = r"""
(el) => {
    function norm(s) {
        if (!s) return "";
        return s.replace(/\s+/g, " ").trim();
    }

    // 1. aria-label / aria-labelledby
    const ariaLabel = el.getAttribute("aria-label");
    if (ariaLabel && norm(ariaLabel)) {
        return { name: ariaLabel, source: "aria_label" };
    }
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
        const doc = el.ownerDocument;
        const parts = labelledby.split(/\s+/)
            .map((id) => { const ref = doc.getElementById(id); return ref ? ref.textContent : ""; })
            .filter((t) => norm(t));
        const joined = parts.join(" ");
        if (norm(joined)) return { name: joined, source: "aria_labelledby" };
    }

    // 2. <label for="..."> or a wrapping <label>
    if (el.id) {
        const escaped = (window.CSS && CSS.escape) ? CSS.escape(el.id) : el.id;
        const forLabel = el.ownerDocument.querySelector(`label[for="${escaped}"]`);
        if (forLabel && norm(forLabel.textContent)) {
            return { name: forLabel.textContent, source: "label_for" };
        }
    }
    const wrapping = el.closest("label");
    if (wrapping && norm(wrapping.textContent)) {
        return { name: wrapping.textContent, source: "label_wrap" };
    }

    // 3. Following text node -- the <input type=radio>Paper inline pattern.
    // Stops at the first sibling element (e.g. the next radio) rather than
    // reading through it, since that text belongs to the next control, not
    // this one.
    {
        let node = el.nextSibling;
        while (node) {
            if (node.nodeType === Node.TEXT_NODE) {
                const t = norm(node.textContent);
                if (t) return { name: t, source: "following_text" };
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                break;
            }
            node = node.nextSibling;
        }
    }

    // 4. Preceding sibling <td>/<th> text -- the <td>Label</td><td><input></td> pattern.
    if (el.parentElement && el.parentElement.tagName === "TD") {
        const prev = el.parentElement.previousElementSibling;
        if (prev && (prev.tagName === "TD" || prev.tagName === "TH")) {
            const t = norm(prev.textContent);
            if (t) return { name: t, source: "preceding_td" };
        }
    }

    // 5. Nearest preceding text anywhere within the same <tr> (broader
    // fallback for controls not directly wrapped by the immediately
    // preceding cell, e.g. nested one level deeper).
    const row = el.closest("tr");
    if (row) {
        const walker = el.ownerDocument.createTreeWalker(row, NodeFilter.SHOW_TEXT);
        let lastText = "";
        let node;
        while ((node = walker.nextNode())) {
            if (el.contains(node)) break;
            const pos = node.compareDocumentPosition(el);
            const nodePrecedesEl = !!(pos & Node.DOCUMENT_POSITION_FOLLOWING);
            if (!nodePrecedesEl) break;
            const t = norm(node.textContent);
            if (t) lastText = t;
        }
        if (lastText) return { name: lastText, source: "preceding_tr_text" };
    }

    // 6. placeholder / title
    const placeholder = el.getAttribute("placeholder");
    if (placeholder && norm(placeholder)) return { name: placeholder, source: "placeholder" };
    const title = el.getAttribute("title");
    if (title && norm(title)) return { name: title, source: "title" };

    // 7. Nothing matched.
    return null;
}
"""


async def _resolve_via_dom(frame, ref: str) -> Optional[dict]:
    try:
        locator = frame.locator(f"aria-ref={ref}")
        if await locator.count() != 1:
            return None
        return await locator.evaluate(_RESOLVE_JS)
    except Exception:
        # Stale ref (page navigated since the snapshot), detached element,
        # cross-origin restriction, etc. -- treated the same as "no rule
        # matched": mark unnamed rather than raise, since a locator/naming
        # failure on one control shouldn't take down the whole snapshot.
        return None


def apply_platform_names(node: Optional[dict]) -> None:
    """First pass, run before augmentation: normalize whatever name the
    accessibility tree itself already provided (links, buttons, and any
    control that happens to have one), and tag it name_source="platform".
    Nodes left with an empty name after this pass are augmentation
    candidates."""
    if node is None:
        return
    raw = node.get("name") or ""
    if raw.strip():
        node["name_raw"] = raw
        node["name"] = normalize_name(raw)
        node["name_source"] = "platform"
    else:
        node["name_raw"] = ""
    for child in node.get("children") or []:
        apply_platform_names(child)


_ACTION_JS = r"""
(el) => {
    // Where activating this control would send the page. Only the form's
    // action -- not its method, fields or values. A control outside a form
    // submits nothing, and reports nothing.
    const form = el.form || el.closest("form");
    if (!form) return null;
    return form.getAttribute("action") || null;
}
"""


async def _resolve_action(frame, ref: str) -> Optional[str]:
    try:
        locator = frame.locator(f"aria-ref={ref}")
        if await locator.count() != 1:
            return None
        return await locator.evaluate(_ACTION_JS)
    except Exception:
        return None


async def augment_tree(node: Optional[dict], frame) -> None:
    """Second pass: for every still-nameless node in an augmentable role,
    resolve its aria_snapshot ref back to the live DOM element and run the
    rule chain against it. Mutates the tree in place.

    Also records `props.action` for submit-type controls -- see SUBMIT_ROLES
    for why perception reads that one non-accessibility attribute.
    """
    if node is None:
        return

    role = (node.get("role") or "").strip().lower()
    name = (node.get("name") or "").strip()
    ref = node.get("ref")

    if role in AUGMENTABLE_ROLES and not name:
        resolved = await _resolve_via_dom(frame, ref) if ref else None
        if resolved and resolved.get("name"):
            node["name_raw"] = resolved["name"]
            node["name"] = normalize_name(resolved["name"])
            node["name_source"] = resolved["source"]
        else:
            node["name_source"] = "none"

    if role in SUBMIT_ROLES and ref:
        action = await _resolve_action(frame, ref)
        if action:
            node.setdefault("props", {})["action"] = action

    for child in node.get("children") or []:
        await augment_tree(child, frame)
