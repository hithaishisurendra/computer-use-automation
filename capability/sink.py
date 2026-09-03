"""The single write path. Nothing reaches disk or a caller without passing here.

Redaction in this project has never failed in the redaction code. It has
failed six times at a *new surface*, and every fix was a retrofit at a new
call site:

    1. seed PII in accessibility dumps        a new file writer
    2. credentials rendered into page chrome  a new kind of value
    3. over-redaction killing a request       the fix for (1) applied too hard
    4. seed_data_scrubber importing coreserv  a new target, silent degradation
    5. "Ada Lovelace" vs "Lovelace, Ada"      model prose, a new text channel
    6. a locator scoped on a member's name    artifact content, a new surface

The lesson is not "be more careful at each call site" -- that is what was
tried, six times. It is that the number of places able to emit data has to
stop growing. So there is one object that owns every outbound byte, and a
structural test (tests/test_redaction_chokepoint.py) fails the build if any
module writes a file or emits a payload without it.

Three surfaces, because those are the three that have leaked:

    text()      free text -- page dumps, snapshots, model prose
    payload()   structures returned to callers or serialised to disk
    is_sensitive()  a predicate, for content about to be embedded IN an
                artifact (a locator scope, a note) rather than written out

Two sources feed it, and neither is a pattern list maintained here:

    the app profile's redaction declaration -- literals, patterns, chrome
        values, and optionally a fixture module for an app we own
    the artifact's sensitivity taxonomy -- `pii`/`secret`/`identifier`
        declared on inputs and outputs, applied by field name

What it deliberately does NOT do is guess. Incident (3) was over-redaction: a
pattern broad enough to eat `member_savings_balance@1.0.0` destroyed the
context an operator needed to act on an intervention. A sink that masks
everything it cannot classify would be a worse bug than the six it replaces,
because it fails silently in the direction nobody checks. Unclassifiable data
passes through and the sink says what it was working with.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from .redaction import Scrubber, profile_scrubber
from .validate import redact


class RedactionSink:
    """Owns every outbound byte for one run."""

    def __init__(self, profile=None, artifact=None):
        self.profile = profile
        self.artifact = artifact
        self._scrubber: Scrubber = profile_scrubber(profile)
        # Declared sensitivity by field name, from the artifact's own
        # contract. This is the half a pattern rule can never cover: a member
        # name is not a recognisable shape, but the artifact says the output
        # holding it is `pii`.
        self._sensitivity: dict[str, str] = {}
        if artifact is not None:
            for spec in list(getattr(artifact, "inputs", [])) + list(
                getattr(artifact, "outputs", [])
            ):
                self._sensitivity[spec.name] = spec.sensitivity
        # Files this run wrote that could not be scrubbed (screenshots).
        # Recorded rather than ignored: "we did not redact this" is a fact
        # the evidence should carry.
        self.unscrubbable: list[dict[str, str]] = []

    # -- sources ------------------------------------------------------------

    def register_secrets(self, values: Iterable[str]) -> None:
        """Credential values for this run. Write-only: they become
        replacements and are never stored anywhere readable."""
        self._scrubber.register_secrets(values)

    @property
    def sources(self):
        return self._scrubber.sources

    @property
    def degraded(self) -> bool:
        return bool(self._scrubber.sources and self._scrubber.sources.degraded)

    def warning(self) -> Optional[str]:
        """Non-None when this sink is scrubbing by shape rules only."""
        return self._scrubber.sources.warning() if self._scrubber.sources else None

    def describe(self) -> dict[str, Any]:
        d = self._scrubber.sources.describe() if self._scrubber.sources else {"degraded": True}
        d["declared_sensitive_fields"] = sorted(
            n for n, s in self._sensitivity.items() if s != "public"
        )
        if self.unscrubbable:
            d["unscrubbable_files"] = list(self.unscrubbable)
        return d

    # -- the three surfaces -------------------------------------------------

    def text(self, value: str) -> str:
        """Free text: a page dump, a snapshot, a model's own prose."""
        return self._scrubber.scrub(value)

    def payload(self, obj: Any, _key: Optional[str] = None) -> Any:
        """A structure headed for disk or for a caller.

        Two passes that catch different things. A declared-sensitive field is
        masked by its declared sensitivity whatever its value looks like --
        that is how a member name in an output gets masked when no pattern
        would recognise it. Everything else is scrubbed as text, which is how
        a value that was never declared anywhere still gets caught.
        """
        if isinstance(obj, dict):
            return {k: self.payload(v, _key=k) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.payload(v, _key=_key) for v in obj]
        if isinstance(obj, str):
            sensitivity = self._sensitivity.get(_key or "")
            if sensitivity and sensitivity != "public":
                return redact(obj, sensitivity)
            return self._scrubber.scrub(obj)
        if _key and self._sensitivity.get(_key, "public") != "public" and obj is not None:
            return redact(obj, self._sensitivity[_key])
        return obj

    def is_sensitive(self, value: str) -> bool:
        """Whether this text may be embedded IN an artifact.

        The third surface, and the one nobody anticipated: a locator scope, a
        step note, a description. Content that is never "written out" as
        evidence because it becomes part of the capability itself.
        """
        return bool(value) and self._scrubber.scrub(value) != value

    # -- the only writers ---------------------------------------------------

    def write_text(self, path: str | Path, value: str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.text(value), encoding="utf-8")
        return path

    def write_json(self, path: str | Path, obj: Any, indent: int = 2) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.payload(obj), indent=indent, default=str), encoding="utf-8"
        )
        return path

    def append_jsonl(self, path: str | Path, record: dict[str, Any]) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(self.payload(record), default=str) + "\n")
        return path

    def emit(self, obj: Any, indent: int = 2) -> str:
        """A payload returned to a caller: printed by a CLI, and the shape an
        API response will take. Scrubbed for the same reason a file is --
        handing PII to a caller is not safer than writing it to disk."""
        return json.dumps(self.payload(obj), indent=indent, default=str)

    def note_unscrubbable(self, path: str | Path, why: str) -> None:
        """Record a file this sink could not scrub.

        A screenshot of a member record contains everything the page showed
        and no text pass can mask it. Pretending otherwise is worse than
        saying so, so the fact lands in the evidence manifest.
        """
        self.unscrubbable.append({"path": str(path), "why": why})


def null_sink() -> RedactionSink:
    """A sink with no profile: shape rules only, and it says so.

    Exists so a caller without a profile still cannot write unscrubbed --
    the degraded path is loud, not a bypass.
    """
    return RedactionSink(profile=None)
