"""The chokepoint, enforced structurally and verified against history.

Redaction has never failed in the redaction code. It failed six times at a
NEW SURFACE, and each fix was a retrofit at a new call site. Being careful at
each call site is what was tried, six times. So the enforcement here is not
"did we scrub" -- it is "can a new consumer emit data at all without going
through the one path", answered by parsing the codebase rather than by
reviewing it.

Two halves:

  * a structural scan that fails if any module writes a file or emits a
    payload outside capability/sink.py
  * a replay of all six historical incidents against the sink, so a
    regression on any of them fails a test rather than reaching a repo
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from capability.profile import AppProfile, load_profile
from capability.sink import RedactionSink, null_sink
from tests import scope

REPO_ROOT = Path(__file__).resolve().parent.parent
# Derived, not listed. `api` had to be added by hand after the dashboard
# shipped, and widening it immediately found two live gaps -- which is the
# argument for never maintaining this list again.
SCANNED = tuple(scope.packages())
CHOKEPOINT = REPO_ROOT / "capability" / "sink.py"

# Writers that put bytes somewhere outside the process.
_WRITE_METHODS = {"write_text", "write_bytes"}

# Receivers that ARE the chokepoint. A call is allowed when it is the sink
# doing the writing.
_SINK_RECEIVERS = {"sink", "self.sink", "SINK", "loop.sink", "self.evidence.sink",
                   "engine.sink", "record._sink", "self._sink"}

# Response classes that put a body on the wire. Same rule as a file write:
# what goes through them has to have been through a sink first.
_RESPONSE_CLASSES = {"JSONResponse", "PlainTextResponse", "HTMLResponse", "Response"}

# Sink methods whose output is already scrubbed. Matched on the method name
# rather than on a receiver spelling, so `sink.payload(...)`,
# `null_sink().payload(...)` and `record._sink.payload(...)` all count -- the
# guarantee comes from which method ran, not from what the variable is called.
_SINK_CALLS = (".payload(", ".emit(", ".text(")


def _sinked_names(tree: ast.AST) -> set[str]:
    """Names in this module whose value provably came from a sink.

    Two shapes: a variable assigned from a sink call, and a function whose
    every `return` is one. Both are how a real module writes this -- refusing
    them would push authors toward inlining, which is not safer, just less
    readable.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            rendered = ast.unparse(node.value)
            if any(marker in rendered for marker in _SINK_CALLS):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            returns = [n for n in ast.walk(node)
                       if isinstance(n, ast.Return) and n.value is not None]
            if returns and all(
                any(marker in ast.unparse(r.value) for marker in _SINK_CALLS)
                for r in returns
            ):
                names.add(node.name)
    return names


def _python_files():
    for package in SCANNED:
        for path in scope.sources(package):
            if path == CHOKEPOINT:
                continue
            yield path


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    try:
        rel = path.relative_to(REPO_ROOT)
    except ValueError:
        rel = path  # a fixture written outside the repo, for the scanner's own tests

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func

        # path.write_text(...) / .write_bytes(...)
        if isinstance(func, ast.Attribute) and func.attr in _WRITE_METHODS:
            receiver = ast.unparse(func.value)
            if receiver not in _SINK_RECEIVERS:
                found.append(f"{rel}:{node.lineno}: {receiver}.{func.attr}(...)")

        # json.dump(obj, fh)
        if isinstance(func, ast.Attribute) and func.attr == "dump" and \
                isinstance(func.value, ast.Name) and func.value.id == "json":
            found.append(f"{rel}:{node.lineno}: json.dump(...)")

        # open(path, "w") / "a"
        if isinstance(func, ast.Name) and func.id == "open":
            mode = next((a.value for a in node.args[1:]
                         if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
            for kw in node.keywords:
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                    mode = kw.value.value
            if any(m in mode for m in ("w", "a", "x")):
                found.append(f"{rel}:{node.lineno}: open(..., {mode!r})")

        # JSONResponse(content=<not from a sink>) -- an HTTP body is an
        # output surface, and the dashboard is the newest one.
        if isinstance(func, ast.Name) and func.id in _RESPONSE_CLASSES:
            content = next(
                (kw.value for kw in node.keywords if kw.arg == "content"),
                node.args[0] if node.args else None,
            )
            if content is not None:
                rendered = ast.unparse(content)
                # Either the call is visibly a sink call, or it is a name
                # whose only assignment in this module is one. Anything else
                # is an unscrubbed body.
                sinked = any(marker in rendered for marker in _SINK_CALLS) or (
                    isinstance(content, ast.Name) and content.id in _sinked_names(tree)
                ) or (
                    isinstance(content, ast.Call)
                    and isinstance(content.func, ast.Name)
                    and content.func.id in _sinked_names(tree)
                )
                if not sinked:
                    found.append(
                        f"{rel}:{node.lineno}: {func.id}(content=...) not from a sink"
                    )

        # print(json.dumps(...)) -- a payload handed to a caller
        if isinstance(func, ast.Name) and func.id == "print":
            for arg in node.args:
                if isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute) \
                        and arg.func.attr == "dumps":
                    found.append(f"{rel}:{node.lineno}: print(json.dumps(...))")

        # obj.open("w") on a Path
        if isinstance(func, ast.Attribute) and func.attr == "open":
            mode = next((a.value for a in node.args
                         if isinstance(a, ast.Constant) and isinstance(a.value, str)), "")
            if any(m in mode for m in ("w", "a", "x")):
                receiver = ast.unparse(func.value)
                if receiver not in _SINK_RECEIVERS:
                    found.append(f"{rel}:{node.lineno}: {receiver}.open({mode!r})")
    return found


def test_nothing_writes_or_emits_outside_the_chokepoint():
    """The load-bearing test. If this fails, a seventh surface just appeared
    and it is not covered -- which is exactly how the previous six happened.

    The fix is to route the write through the sink, not to add an exemption.
    """
    offenders = [v for path in _python_files() for v in _violations(path)]
    assert not offenders, (
        "these write or emit data without going through capability/sink.py:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scan_actually_detects_a_bypass(tmp_path):
    """A structural test that cannot fail is decoration. This proves the
    scanner catches the shape it claims to catch."""
    bad = tmp_path / "bypass.py"
    bad.write_text(
        "import json\n"
        "from pathlib import Path\n"
        "def leak(p, obj):\n"
        "    Path(p).write_text(json.dumps(obj))\n"
        "    print(json.dumps(obj))\n"
        "    with open(p, 'w') as fh:\n"
        "        json.dump(obj, fh)\n",
        encoding="utf-8",
    )
    found = _violations(bad)
    kinds = " ".join(found)
    assert "write_text" in kinds and "print(json.dumps" in kinds
    assert "json.dump(" in kinds and "open(" in kinds


def test_a_sink_write_is_not_flagged(tmp_path):
    ok = tmp_path / "good.py"
    ok.write_text(
        "def emit(sink, p, obj):\n"
        "    sink.write_json(p, obj)\n"
        "    sink.write_text(p, 'x')\n"
        "    print(sink.emit(obj))\n",
        encoding="utf-8",
    )
    assert _violations(ok) == []


# ---------------------------------------------------------------------------
# The six historical incidents, replayed against the chokepoint
# ---------------------------------------------------------------------------


@pytest.fixture
def coreserv_sink():
    return RedactionSink(load_profile("coreserv"))


@pytest.fixture
def meridian_sink():
    return RedactionSink(load_profile("meridian"))


def test_incident_1_seed_pii_in_a_page_dump(coreserv_sink):
    """Phase 1. A member detail screen renders SSN, address and name beside
    the field the flow reads. They arrive as page CONTENT, so nothing
    sensitivity-driven sees them."""
    from coreserv.data import MEMBERS

    m = MEMBERS[0]
    dump = (f'cell "{m["ssn"]}" cell "{m["first_name"]} {m["last_name"]}" '
            f'cell "{m["address"]}" cell "{m["email"]}"')
    out = coreserv_sink.text(dump)
    for value in (m["ssn"], f'{m["first_name"]} {m["last_name"]}', m["address"], m["email"]):
        assert value not in out, f"leaked {value!r}"


def test_incident_2_credentials_rendered_into_page_chrome(coreserv_sink):
    """Phase 1. The app prints the signed-on operator into its own nav frame,
    so a page snapshot carries a credential component no output-level masking
    would ever look at."""
    coreserv_sink.register_secrets(["s3cretoperator"])
    assert "s3cretoperator" not in coreserv_sink.text('cell "User: s3cretoperator"')


def test_incident_3_over_redaction_must_not_return(coreserv_sink):
    """Phase 1, and the one that ran the other way. An email pattern broad
    enough to match `member_savings_balance@1.0.0` destroyed the context an
    operator needed to act on an intervention request. The chokepoint must not
    be more aggressive than the rules it replaced."""
    text = "capability member_savings_balance@1.0.0 failed at step s4"
    assert coreserv_sink.text(text) == text
    # A real address still goes.
    assert "ada@example.com" not in coreserv_sink.text("contact ada@example.com")


def test_incident_4_no_literal_source_is_loud_not_silent():
    """Phase 2. seed_data_scrubber() imported coreserv.data, so on any other
    target it silently became pattern-only and let names and addresses
    through with nothing saying so."""
    bare = RedactionSink(AppProfile(name="unconfigured"))
    assert bare.degraded is True
    warning = bare.warning()
    assert warning and "degraded" in warning and "NOT be masked" in warning
    assert bare.describe()["degraded"] is True
    # And a sink with no profile at all is degraded rather than permissive.
    assert null_sink().degraded is True


def test_incident_5_model_prose_restates_a_name_in_another_shape(meridian_sink):
    """Phase 2. The profile declares "Lovelace, Ada" -- the form the app
    renders. A run summary said "member 100234 (Ada Lovelace)"."""
    out = meridian_sink.text("member 100234 (Ada Lovelace) is 20")
    assert "Ada Lovelace" not in out
    assert "Lovelace, Ada" not in meridian_sink.text('cell "Lovelace, Ada"')


def test_incident_6_a_locator_scope_keyed_on_a_member_name(meridian_sink):
    """Phase 2. Tightening positional rungs scoped a locator on a member's
    name -- content embedded IN the artifact, not written out as evidence."""
    assert meridian_sink.is_sensitive("Lovelace, Ada") is True
    assert meridian_sink.is_sensitive("* From Share") is False
    assert meridian_sink.is_sensitive("{{member_ref}}") is False


def test_the_seventh_surface_a_payload_returned_to_a_caller():
    """Not one of the six: found while building the chokepoint. replay/run.py
    printed the raw result, and an artifact's declared-pii output would have
    gone straight to stdout -- the shape the capability API's response takes."""
    from capability.loader import load_resolved

    artifact = load_resolved(REPO_ROOT / "capabilities", "member_savings_balance", "1.0.0")
    sink = RedactionSink(load_profile("coreserv"), artifact)
    payload = sink.payload({"outputs": {"member_name": "Grace Hopper",
                                        "savings_balance": "8320.10"}})
    # member_name is declared `pii` in the artifact; no pattern recognises a
    # person's name, so only the declared taxonomy catches it.
    assert payload["outputs"]["member_name"] == "<redacted>"
    assert payload["outputs"]["savings_balance"] == "8320.10"


def test_declared_sensitivity_masks_what_no_pattern_would_catch():
    from capability.loader import load_resolved

    artifact = load_resolved(REPO_ROOT / "capabilities", "member_savings_balance", "1.0.0")
    sink = RedactionSink(load_profile("coreserv"), artifact)
    # `member_ref` is declared `identifier`: masked to a correlatable suffix,
    # not destroyed.
    assert sink.payload({"member_ref": "10001"})["member_ref"] == "***01"


def test_the_sink_does_not_drop_what_it_cannot_classify(meridian_sink):
    """Over-redaction is a worse bug than the six, because it fails in the
    direction nobody checks. Unclassifiable data passes through."""
    payload = {"step_id": "s4", "duration_ms": 812.5, "note": "clicked Continue",
               "count": 19, "ok": True, "nothing": None}
    assert meridian_sink.payload(payload) == payload


def test_an_unscrubbable_file_is_recorded_rather_than_assumed_clean(meridian_sink):
    """A screenshot of a member record shows everything the page showed."""
    meridian_sink.note_unscrubbable("evidence/x/failure.png", "screenshot")
    assert meridian_sink.describe()["unscrubbable_files"][0]["path"].endswith("failure.png")


# ---------------------------------------------------------------------------
# The dashboard is the newest output surface
# ---------------------------------------------------------------------------


def test_the_scan_detects_an_unsinked_http_body(tmp_path):
    """An HTTP response body is an output surface, and this project has
    leaked at every one it added. Proves the scanner catches the shape rather
    than merely passing."""
    bad = tmp_path / "leaky_api.py"
    bad.write_text(
        "from fastapi.responses import JSONResponse\n"
        "def handler(record):\n"
        "    return JSONResponse(status_code=200, content=record.raw_result)\n",
        encoding="utf-8",
    )
    found = _violations(bad)
    assert any("JSONResponse" in v for v in found), found


def test_a_sinked_http_body_is_not_flagged(tmp_path):
    ok = tmp_path / "clean_api.py"
    ok.write_text(
        "from fastapi.responses import JSONResponse\n"
        "def handler(sink, payload):\n"
        "    return JSONResponse(status_code=200, content=sink.payload(payload))\n",
        encoding="utf-8",
    )
    assert _violations(ok) == []


def test_a_body_built_by_a_sinking_helper_is_not_flagged(tmp_path):
    """Refusing this would push authors to inline everything, which is not
    safer -- just less readable."""
    ok = tmp_path / "helper_api.py"
    ok.write_text(
        "from fastapi.responses import JSONResponse\n"
        "def _body(record):\n"
        "    return record._sink.payload({'run': record.run_id})\n"
        "def handler(record):\n"
        "    return JSONResponse(status_code=200, content=_body(record))\n",
        encoding="utf-8",
    )
    assert _violations(ok) == []


def test_the_dashboard_module_reaches_nothing():
    """The dashboard serves static files and nothing else. If it could load an
    artifact or drive a page it would be a second way to reach the engine,
    which is exactly what the API was built not to be."""
    import ast

    tree = ast.parse((REPO_ROOT / "api" / "dashboard.py").read_text(encoding="utf-8"))
    imported = {n.module for n in ast.walk(tree)
                if isinstance(n, ast.ImportFrom) and n.module}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import)
                 for a in n.names}
    for banned in ("playwright", "replay", "perception", "capability", "discovery",
                   "escalation"):
        assert not any(m.split(".")[0] == banned for m in imported), (
            f"api/dashboard.py imports {banned}: it must serve files, not reach the engine"
        )


# ---------------------------------------------------------------------------
# Finding 8: no guard carries a hand-maintained scope
# ---------------------------------------------------------------------------


def test_no_structural_guard_hardcodes_its_package_list():
    """The defect this whole audit is about, in the tests themselves.

    Four guards each carried their own list. `api/` was added, none were
    updated, and four guards went on reporting clean over code they never
    read. Scope is now derived from the repository, so a package added
    tomorrow is covered the moment it exists.
    """
    import ast

    literals = {"replay", "perception", "escalation", "capability",
                "discovery", "api", "scripts"}
    offenders = []
    for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # A list or tuple of two or more package names is a scope list.
            if not isinstance(node, (ast.List, ast.Tuple)):
                continue
            names = {e.value for e in node.elts
                     if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            if len(names & literals) < 2:
                continue
            # A list of packages to SCAN is a scope list and must be derived.
            # A list of packages a module may not IMPORT is a different thing
            # that happens to name the same strings -- it is the assertion
            # itself, not the ground it covers, and deriving it would assert
            # nothing. `playwright` in the list is the tell.
            if names - literals:
                continue
            offenders.append(
                f"{path.name}:{node.lineno}: {sorted(names & literals)}")
    assert not offenders, (
        "a guard maintains its own package list; use tests.scope instead:\n  "
        + "\n  ".join(offenders))


def test_scope_covers_every_first_party_package():
    from tests import scope

    covered = set(scope.packages())
    on_disk = {
        p.name for p in REPO_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".") and any(p.glob("*.py"))
    }
    missing = on_disk - covered - set(scope._NOT_RUNTIME)
    assert not missing, f"packages no guard would read: {missing}"


def test_excluding_a_package_from_scope_requires_a_reason():
    """An omission is invisible; a named exclusion is reviewable."""
    from tests import scope

    for name, reason in scope._NOT_RUNTIME.items():
        assert reason and reason.strip(), f"{name} is excluded without a reason"


def test_scope_reads_nested_modules_too():
    """A guard that reads only a package's top level misses a subpackage --
    the same stale-scope failure one directory down."""
    from tests import scope

    assert "rglob" in (REPO_ROOT / "tests" / "scope.py").read_text()
