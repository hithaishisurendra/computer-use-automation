"""Accessibility-tree diagnostic against the live MERIDIAN CORE target.

A sibling of scripts/a11y_diagnostic.py, not a replacement. The original walks
CoreServ's frameset/login/search/sub-account flow with CoreServ-specific CSS
selectors and scrubs with `seed_data_scrubber()`, which imports
`coreserv.data`; none of that applies to MERIDIAN, so pointing the existing
script at the new host would have meant rewriting it rather than
reconfiguring it. That fact is itself a finding (see the coupling audit) and
the reason this is a second file: the measurement pass must not change any
core or existing code.

Walks, as teller1: sign on -> main menu -> member inquiry -> search results ->
member record with balances -> funds transfer form -> transfer REVIEW. It
stops there. Nothing irreversible is posted.

Beyond the raw/filtered dumps the original produces, this answers four
questions the new surface raises:

  * the per-transaction hidden token: is it in the accessibility tree at all,
    or DOM-only, and what does it look like;
  * the live clock in the status bar: does it survive into the filtered tree,
    and does it actually change between two consecutive snapshots of the same
    page (a text-based checkpoint would be poisoned if so);
  * the function-key row: does it resolve to controls with usable accessible
    names, or is it inert text;
  * per-control name coverage and which labeling rule fired
    (`name_source`), tabulated rather than left for a human to eyeball.

Usage:
    python -m scripts.a11y_diagnostic_meridian [--base-url https://...] [--headed]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from playwright.async_api import async_playwright, Page

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from capability.profile import load_profile
from capability.sink import RedactionSink
from perception.labeling import AUGMENTABLE_ROLES
from perception.tree import (
    INTERACTIVE_ROLES,
    count_nodes,
    estimate_tokens,
    filter_tree,
    raw_json_text,
    snapshot_all_frames,
    to_compact_text,
)

BASE_URL = "https://web-sample.interface-hiring.com"
EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence" / "a11y_diagnostic_meridian"

# Everything this walk writes goes through the one write path, built from
# MERIDIAN's own profile. The literals it needs -- names, the street address,
# the short phone format -- are declared there, beside every other fact about
# the app, rather than duplicated in this script.
SINK = RedactionSink(load_profile("meridian"))

# Findings accumulate here and are written as one machine-readable file at the
# end, so the report is derived from measurements rather than from prose.
FINDINGS: dict[str, Any] = {"steps": {}, "probes": {}}


# ---------------------------------------------------------------------------
# per-step measurement
# ---------------------------------------------------------------------------


def iter_nodes(node: Optional[dict]):
    if node is None:
        return
    yield node
    for child in node.get("children") or []:
        yield from iter_nodes(child)


def name_audit(tree: Optional[dict]) -> dict[str, Any]:
    """Per-control accessible-name coverage and which rule produced each name."""
    controls = [n for n in iter_nodes(tree) if (n.get("role") or "").lower() in INTERACTIVE_ROLES]
    sources = Counter()
    nameless = []
    rows = []
    for n in controls:
        role = (n.get("role") or "").lower()
        name = (n.get("name") or "").strip()
        source = n.get("name_source") or ("none" if not name else "platform")
        sources[source] += 1
        rows.append({"role": role, "name": name, "name_source": source, "ref": n.get("ref")})
        if not name:
            nameless.append({"role": role, "ref": n.get("ref"), "name_source": source})
    return {
        "interactive_control_count": len(controls),
        "named": len(controls) - len(nameless),
        "nameless": nameless,
        "name_source_counts": dict(sources),
        "controls": rows,
        # Controls in roles the labeling chain is allowed to augment, so the
        # augmentable-vs-platform split is visible rather than inferred.
        "augmentable_controls": sum(
            1 for n in controls if (n.get("role") or "").lower() in AUGMENTABLE_ROLES
        ),
    }


async def dump_step(page: Page, step: str) -> dict[str, Any]:
    frame_snapshots = await snapshot_all_frames(page)

    lines = [
        f"STEP: {step}",
        f"page.url: {page.url}",
        f"frame count: {len(frame_snapshots)}",
        "NOTE: sensitive values below are masked on write. Node and token "
        "counts are measured BEFORE masking, so they reflect what perception "
        "actually produces for an LLM.",
        "=" * 78,
    ]

    step_record: dict[str, Any] = {"url": page.url, "frames": {}}

    for fs in frame_snapshots:
        lines += ["", f"--- FRAME name={fs.frame_name!r} url={fs.frame_url!r} ---"]
        if fs.error:
            lines.append(f"ERROR: {fs.error}")
            continue
        if fs.tree is None:
            lines.append("(no tree returned)")
            continue

        raw_count = count_nodes(fs.tree)
        raw_tokens = estimate_tokens(raw_json_text(fs.tree))
        filtered = filter_tree(fs.tree)
        filtered_count = count_nodes(filtered)
        compact = to_compact_text(filtered)
        filtered_tokens = estimate_tokens(compact)
        audit = name_audit(fs.tree)

        step_record["frames"][fs.frame_name or "(top)"] = {
            "raw_nodes": raw_count,
            "raw_tokens": raw_tokens,
            "filtered_nodes": filtered_count,
            "filtered_tokens": filtered_tokens,
            "name_audit": audit,
        }

        lines += [
            f"raw node count: {raw_count}",
            f"raw estimated tokens (full JSON snapshot): {raw_tokens}",
            f"filtered node count: {filtered_count}",
            f"filtered estimated tokens (compact text): {filtered_tokens}",
            "",
            "interactive controls: "
            f"{audit['named']}/{audit['interactive_control_count']} named "
            f"({audit['name_source_counts']})",
        ]
        if audit["nameless"]:
            lines.append(f"NAMELESS: {json.dumps(audit['nameless'])}")
        lines += ["", "compact rendering:", compact or "(empty after filtering)"]

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    SINK.write_text(EVIDENCE_DIR / f"{step}.txt", "\n".join(lines))
    FINDINGS["steps"][step] = step_record
    print(f"[{step}] wrote {EVIDENCE_DIR / (step + '.txt')}")
    return step_record


# ---------------------------------------------------------------------------
# targeted probes
# ---------------------------------------------------------------------------


async def probe_hidden_token(page: Page) -> dict[str, Any]:
    """Is the per-transaction token in the a11y tree, or DOM-only?"""
    dom = await page.evaluate(
        """() => Array.from(document.querySelectorAll('input[type=hidden]')).map(el => ({
              name: el.name, value: el.value, form_action: el.form ? el.form.getAttribute('action') : null
           }))"""
    )
    trees = [s.tree for s in await snapshot_all_frames(page)]

    def carried_in_tree(values: list[str]) -> list[dict]:
        found = []
        for tree in trees:
            for n in iter_nodes(tree):
                blob = f"{n.get('name') or ''} {n.get('value') or ''}"
                for v in values:
                    if v and v in blob:
                        found.append({"role": n.get("role"), "name": n.get("name"), "match": v})
        return found

    # The token is asked about on its own. The review page's OTHER hidden
    # fields (from/to/amount/memo) are echoed as visible confirmation cells, so
    # lumping them together would report the token as "in the tree" when what
    # is in the tree is the amount beside it.
    token_values = [h["value"] for h in dom if h.get("name") == "_token" and h.get("value")]
    other_values = [h["value"] for h in dom if h.get("name") != "_token" and h.get("value")]
    token_in_tree = carried_in_tree(token_values)
    compact_all = "\n".join(to_compact_text(filter_tree(t)) for t in trees)
    return {
        "hidden_inputs_in_dom": dom,
        "token_found_in_a11y_tree": token_in_tree,
        "token_value_in_compact_rendering": any(v in compact_all for v in token_values),
        "other_hidden_values_echoed_as_visible_nodes": carried_in_tree(other_values),
        "conclusion": (
            "DOM-only: no accessibility node carries the token value"
            if not token_in_tree
            else "the token value is present in the accessibility tree"
        ),
    }


async def probe_live_clock(page: Page) -> dict[str, Any]:
    """Does the status-bar clock reach the filtered tree, and does it move?"""
    def status_lines(text: str) -> list[str]:
        return [ln.strip() for ln in text.splitlines() if "OPR " in ln or "SID " in ln or "NOT SIGNED ON" in ln]

    first = "\n".join(to_compact_text(filter_tree(s.tree)) for s in await snapshot_all_frames(page))
    await asyncio.sleep(1.2)
    await page.reload(wait_until="load")
    second = "\n".join(to_compact_text(filter_tree(s.tree)) for s in await snapshot_all_frames(page))

    a, b = status_lines(first), status_lines(second)
    return {
        "status_bar_in_filtered_tree": bool(a),
        "first_observation": SINK.text("; ".join(a)),
        "second_observation": SINK.text("; ".join(b)),
        "changed_between_loads": a != b,
        "whole_filtered_tree_identical": first == second,
    }


async def probe_function_keys(page: Page) -> dict[str, Any]:
    """Do the F-key affordances resolve as controls with usable names?"""
    anchors = await page.evaluate(
        """() => Array.from(document.querySelectorAll('a')).map(a => ({
              text: (a.textContent||'').trim().slice(0,40), href: a.getAttribute('href')
           }))"""
    )
    fkey_dom = await page.evaluate(
        """() => {
             const out = [];
             document.querySelectorAll('b').forEach(b => {
               const t = (b.textContent||'').trim();
               if (/^F\\d{1,2}$/.test(t)) {
                 const parent = b.parentElement;
                 out.push({
                   key: t,
                   tag: b.tagName,
                   clickable_ancestor: b.closest('a,button') ? b.closest('a,button').tagName : null,
                   surrounding_text: (parent ? parent.textContent : '').replace(/\\s+/g,' ').trim().slice(0,120)
                 });
               }
             });
             return out;
           }"""
    )
    trees = [s.tree for s in await snapshot_all_frames(page)]
    fkey_nodes = []
    for tree in trees:
        for n in iter_nodes(tree):
            nm = (n.get("name") or "")
            if any(f"F{k}" in nm for k in (1, 3, 5, 7, 12)) and nm:
                fkey_nodes.append({"role": n.get("role"), "name": nm[:120]})
    return {
        "fkey_markup": fkey_dom,
        "fkey_bearing_a11y_nodes": fkey_nodes[:10],
        "anchors_on_page": anchors,
        "conclusion": (
            "F-keys are inert text: no <a>/<button> ancestor, so no interactive "
            "accessibility node and no accessible name to target"
            if all(f.get("clickable_ancestor") is None for f in fkey_dom)
            else "at least one F-key resolves to a real control"
        ),
    }


# ---------------------------------------------------------------------------
# the walk
# ---------------------------------------------------------------------------


async def run(base_url: str, headed: bool, operator: str, password: str) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        page = await browser.new_page()

        # --- 01: sign on ---
        await page.goto(f"{base_url}/signon", wait_until="load")
        await dump_step(page, "01_signon")
        FINDINGS["probes"]["function_keys_signon"] = await probe_function_keys(page)

        await page.fill('input[name="operator"]', operator)
        await page.fill('input[name="password"]', password)
        await page.select_option('select[name="branch"]', "MAIN-001")
        async with page.expect_navigation():
            await page.click('input[type="submit"][value="Sign On"]')

        # --- 02: main menu ---
        await dump_step(page, "02_main_menu")
        FINDINGS["probes"]["live_clock"] = await probe_live_clock(page)

        # --- 03: member inquiry ---
        async with page.expect_navigation():
            await page.click('a[href="/members"]')
        await dump_step(page, "03_member_inquiry")

        # --- 04: search results ---
        await page.select_option('select[name="by"]', "number")
        await page.fill('input[name="q"]', "100234")
        async with page.expect_navigation():
            await page.click('input[type="submit"][value="Search"]')
        await dump_step(page, "04_search_results")

        # --- 05: member record with balances ---
        async with page.expect_navigation():
            await page.click('a[href="/members/100234"]')
        await dump_step(page, "05_member_record")

        # --- 06: funds transfer form (nothing submitted yet) ---
        async with page.expect_navigation():
            await page.click('a[href="/members/100234/transfer"]')
        await dump_step(page, "06_transfer_form")
        FINDINGS["probes"]["hidden_token_transfer_form"] = await probe_hidden_token(page)
        FINDINGS["probes"]["function_keys_transfer"] = await probe_function_keys(page)

        # --- 07: review. Reached by filling the form and pressing Continue.
        # Review renders the confirmation screen; it moves no money. The POST
        # step beyond it is not taken.
        await page.select_option('select[name="from"]', "100234-S0001-6")
        await page.select_option('select[name="to"]', "100234-S0001-12")
        await page.fill('input[name="amount"]', "1.00")
        await page.fill('input[name="memo"]', "perception diagnostic")
        async with page.expect_navigation():
            await page.click('input[type="submit"][value="Continue"]')
        await dump_step(page, "07_transfer_review")
        FINDINGS["probes"]["hidden_token_review"] = await probe_hidden_token(page)

        EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
        SINK.write_json(EVIDENCE_DIR / "findings.json", FINDINGS)
        print(f"wrote {EVIDENCE_DIR / 'findings.json'}")

        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--operator", default="teller1")
    parser.add_argument("--password", default="password")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.headed, args.operator, args.password))


if __name__ == "__main__":
    main()
