"""Value scrubbing for anything written to disk.

Separated from `capability.validate.redact` on purpose: that function masks a
*known field* by its declared sensitivity ("this output is pii, so mask it").
This module masks *unknown text* -- a page snapshot, a log payload, a raw
accessibility dump -- where the sensitive values are embedded in content
nobody enumerated in advance. Both are needed, and conflating them is how a
full SSN ends up in a diagnostic dump that never declared an SSN field.

Two complementary mechanisms, and the difference matters:

- **Pattern rules** catch shapes: an SSN, an email, a phone number. These
  are the general mechanism, and the only one that works against real data
  where you cannot enumerate what to look for.
- **Registered literals** catch exact known strings: this run's credentials,
  or a fixture's seed values. Exact, no collateral damage, but only
  available when you already know the value.

Production redaction rests on the pattern rules. The literal registry exists
for the cases where we genuinely do know -- our own credentials, and a seed
dataset we own -- and it buys precision that patterns cannot: masking
`44 Poplar Ave, Northridge, CA 91324` without a rule broad enough to eat
every street address in the evidence.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

# Shape-based rules. Deliberately conservative: each targets a format that
# is unambiguous enough that a false positive is very unlikely, because
# over-masking destroys the evidence these files exist to provide.
DEFAULT_PATTERNS: list[tuple[str, str]] = [
    (r"\b\d{3}-\d{2}-\d{4}\b", "<redacted:ssn>"),
    # The final label must be alphabetic. Without that, a capability
    # reference like "member_savings_balance@1.0.0" reads as an email and
    # gets masked out of an intervention request -- destroying exactly the
    # context an operator needs. Over-redaction is a real failure mode, not
    # a safe default.
    (r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b", "<redacted:email>"),
    (r"\b\d{3}-\d{3}-\d{4}\b", "<redacted:phone>"),
]


def mask_identifier(value: str) -> str:
    """Partial mask for identifiers: keep the last two characters.

    Matches `capability.validate.redact`'s treatment of `identifier`
    sensitivity, so an identifier looks the same whether it was masked as a
    declared field or scrubbed out of free text. Keeping a suffix lets an
    operator correlate a run with a record without the full value on disk.
    """
    if len(value) <= 2:
        return "*" * len(value)
    return "*" * (len(value) - 2) + value[-2:]


class Scrubber:
    """Applies pattern rules and registered literals to text."""

    # Below this length a literal is too short to scrub for without
    # mangling unrelated text. Such a value is not a usable secret or
    # identifier anyway; the floor guards against pathological
    # over-redaction, it is not permission to write short secrets.
    MIN_LITERAL = 3

    def __init__(self, patterns: Optional[list[tuple[str, str]]] = None):
        rules = DEFAULT_PATTERNS if patterns is None else patterns
        self._patterns = [(re.compile(p), r) for p, r in rules]
        self._literals: list[tuple[str, str]] = []

    def register_literal(self, value: str, replacement: str) -> None:
        if value and len(value) >= self.MIN_LITERAL:
            self._literals.append((value, replacement))

    def register_secrets(self, values: Iterable[str]) -> None:
        """Credential values. Write-only: they become replacements and are
        never stored anywhere readable, logged, or echoed back."""
        for value in values:
            self.register_literal(value, "<redacted:credential>")

    def register_pii(self, values: Iterable[str]) -> None:
        for value in values:
            self.register_literal(value, "<redacted:pii>")

    def register_identifiers(self, values: Iterable[str]) -> None:
        for value in values:
            self.register_literal(value, mask_identifier(value))

    def scrub(self, text: str) -> str:
        if not text:
            return text
        # Longest literals first: a shorter value can be a substring of a
        # longer one (a member id inside an account number), and replacing
        # the short one first would corrupt the longer match.
        for value, replacement in sorted(self._literals, key=lambda p: -len(p[0])):
            text = text.replace(value, replacement)
        for pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)
        return text

    def scrub_obj(self, obj):
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.scrub_obj(v) for v in obj]
        return obj


def seed_data_scrubber() -> Scrubber:
    """A Scrubber preloaded with CoreServ's seed dataset.

    Only possible because we own the fixture -- a real deployment cannot
    enumerate its members, which is exactly why the pattern rules above are
    the primary mechanism and this is a precision supplement for evidence
    generated against our own app.
    """
    from coreserv.data import MEMBERS

    scrubber = Scrubber()
    scrubber.register_pii(m["ssn"] for m in MEMBERS)
    scrubber.register_pii(m["date_of_birth"] for m in MEMBERS)
    scrubber.register_pii(m["address"] for m in MEMBERS)
    scrubber.register_pii(m["email"] for m in MEMBERS)
    scrubber.register_pii(m["phone"] for m in MEMBERS)
    scrubber.register_pii(f"{m['first_name']} {m['last_name']}" for m in MEMBERS)
    scrubber.register_identifiers(
        a["account_number"] for m in MEMBERS for a in m["accounts"]
    )
    scrubber.register_identifiers(m["member_id"] for m in MEMBERS)
    return scrubber
