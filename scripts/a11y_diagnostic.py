"""Accessibility-tree diagnostic against a live CoreServ instance.

Walks the full login -> search -> results -> member detail -> sub-account
(invalid then valid) -> confirmation flow with Playwright/Chromium, and at
each step dumps the raw and perception.tree-filtered accessibility snapshot
of every frame to evidence/a11y_diagnostic/{step}.txt.

This exists to check the perception strategy against a live run, not to
demonstrate it working -- see evidence/a11y_diagnostic/REPORT.txt for the
honest answer on whether role+name actually survived CoreServ's hostile
markup. Interactions here are driven by CSS attribute/text selectors, not
accessible-name locators, deliberately: the point of this script is to
*observe* what the accessibility tree looks like, not to assume it's already
usable for driving actions (that's exactly the open question).

Usage:
    python -m scripts.a11y_diagnostic [--base-url http://127.0.0.1:8800] [--headed]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import httpx
from playwright.async_api import async_playwright, Frame, Page

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from perception.tree import (
    count_nodes,
    estimate_tokens,
    filter_tree,
    raw_json_text,
    snapshot_all_frames,
    to_compact_text,
)

EVIDENCE_DIR = Path(__file__).resolve().parent.parent / "evidence" / "a11y_diagnostic"


async def dump_step(page: Page, step: str) -> None:
    frame_snapshots = await snapshot_all_frames(page)

    lines: list[str] = []
    lines.append(f"STEP: {step}")
    lines.append(f"page.url: {page.url}")
    lines.append(f"frame count: {len(frame_snapshots)}")
    lines.append("=" * 78)

    for fs in frame_snapshots:
        lines.append("")
        lines.append(f"--- FRAME name={fs.frame_name!r} url={fs.frame_url!r} ---")
        if fs.error:
            lines.append(f"ERROR: {fs.error}")
            continue
        if fs.tree is None:
            lines.append("(no tree returned)")
            continue

        raw_count = count_nodes(fs.tree)
        raw_text = raw_json_text(fs.tree)
        raw_tokens = estimate_tokens(raw_text)

        filtered = filter_tree(fs.tree)
        filtered_count = count_nodes(filtered)
        compact = to_compact_text(filtered)
        filtered_tokens = estimate_tokens(compact)

        lines.append(f"raw node count: {raw_count}")
        lines.append(f"raw estimated tokens (full JSON snapshot): {raw_tokens}")
        lines.append(f"filtered node count: {filtered_count}")
        lines.append(f"filtered estimated tokens (compact text): {filtered_tokens}")
        lines.append("")
        lines.append("compact rendering:")
        lines.append(compact if compact else "(empty after filtering)")

    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVIDENCE_DIR / f"{step}.txt"
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[{step}] wrote {out_path}")


def get_frame(page: Page, name: str) -> Frame:
    frame = page.frame(name=name)
    if frame is None:
        raise RuntimeError(f"frame {name!r} not found; page.frames={[f.name for f in page.frames]}")
    return frame


async def run(base_url: str, headed: bool) -> None:
    # Clean fault state before the run so this diagnostic reflects the
    # happy-path structure of the app, not whatever fault a previous run
    # left toggled on.
    async with httpx.AsyncClient() as client:
        await client.post(f"{base_url}/_faults/reset")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        page = await browser.new_page()

        # --- 01: login page (no frames yet) ---
        await page.goto(base_url)
        await dump_step(page, "01_login")

        await page.fill('input[name="username"]', "diagnostic")
        await page.fill('input[name="password"]', "x")
        async with page.expect_navigation():
            await page.click('button:has-text("Login")')

        # --- home frameset now loaded; content frame starts on /search ---
        content = get_frame(page, "content")
        await content.wait_for_load_state()
        await dump_step(page, "02_search")

        await content.fill('input[name="last_name"]', "Nguyen")
        async with page.expect_event("framenavigated", lambda f: f == content):
            await content.click('button:has-text("Submit")')
        await content.wait_for_load_state()
        await dump_step(page, "03_results")

        # Click "View" on a specific row (Mary Nguyen, member 10002) rather
        # than just the first match, since every row's View link is
        # visually and textually identical -- the row is what disambiguates.
        # The [not(.//tr[...])] clause picks the innermost matching <tr>:
        # CoreServ's page-shell and panel wrapper tables are themselves
        # ancestor <tr>s that transitively "contain" every row's text
        # through 3 levels of nesting, so a naive contains(., ...) predicate
        # matches those outer wrapper rows too, not just the target row.
        view_link = content.locator(
            "xpath=//tr[contains(., 'Mary Nguyen')]"
            "[not(.//tr[contains(., 'Mary Nguyen')])]"
            "//a[text()='View']"
        )
        async with page.expect_event("framenavigated", lambda f: f == content):
            await view_link.click()
        await content.wait_for_load_state()
        await dump_step(page, "04_member_detail")

        async with page.expect_event("framenavigated", lambda f: f == content):
            await content.click('a:has-text("Open Sub-Account")')
        await content.wait_for_load_state()
        await dump_step(page, "05_subaccount_form")

        # Invalid submission: deposit below the $25 minimum, terms left
        # unchecked -- two validation errors at once.
        await content.fill('input[name="deposit"]', "5")
        async with page.expect_event("framenavigated", lambda f: f == content):
            await content.click('button:has-text("Submit")')
        await content.wait_for_load_state()
        await dump_step(page, "06_subaccount_invalid")

        # Fix it up and resubmit.
        await content.fill('input[name="deposit"]', "100")
        await content.check('input[name="terms"]')
        await content.fill('input[name="nickname"]', "DiagnosticFund")
        await content.check('input[name="delivery"][value="Electronic"]')
        async with page.expect_event("framenavigated", lambda f: f == content):
            await content.click('button:has-text("Submit")')
        await content.wait_for_load_state()
        await dump_step(page, "07_confirmation")

        await browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8800")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    asyncio.run(run(args.base_url, args.headed))


if __name__ == "__main__":
    main()
