"""Per-app profiles: what the engine needs to know about a target application.

The seam this file exists to create: pointing the core at a new application
should be writing a profile, not editing `replay/` or `discovery/`. Everything
here was previously a constant in engine code, and every one of those
constants was silently wrong on a second app -- a marker string that never
matched, a version regex that turned drift detection into a no-op, a dismiss
control that does not exist.

A profile carries knowledge about the *application*, not about a tenant or a
flow. The distinction matters because there are three layers now:

    app profile   what this vendor product looks like       (this file)
    artifact      what this recorded flow does              (capability/schema)
    overlay       how one tenant's install differs          (capability/loader)

Profiles are data, loaded by name from `config/app_profiles/{name}.json`, and
referenced from `target.app_profile` -- defaulting to `target.app`, so an
artifact recorded before profiles existed resolves to the right one without
being edited.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILE_ROOT = Path(__file__).resolve().parent.parent / "config" / "app_profiles"

# Recovery actions a profile may prescribe. The two differ in whether the app
# keeps your place, which is not a detail: CoreServ's interstitial is a
# <button> that re-renders the page underneath it, so dismissing it in place
# is correct. MERIDIAN's is <a href="/menu">, which navigates to the main menu
# and loses the flow's position -- clicking it would "recover" by walking away
# from the step being retried.
RecoveryKind = Literal["dismiss_control", "reload_step_url", "backoff"]


class ProfileModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProfileOutcome(ProfileModel):
    """An app-level business outcome, recognised from page text."""

    name: str
    classification: Literal["business_outcome", "recoverable", "hard_failure"] = "business_outcome"
    text: str = Field(description="Marker text identifying this outcome on the page.")
    message: str = Field(description="What the caller is told. Written for a caller, not a log.")


class ErrorMarkers(ProfileModel):
    """Text that identifies each engine universal on this app.

    Lists rather than single strings because one condition can surface with
    different wording on different screens, and because a profile author
    should not have to pick the one true phrasing.
    """

    session_expired: list[str] = Field(default_factory=list)
    server_error: list[str] = Field(default_factory=list)
    maintenance: list[str] = Field(default_factory=list)


class RecoveryAction(ProfileModel):
    kind: RecoveryKind
    control_role: Optional[str] = Field(
        default=None, description="Role of the control to click, for dismiss_control."
    )
    control_name: Optional[str] = Field(
        default=None, description="Accessible name of that control."
    )
    notes: Optional[str] = None

    @model_validator(mode="after")
    def _shape_matches_kind(self) -> "RecoveryAction":
        if self.kind == "dismiss_control" and not self.control_name:
            raise ValueError("recovery kind 'dismiss_control' requires 'control_name'")
        return self


class ChromeLiteral(ProfileModel):
    """A value the app renders into its own page furniture.

    These leak into evidence on every page, including error pages, and no
    sensitivity-driven masking sees them: they were never declared as a field
    of anything. MERIDIAN's status bar carries a session id and the signed-on
    operator on every screen.

    Exactly one of `value` or `pattern`. `pattern` exists because the
    interesting cases are not constants -- a session id is generated per
    sign-on and cannot be enumerated when the profile is written, which is
    precisely the situation the scrubber's pattern rules exist for.
    """

    value: Optional[str] = None
    pattern: Optional[str] = None
    replacement: str
    description: str

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ChromeLiteral":
        if bool(self.value) == bool(self.pattern):
            raise ValueError("chrome_literals entry needs exactly one of 'value' or 'pattern'")
        if self.pattern is not None:
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"chrome_literals pattern is not a valid regex: {exc}") from exc
        return self


class Redaction(ProfileModel):
    """Where this app's known-sensitive literals come from.

    See docs/phase2-diagnostic.md for why this is declared rather than
    imported. In short: `seed_data_scrubber()` imported `coreserv.data`, so on
    any other target it degraded to pattern-only matching with no signal --
    names and street addresses passed straight through and nothing said so.

    `patterns` is the production mechanism and works everywhere. `literals` is
    for values genuinely known up front. `fixture_module` is an explicit
    escape hatch for an app whose dataset we own and which would be absurd to
    paste into JSON -- it is declared here, in config, rather than imported
    from library code, which is the coupling that mattered.

    A profile that supplies none of the three is degraded, and says so.
    """

    literals: list[str] = Field(default_factory=list)
    patterns: list[ChromeLiteral] = Field(default_factory=list)
    fixture_module: Optional[str] = Field(
        default=None,
        description=(
            "Dotted path to a module exposing MEMBERS, for an app whose seed "
            "dataset this repo owns. Only meaningful for a fixture app."
        ),
    )

    @property
    def has_literal_source(self) -> bool:
        return bool(self.literals or self.fixture_module)


class AuthDefaults(ProfileModel):
    """Login element definitions the loader supplies to an artifact that does
    not declare its own.

    Artifacts recorded before the auth block referenced the element registry
    have no login elements, and they must keep replaying unchanged. Rather
    than rewrite them, the loader injects these under reserved keys. New
    artifacts declare their own and these are never consulted.
    """

    elements: dict[str, dict[str, Any]] = Field(default_factory=dict)
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Credential/parameter name -> element key in `elements`.",
    )
    submit: Optional[str] = Field(default=None, description="Element key of the submit control.")
    path: str = Field(default="/", description="Where this app's sign-on form lives.")
    success_pattern: str = Field(
        default="/",
        description=(
            "URL pattern proving sign-on worked. Was a CLI default of "
            "'/home|/search' -- CoreServ's post-login landing pages, hardcoded "
            "one layer above the constants the profile already removed."
        ),
    )
    credentials_ref: dict[str, str] = Field(
        default_factory=dict,
        description="Credential field -> environment variable NAME. Never a value.",
    )
    role_credentials: dict[str, dict[str, str]] = Field(
        default_factory=dict,
        description=(
            "Credential sets by operator privilege, for apps that gate "
            "functions by role. Env var NAMES only, as above. A capability's "
            "`required_role` selects one."
        ),
    )
    parameters: dict[str, str] = Field(
        default_factory=dict,
        description="Non-secret sign-on values, e.g. a branch code.",
    )


class AppProfile(ProfileModel):
    name: str
    description: str = ""

    content_frame: Optional[str] = Field(
        default=None,
        description=(
            "Name of the frame holding the working area, for a frameset app. "
            "None means the application is a single document -- which is the "
            "common case, and why it is the default rather than a special case."
        ),
    )

    entry_path: Optional[str] = Field(
        default=None,
        description=(
            "Where a flow starts on this app, post-authentication. The default "
            "for discovery's --entry, which was '/search' -- a CoreServ route "
            "baked into the CLI."
        ),
    )

    business_outcomes: list["ProfileOutcome"] = Field(
        default_factory=list,
        description=(
            "Answers this application gives that are legitimate results rather "
            "than faults, and whose meaning does not depend on which flow asked. "
            "Phase 1 assumed every business outcome was flow-specific, on the "
            "grounds that only the flow knows a not-found search is an answer. "
            "MERIDIAN shows that is too strong: 'No member records matched your "
            "search.' can only mean the search found nothing, on any flow that "
            "searches. Flow-specific outcomes still live in the artifact and are "
            "checked FIRST, so a capability can always be more precise than its app."
        ),
    )

    error_markers: ErrorMarkers = Field(default_factory=ErrorMarkers)
    recovery: dict[str, RecoveryAction] = Field(default_factory=dict)

    version_pattern: Optional[str] = Field(
        default=None,
        description=(
            "Regex with one capture group reading the app version off the page. "
            "Null declares that this app shows no version, which switches drift "
            "detection off explicitly instead of by accident."
        ),
    )

    sensitive_labels: list[str] = Field(
        default_factory=list,
        description=(
            "Field labels that name personal data on this app, matched case- "
            "and punctuation-insensitively.\n\n"
            "The FIELD is the durable fact; the value observed during one "
            "discovery run is not. A member with no e-mail recorded yields an "
            "innocuous sample, and classifying from that sample alone would "
            "declare the e-mail output public forever. Which labels name "
            "personal data is knowledge about an application, so it lives "
            "with the application's other knowledge."
        ),
    )

    parameter_aliases: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Field label -> parameter name, for labels too generic to name a "
            "capability's public contract after. The recorder names a parameter "
            "from the label of the field a value was typed into, which works "
            "while apps label fields for what they hold. MERIDIAN's member "
            "search is labelled 'Value' -- correct on screen, beside a 'Search "
            "by' selector, and useless as a parameter name: it would put "
            "`value_ref` in the contract every calling agent reads. Which "
            "entity a generically-labelled field identifies is knowledge about "
            "the app, so it is declared with the app's other knowledge."
        ),
    )

    commit_paths: list[str] = Field(
        default_factory=list,
        description=(
            "Glob patterns for endpoints that commit. A click that LANDS on one "
            "of these is irreversible whatever its label said.\n\n"
            "The verb list is lexical and it misses: MERIDIAN labels its transfer "
            "commit 'Post Transfer' (caught) and its share commit 'Open Share' "
            "(missed), so an entire capability recorded with its post step marked "
            "safe. A false negative here is far worse than the 'Funds Transfer' "
            "false positive -- it means replay performs an irreversible action "
            "unattended. Where the click ENDED UP is observed rather than guessed, "
            "and the recorder already captures it for checkpoints."
        ),
    )

    risk_verbs: list[str] = Field(default_factory=list)
    near_miss_verbs: list[str] = Field(default_factory=list)

    chrome_literals: list[ChromeLiteral] = Field(default_factory=list)
    redaction: Redaction = Field(default_factory=Redaction)
    auth_defaults: Optional[AuthDefaults] = None

    @model_validator(mode="after")
    def _version_pattern_can_match(self) -> "AppProfile":
        """A version pattern that cannot match is worse than none at all.

        Drift detection is a warning system; one that reports nothing looks
        exactly like one with nothing to report. `CoreServ\\s+(\\d+...)` against
        MERIDIAN was a no-op for exactly this reason, and nothing said so. So
        an uncompilable pattern, or one with no capture group, fails at load.
        """
        if self.version_pattern is None:
            return self
        try:
            compiled = re.compile(self.version_pattern)
        except re.error as exc:
            raise ValueError(f"version_pattern is not a valid regex: {exc}") from exc
        if compiled.groups < 1:
            raise ValueError(
                "version_pattern must contain a capture group for the version itself; "
                f"{self.version_pattern!r} has none, so it could never report a version"
            )
        return self

    @property
    def version_re(self) -> Optional[re.Pattern]:
        return re.compile(self.version_pattern) if self.version_pattern else None

    def markers_for(self, condition: str) -> list[str]:
        return list(getattr(self.error_markers, condition, []) or [])

    def matches(self, condition: str, page_text: str) -> Optional[str]:
        """The marker that fired for a condition, if any."""
        for marker in self.markers_for(condition):
            if marker in page_text:
                return marker
        return None


class ProfileError(Exception):
    """A profile is missing or malformed."""


def profile_path(name: str, root: str | Path = PROFILE_ROOT) -> Path:
    return Path(root) / f"{name}.json"


def load_profile(name: str, root: str | Path = PROFILE_ROOT) -> AppProfile:
    """Load an app profile by name.

    A missing profile is an error rather than a silent default. An engine
    running with no error markers would detect no session expiry and no
    interstitial, and would do it quietly -- which is the failure mode this
    whole file exists to remove.
    """
    path = profile_path(name, root)
    if not path.exists():
        available = sorted(p.stem for p in Path(root).glob("*.json")) if Path(root).exists() else []
        raise ProfileError(
            f"no app profile named {name!r} at {path}. Declared profiles: {available or 'none'}. "
            "An app profile is required: without one the engine has no error markers, "
            "no recovery actions and no redaction sources, and would fail silently."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ProfileError(f"{path}: not valid JSON: {exc}") from exc
    data.pop("_comment", None)
    try:
        return AppProfile.model_validate(data)
    except Exception as exc:
        raise ProfileError(f"{path}: invalid app profile: {exc}") from exc


def profile_for(target, root: str | Path = PROFILE_ROOT) -> AppProfile:
    """Resolve the profile a target refers to.

    `app_profile` falls back to `app`, so an artifact written before profiles
    existed names its profile correctly without being edited.
    """
    name = getattr(target, "app_profile", None) or target.app
    return load_profile(name, root)
