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


def name_variants(value: str) -> list[str]:
    """Other orderings a person's name is likely to be written in.

    Back-office consoles render names surname-first ("Lovelace, Ada"), and a
    literal registered in that form does not match the same person written
    naturally. That is not hypothetical: a discovery run's own summary said
    "member 100234 (Ada Lovelace)" and the scrubber, holding the comma form,
    let it through into evidence.

    Free text that restates a value in a different shape is a redaction
    channel nobody enumerates, and the model's prose is exactly that channel.
    Covering the comma flip is cheap and catches the common case; it is not a
    claim to catch every paraphrase.
    """
    if "," not in value:
        return []
    last, _, first = value.partition(",")
    last, first = last.strip(), first.strip()
    if not last or not first:
        return []
    return [f"{first} {last}"]


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
        # Set by profile_scrubber. A bare Scrubber() is pattern-only by
        # construction and says so rather than claiming sources it lacks.
        self.sources: Optional["RedactionSources"] = None

    def add_pattern(self, pattern: str, replacement: str) -> None:
        """An app-specific shape rule, from its profile.

        Needed for values that are neither enumerable nor covered by the
        default shapes -- a per-session id printed into a status bar, or a
        phone format the default rule does not match.
        """
        self._patterns.append((re.compile(pattern), replacement))

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
            for variant in name_variants(value):
                self.register_literal(variant, "<redacted:pii>")

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


class RedactionSources:
    """What a scrubber was actually given, and whether that is enough.

    The phase-1 lesson was that redaction fails quietly: `seed_data_scrubber()`
    imported `coreserv.data`, so on any app without that module it silently
    became pattern-only and let names and street addresses through with
    nothing saying so. A Scrubber therefore now carries an account of its own
    sources, and a degraded one is reported rather than assumed adequate.
    """

    def __init__(self, profile_name: str = "<none>"):
        self.profile_name = profile_name
        self.sources: list[str] = []
        self.literal_count = 0
        self.pattern_count = 0

    @property
    def degraded(self) -> bool:
        """True when only shape-based rules are in play.

        Pattern rules catch an SSN, an email and a phone number by shape.
        They do not catch a member's name or street address, which is exactly
        what a whole-page evidence capture contains.
        """
        return self.literal_count == 0

    def describe(self) -> dict:
        return {
            "profile": self.profile_name,
            "sources": list(self.sources),
            "known_literals": self.literal_count,
            "extra_patterns": self.pattern_count,
            "degraded": self.degraded,
        }

    def warning(self) -> Optional[str]:
        if not self.degraded:
            return None
        return (
            f"redaction is degraded: app profile {self.profile_name!r} supplies no "
            "known-sensitive literals, so evidence is scrubbed by shape rules only. "
            "Names and street addresses on a captured page will NOT be masked."
        )


def profile_scrubber(profile=None) -> Scrubber:
    """Build a Scrubber from an app profile's declared redaction sources.

    Replaces `seed_data_scrubber()`, which imported CoreServ's data module
    from library code. Where the sensitive values come from is knowledge
    about an application, so it is declared in that application's profile:

      literals        values known up front, masked exactly
      patterns        extra shape rules this app needs
      fixture_module  an app whose dataset this repo owns, named in config
                      rather than imported here -- the coupling that mattered
                      was library code importing a specific target's module,
                      not the fixture existing

    A profile supplying no literal source produces a scrubber that says so;
    see `Scrubber.sources`.
    """
    name = getattr(profile, "name", "<none>") if profile is not None else "<none>"
    scrubber = Scrubber()
    scrubber.sources = RedactionSources(name)

    if profile is None:
        return scrubber

    redaction = profile.redaction

    if redaction.fixture_module:
        import importlib

        module = importlib.import_module(redaction.fixture_module)
        members = getattr(module, "MEMBERS", [])
        for m in members:
            for field in ("ssn", "date_of_birth", "address", "email", "phone"):
                if m.get(field):
                    scrubber.register_literal(m[field], "<redacted:pii>")
            if m.get("first_name") and m.get("last_name"):
                scrubber.register_literal(f"{m['first_name']} {m['last_name']}", "<redacted:pii>")
            if m.get("member_id"):
                scrubber.register_identifiers([m["member_id"]])
            for account in m.get("accounts") or []:
                if account.get("account_number"):
                    scrubber.register_identifiers([account["account_number"]])
        scrubber.sources.sources.append(f"fixture_module:{redaction.fixture_module}")

    if redaction.literals:
        scrubber.register_pii(redaction.literals)
        scrubber.sources.sources.append("profile:literals")

    for rule in list(redaction.patterns) + list(profile.chrome_literals):
        if rule.pattern:
            scrubber.add_pattern(rule.pattern, rule.replacement)
        else:
            scrubber.register_literal(rule.value, rule.replacement)
        scrubber.sources.pattern_count += 1
    if profile.chrome_literals:
        scrubber.sources.sources.append("profile:chrome_literals")
    if redaction.patterns:
        scrubber.sources.sources.append("profile:patterns")

    scrubber.sources.literal_count = len(scrubber._literals)
    return scrubber
