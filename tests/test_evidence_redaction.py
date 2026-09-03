"""Nothing under evidence/ may contain an unmasked sensitive value.

Evidence is the one place sensitive data leaks by accident rather than by
design: it is captured from whole pages, so it picks up whatever the surface
happens to render next to the fields a flow actually reads. A full SSN
reached evidence/a11y_diagnostic/ exactly that way -- the diagnostic declared
no SSN field, so nothing sensitivity-driven had any reason to mask it.

These tests are the regression guard for that class of mistake. They scan
committed evidence rather than reasoning about which writer produced it, so
a new script that forgets to scrub fails here regardless of how it was
written.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from capability.profile import load_profile
from capability.redaction import Scrubber, mask_identifier, profile_scrubber
from coreserv.data import MEMBERS

REPO_ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = REPO_ROOT / "evidence"

# Binary evidence (screenshots) is excluded: a PNG of a page necessarily
# shows what was on screen, and scrubbing pixels is a different problem from
# scrubbing text. Only failure screenshots are ever written, and they are
# not committed for runs against real data.
TEXT_SUFFIXES = {".txt", ".json", ".jsonl", ".md", ".log", ".csv", ".yaml", ".yml"}

SEED_SSNS = [m["ssn"] for m in MEMBERS]
SEED_ACCOUNTS = [a["account_number"] for m in MEMBERS for a in m["accounts"]]
SEED_DOBS = [m["date_of_birth"] for m in MEMBERS]
SEED_PHONES = [m["phone"] for m in MEMBERS]
SEED_EMAILS = [m["email"] for m in MEMBERS]
SEED_ADDRESSES = [m["address"] for m in MEMBERS]
SEED_NAMES = [f"{m['first_name']} {m['last_name']}" for m in MEMBERS]


def committed_evidence_files() -> list[Path]:
    """Committed text files under evidence/.

    Uses git rather than a filesystem walk so that untracked local run
    output (a developer's scratch replay) does not fail the suite -- the
    invariant being protected is about what ships in the repository.
    """
    out = subprocess.run(
        ["git", "ls-files", "evidence"], capture_output=True, text=True, cwd=REPO_ROOT
    ).stdout.split()
    return [
        REPO_ROOT / f for f in out if Path(f).suffix in TEXT_SUFFIXES and (REPO_ROOT / f).exists()
    ]


def find_hits(values: list[str]) -> dict[str, list[str]]:
    hits: dict[str, list[str]] = {}
    for path in committed_evidence_files():
        content = path.read_text(encoding="utf-8", errors="ignore")
        found = sorted({v for v in values if v in content})
        if found:
            hits[str(path.relative_to(REPO_ROOT))] = found
    return hits


def test_evidence_files_exist_to_scan():
    """Guard against the scans below passing vacuously."""
    files = committed_evidence_files()
    assert len(files) >= 5, f"expected committed evidence to scan, found {files}"


def test_no_seed_ssn_appears_anywhere_under_evidence():
    assert find_hits(SEED_SSNS) == {}


def test_no_seed_account_number_appears_anywhere_under_evidence():
    assert find_hits(SEED_ACCOUNTS) == {}


def test_no_ssn_shaped_string_appears_anywhere_under_evidence():
    """Pattern-level check, so an SSN that is not in the seed data (a typo, a
    hand-written example) is caught too."""
    offenders = {}
    for path in committed_evidence_files():
        content = path.read_text(encoding="utf-8", errors="ignore")
        found = re.findall(r"\b\d{3}-\d{2}-\d{4}\b", content)
        if found:
            offenders[str(path.relative_to(REPO_ROOT))] = sorted(set(found))
    assert offenders == {}


@pytest.mark.parametrize(
    "label,values",
    [
        ("dates of birth", SEED_DOBS),
        ("phone numbers", SEED_PHONES),
        ("email addresses", SEED_EMAILS),
        ("home addresses", SEED_ADDRESSES),
        ("member names", SEED_NAMES),
    ],
)
def test_no_other_seed_pii_appears_under_evidence(label, values):
    assert find_hits(values) == {}, f"unmasked {label} in evidence"


# ---------------------------------------------------------------------------
# The scrubber itself
# ---------------------------------------------------------------------------


def test_scrubber_masks_ssn_by_shape_without_registration():
    """Pattern rules are the mechanism that works against data we cannot
    enumerate, which is the only mechanism a real deployment has."""
    scrubber = Scrubber()
    assert "999-88-7777" not in scrubber.scrub("SSN : 999-88-7777")
    assert "<redacted:ssn>" in scrubber.scrub("SSN : 999-88-7777")


def test_scrubber_masks_email_and_phone_by_shape():
    scrubber = Scrubber()
    out = scrubber.scrub("contact nobody@example.com or 555-111-2222")
    assert "nobody@example.com" not in out
    assert "555-111-2222" not in out


def test_identifiers_are_partially_masked_not_erased():
    """Partial masking is deliberate: an operator has to be able to correlate
    a run with a record, and the results-grid finding is only checkable if
    distinct rows stay distinct."""
    assert mask_identifier("10002") == "***02"
    assert mask_identifier("10004") == "***04"
    assert mask_identifier("10002") != mask_identifier("10004")


def test_longer_literals_are_replaced_first():
    """A member id can be a substring of another value; replacing the short
    one first would corrupt the longer match."""
    scrubber = Scrubber(patterns=[])
    scrubber.register_literal("12345678", "<long>")
    scrubber.register_literal("1234", "<short>")
    assert scrubber.scrub("12345678") == "<long>"


def test_too_short_literals_are_not_registered():
    """Scrubbing a one-character value would mangle unrelated text."""
    scrubber = Scrubber(patterns=[])
    scrubber.register_literal("ab", "<x>")
    assert scrubber.scrub("a fabulous cab") == "a fabulous cab"


def test_seed_scrubber_masks_every_seed_value():
    scrubber = profile_scrubber(load_profile("coreserv"))
    for member in MEMBERS:
        scrubbed = scrubber.scrub(
            f"{member['ssn']} {member['date_of_birth']} {member['phone']} "
            f"{member['email']} {member['address']} "
            f"{member['first_name']} {member['last_name']}"
        )
        assert member["ssn"] not in scrubbed
        assert member["date_of_birth"] not in scrubbed
        assert member["phone"] not in scrubbed
        assert member["email"] not in scrubbed
        assert member["address"] not in scrubbed
        for account in member["accounts"]:
            assert account["account_number"] not in scrubber.scrub(account["account_number"])


def test_scrubber_leaves_non_sensitive_evidence_intact():
    """Over-masking destroys the thing evidence exists for. Roles, control
    names and statuses must survive."""
    scrubber = profile_scrubber(load_profile("coreserv"))
    line = 'row: cell "Status" cell "restricted" link "View" button "Submit" columnheader "Balance"'
    assert scrubber.scrub(line) == line


def test_scrub_obj_walks_nested_structures():
    scrubber = Scrubber()
    payload = {"a": ["SSN 123-45-6789", {"b": "ok"}], "c": 5}
    out = scrubber.scrub_obj(payload)
    assert "123-45-6789" not in str(out)
    assert out["c"] == 5
    assert out["a"][1]["b"] == "ok"


def test_capability_references_are_not_mistaken_for_emails():
    """`member_savings_balance@1.0.0` is a capability reference, not an
    address. Masking it strips the one thing an intervention request most
    needs to name -- over-redaction destroys evidence just as surely as
    under-redaction leaks it."""
    scrubber = Scrubber()
    for reference in (
        "Replay member_savings_balance@1.0.0",
        "extends member_savings_balance@1.0.0",
        "capability@2.10.3",
    ):
        assert scrubber.scrub(reference) == reference


def test_real_emails_are_still_masked_after_the_tld_tightening():
    scrubber = Scrubber()
    for address in ("mary.nguyen@example.com", "a@mail.example.co.uk", "x+tag@sub.domain.org"):
        assert address not in scrubber.scrub(address)
        assert "<redacted:email>" in scrubber.scrub(address)
