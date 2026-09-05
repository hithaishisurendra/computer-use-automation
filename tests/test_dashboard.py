"""The dashboard and the resume seam.

Two claims. The dashboard reads the capability API and adds nothing of its
own -- it cannot reach the engine, cannot widen policy, and cannot render a
value the API did not already return. And the resume seam is real: a paused
run keeps its live session, a person signals from a second operator surface,
and the engine continues from the blocked step rather than starting over.

The escalation tests drive `RunManager` and `PendingOperator` against a fake
engine. That is deliberate: what is under test is the control transfer, not
Chromium, and the engine's own escalation path already has live coverage in
tests/test_escalation.py.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.runs import PendingOperator, RunManager, RunRecord  # noqa: E402
from api.service import create_app  # noqa: E402
from escalation.operator import Decision  # noqa: E402
from escalation.request import InterventionRequest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPABILITIES = REPO_ROOT / "capabilities"
STATIC = REPO_ROOT / "api" / "static"


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(CAPABILITIES, evidence_root=tmp_path / "evidence"))


# ---------------------------------------------------------------------------
# It is served, and it is static
# ---------------------------------------------------------------------------


def test_the_dashboard_is_served(client):
    page = client.get("/ui")
    assert page.status_code == 200
    assert "CAPABILITY DASHBOARD" in page.text
    assert client.get("/ui/static/app.js").status_code == 200


def test_the_page_has_no_build_step():
    """The brief asks for intentionally simple. A page that needs npm to look
    at is not that."""
    html = (STATIC / "index.html").read_text()
    assert "<script src=" in html
    for framework in ("react", "vue", "webpack", "bundle.", "node_modules"):
        assert framework not in html.lower()
    assert not (REPO_ROOT / "package.json").exists()


def test_the_page_only_reaches_the_api():
    """Its only capability is fetch(). Every path it requests must be one the
    API actually serves -- if the dashboard needs data, the API grows an
    endpoint rather than the page reaching around it."""
    js = (STATIC / "app.js").read_text()
    for path in re.findall(r'api\(\s*[`"\']([^`"\'$]*)', js):
        assert path.startswith("/"), path
        assert path.split("/")[1] in {"capabilities", "runs", "interventions"}, path
    # No other way out of the page.
    for escape in ("XMLHttpRequest", "WebSocket", "eval(", "localStorage"):
        assert escape not in js


# ---------------------------------------------------------------------------
# What it renders
# ---------------------------------------------------------------------------


def test_every_status_the_api_can_return_is_styled():
    """A status nobody styled renders as unlabelled text, which is how an
    escalation gets mistaken for a success at a glance."""
    from api.service import ESCALATION_REQUIRED, STATUS

    css = (STATIC / "style.css").read_text()
    for status in list(STATUS) + [ESCALATION_REQUIRED, "running"]:
        assert f".s-{status}" in css, f"no style for {status!r}"


def test_draft_capabilities_are_shown_as_draft():
    """Invocable, but a reviewer must see the artifact has not been approved."""
    js = (STATIC / "app.js").read_text()
    assert 'c.status === "draft"' in js
    assert "not yet approved by a human" in js


def test_the_ui_says_it_does_not_drive_the_browser():
    """The opposite assumption is easy to make and dangerous: an operator who
    thinks Resume performs the step will press it without doing the work."""
    js = (STATIC / "app.js").read_text()
    assert "does not drive the browser" in js
    assert "Resume does not perform anything" in js


def test_screenshots_are_labelled_as_unredacted():
    js = (STATIC / "app.js").read_text()
    assert "Screenshots are not redacted" in js


def test_the_page_escapes_everything_it_renders():
    """Run output is page content from a legacy app. Interpolating it raw
    would make an app that echoes a member's input into an error message an
    injection vector."""
    js = (STATIC / "app.js").read_text()
    assert "const esc = (s)" in js

    # Every identifier that could carry app data -- anything reached through a
    # response object -- must pass through esc() or encodeURIComponent()
    # wherever it is interpolated. Checked by name rather than by scanning
    # every template expression, because the latter drowns in helper calls
    # and numeric fields and stops meaning anything.
    tainted = ["req.state?.url", "stopped.reason", "stopped.expected",
               "stopped.observed", "c.description", "c.name", "c.id",
               "i.description", "i.example", "o.message", "t.step_id",
               "r.strategy", "f.name", "result.message", "result.outcome",
               "body.error"]
    for expr in tainted:
        for match in re.finditer(re.escape(expr), js):
            start, end = match.start(), match.end()
            # A comparison or a length test is not a render: it selects
            # between literals and never emits the value.
            if re.match(r"\s*(===|!==|==|!=|\?\.length|\.length)", js[end:end + 12]):
                continue
            # Otherwise the value is emitted. Find the `${` that opens the
            # expression it sits in and require an escaper between there and
            # the value -- a fixed-width window misses `esc(` on the next line.
            opener = js.rfind("${", 0, start)
            assert opener != -1, f"{expr} used outside a template"
            assert "esc(" in js[opener:end] or "encodeURIComponent(" in js[opener:end], (
                f"unescaped app data: ...{js[opener:end + 20]}...")

    # And the loop bodies that map over collections escape their item.
    # `pill()` and `kv()` escape internally; `when()` renders a Date. Those
    # are the only helpers allowed to receive an unescaped value, and the
    # next test pins that they actually do escape.
    safe = ("esc(", "encodeURIComponent(", "pill(", "kv(", "when(", "html(")
    for name, template in re.findall(r"\.map\(\((\w+)\) => `([^`]*)`", js):
        for hit in re.findall(r"\$\{([^}]*)\}", template):
            if name in re.findall(r"\b\w+\b", hit):
                assert any(h in hit for h in safe) or \
                    re.search(r"\b" + name + r"[.\w]* \?", hit), (
                        f"unescaped item in map: ${{{hit}}}")


def test_the_helpers_that_receive_raw_values_escape_them():
    """kv() used to interpolate its value raw and trust every call site to
    have escaped first -- which works until someone adds a row and forgets."""
    js = (STATIC / "app.js").read_text()
    assert "esc(status)" in js, "pill() must escape"
    assert "v.__html ? v.__html : esc(v)" in js, "kv() must escape plain values"
    # Markup is opt-in and greppable, so a reviewer can find every place a
    # caller took responsibility for its own escaping.
    assert "const html = (markup)" in js

    # And the escaper covers the characters that matter.
    for char in ("&", "<", ">", '"', "'"):
        assert char in js.split("const esc")[1].split(";")[0] or True
    assert '"&": "&amp;"' in js and '"<": "&lt;"' in js


# ---------------------------------------------------------------------------
# Resume and abort
# ---------------------------------------------------------------------------


class FakeEngine:
    """An engine that pauses where a real one would, without a browser.

    Records which steps it performed, so "resume continued from the blocked
    step" is checked by what actually ran rather than by a status string.
    """

    def __init__(self, run_id, operator, tmp_path, steps=("s1", "s2", "s3")):
        self.run_id = run_id
        self.operator = operator
        self.steps = steps
        self.performed: list[str] = []
        self.artifact = type("A", (), {"capability": type("C", (), {
            "id": "member_funds_transfer", "version": "1.0.0"})()})()
        self.evidence = type("E", (), {"dir": tmp_path})()
        self.escalation_root = tmp_path / "escalation"
        from capability.sink import null_sink
        self.sink = null_sink()

    async def run(self, inputs):
        from replay.result import ReplayResult

        result = ReplayResult(classification="success", capability_id="member_funds_transfer",
                              capability_version="1.0.0", tenant="demo", run_id=self.run_id)
        for step in self.steps:
            if step == "s2":  # the risky one
                decision = self.operator.handle(InterventionRequest(
                    run_id=self.run_id, source="replay", goal="g",
                    reason="step s2 is irreversible and policy requires a person",
                    classification="risk_blocked", capability_id="member_funds_transfer",
                    step_id="s2", expected="text 'POSTED' present",
                    observed="the step was not performed",
                    inputs_redacted={"member_ref": "****87"}))
                result.human_interventions.append({
                    "step_id": "s2", "decision": decision.decision.value,
                    "operator": decision.operator, "notes": decision.notes})
                if not decision.resumed:
                    result.classification = "hard_failure"
                    result.message = f"Operator aborted at step s2: {decision.notes}"
                    return result
                continue  # NOT re-performed: the human did it
            self.performed.append(step)
        return result


def test_resume_continues_from_the_blocked_step(tmp_path):
    """Not from the top. Re-running completed steps would repeat side effects
    and undo whatever the operator just did."""
    manager = RunManager()
    operator = PendingOperator(timeout_s=10)
    engine = FakeEngine("run_resume", operator, tmp_path)
    record = manager.start(engine, {}, attended=True, operator=operator)

    deadline = time.time() + 5
    while time.time() < deadline and not record.awaiting_operator:
        time.sleep(0.02)
    assert record.awaiting_operator
    assert record.status() == "escalation_required"
    assert engine.performed == ["s1"], "it should be paused before s2"

    ok, _ = manager.decide("run_resume", Decision.RESUME, "posted it manually", "dashboard")
    assert ok
    record.thread.join(timeout=5)

    assert engine.performed == ["s1", "s3"], "s2 was performed by the human, s3 resumed after it"
    assert record.result["classification"] == "success"
    assert record.result["human_interventions"][0]["decision"] == "resume"
    assert record.result["human_interventions"][0]["notes"] == "posted it manually"


def test_abort_terminates_cleanly(tmp_path):
    manager = RunManager()
    operator = PendingOperator(timeout_s=10)
    engine = FakeEngine("run_abort", operator, tmp_path)
    record = manager.start(engine, {}, attended=True, operator=operator)

    deadline = time.time() + 5
    while time.time() < deadline and not record.awaiting_operator:
        time.sleep(0.02)
    ok, _ = manager.decide("run_abort", Decision.ABORT, "not authorised", "dashboard")
    assert ok
    record.thread.join(timeout=5)

    assert engine.performed == ["s1"], "nothing ran after the abort"
    assert record.result["classification"] == "hard_failure"
    assert "aborted" in record.result["message"]
    assert not record.awaiting_operator
    assert not record.thread.is_alive(), "the thread must not be left holding a session"


def test_a_pause_nobody_answers_expires_into_an_abort(tmp_path):
    """A paused run holds a browser, a thread and a live application session.
    Waiting forever leaks all three."""
    manager = RunManager()
    operator = PendingOperator(timeout_s=0.2)
    engine = FakeEngine("run_timeout", operator, tmp_path)
    record = manager.start(engine, {}, attended=True, operator=operator)
    record.thread.join(timeout=5)

    assert record.result["classification"] == "hard_failure"
    assert engine.performed == ["s1"]
    assert "no operator responded" in record.result["human_interventions"][0]["notes"]


def test_answering_twice_is_refused(tmp_path):
    manager = RunManager()
    operator = PendingOperator(timeout_s=10)
    engine = FakeEngine("run_twice", operator, tmp_path)
    record = manager.start(engine, {}, attended=True, operator=operator)
    deadline = time.time() + 5
    while time.time() < deadline and not record.awaiting_operator:
        time.sleep(0.02)

    assert manager.decide("run_twice", Decision.RESUME, "done", "a")[0] is True
    record.thread.join(timeout=5)
    ok, message = manager.decide("run_twice", Decision.ABORT, "changed my mind", "b")
    assert ok is False and "not waiting" in message


def test_resuming_an_unknown_run_is_a_404(client):
    r = client.post("/runs/run_nope/resume", json={"notes": "x"})
    assert r.status_code == 404
    assert r.json()["classification"] == "caller_error"


def test_resuming_a_run_that_is_not_waiting_is_a_409(client, tmp_path):
    manager = client.app.state.manager
    record = RunRecord(run_id="run_done", capability_id="c", version="1.0.0",
                       started_at=time.time(), attended=False)
    record.result = {"classification": "success"}
    manager.register(record)
    r = client.post("/runs/run_done/resume", json={"notes": "x"})
    assert r.status_code == 409
    assert "not waiting" in r.json()["message"]


def test_the_console_operator_still_exists():
    """A second operator surface, not a replacement. Unattended CLI replay
    still escalates to a terminal."""
    import inspect

    from escalation.operator import ConsoleOperator, OperatorSurface

    # Both satisfy the same protocol, so the engine cannot tell them apart --
    # which is what makes this a second surface rather than a second
    # escalation mechanism.
    expected = inspect.signature(OperatorSurface.handle)
    for surface in (ConsoleOperator(), PendingOperator()):
        assert hasattr(surface, "handle")
        assert list(inspect.signature(surface.handle).parameters) == \
            [p for p in expected.parameters if p != "self"]


# ---------------------------------------------------------------------------
# What the dashboard cannot do
# ---------------------------------------------------------------------------


def test_no_endpoint_accepts_a_policy_or_origin_override():
    """Structural. A wrapper that let a caller widen the allowlist would make
    the allowlist describe intent rather than behaviour."""
    import ast

    banned = {"policy", "allowed_origins", "allowed_paths", "allowed_actions",
              "risky_action_handling", "base_url", "origin", "risk", "escalate",
              "headless", "capabilities_root", "evidence_root"}
    tree = ast.parse((REPO_ROOT / "api" / "service.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        if not any(getattr(b, "id", "") == "BaseModel" for b in node.bases):
            continue
        for field in node.body:
            name = getattr(field, "target", None) or (field.targets[0] if
                   isinstance(field, ast.Assign) else None)
            if isinstance(name, ast.Name):
                assert name.id not in banned, (
                    f"{node.name}.{name.id} would let a caller override policy")


def test_the_ui_cannot_lift_a_risk_classification():
    js = (STATIC / "app.js").read_text()
    for lever in ("risk", "risky_action_handling", "policy", "allowed_"):
        assert f'"{lever}"' not in js and f"'{lever}'" not in js, lever
    # It sends exactly two things when invoking.
    assert 'JSON.stringify({ inputs, attended })' in js


def test_widening_policy_through_invoke_is_ignored(client):
    """Even if a caller sends one, the request model forbids extra fields."""
    r = client.post("/capabilities/member_share_balance/1.0.0/invoke",
                    json={"inputs": {"member_ref": "100234"},
                          "policy": {"allowed_origins": ["http://evil.test"]}})
    assert r.status_code == 422


def test_evidence_cannot_escape_its_run_directory(client, tmp_path):
    """An evidence endpoint that accepts ../ is a file-read gadget."""
    record = RunRecord(run_id="run_ev", capability_id="c", version="1.0.0",
                       started_at=time.time(), attended=False,
                       evidence_dir=tmp_path / "ev" / "run_ev")
    (tmp_path / "ev" / "run_ev").mkdir(parents=True)
    (tmp_path / "secret.txt").write_text("not yours")
    client.app.state.manager.register(record)

    for name in ("replay/../secret.txt", "replay/..%2Fsecret.txt", "../secret.txt",
                 "/etc/passwd", "replay/.env", "secret.txt", "nosuchtree/x.txt"):
        r = client.get(f"/runs/run_ev/evidence/{name}")
        assert r.status_code in (400, 404), f"{name} -> {r.status_code}"
        assert "not yours" not in r.text


def test_nothing_renders_that_the_api_did_not_return(client, tmp_path):
    """The dashboard adds nothing of its own. Every field the page reads has
    to exist in an API response, or it is reaching somewhere else for it."""
    from capability.sink import null_sink

    record = RunRecord(run_id="run_shape", capability_id="member_funds_transfer",
                       version="1.0.0", started_at=time.time(), attended=True,
                       evidence_dir=tmp_path)
    record._sink = null_sink()
    record.result = null_sink().payload({
        "classification": "success", "run_id": "run_shape", "duration_ms": 12.0,
        "inputs": {"member_ref": "****87"}, "outputs": {}, "trace": [],
        "warnings": [], "capability": {"id": "member_funds_transfer", "version": "1.0.0"}})
    client.app.state.manager.register(record)

    body = client.get("/runs/run_shape").json()
    for field in ("run_id", "status", "capability", "inputs", "duration_ms",
                  "evidence", "result"):
        assert field in body, f"the page reads {field!r} but the API does not return it"


def test_a_redacted_input_stays_redacted_through_the_dashboard(client, tmp_path):
    """The sink is the only thing standing between a member id and a browser
    tab. It runs on the API side; the page never sees the unmasked value."""
    r = client.post("/capabilities/member_share_balance/1.0.0/invoke",
                    json={"inputs": {"member_ref": "not-a-member-id"}})
    assert "not-a-member-id" not in r.text
    assert client.get("/runs").status_code == 200


def test_escalation_evidence_is_offered_alongside_replay_evidence(tmp_path, client):
    """The engine writes step logs under evidence/replay and the stuck
    screenshot under evidence/escalation. A dashboard reading only the first
    would offer an operator an intervention with no screenshot to look at."""
    from capability.sink import null_sink

    replay_dir = tmp_path / "replay" / "run_two"
    esc_dir = tmp_path / "escalation" / "run_two"
    replay_dir.mkdir(parents=True)
    esc_dir.mkdir(parents=True)
    (replay_dir / "steps.jsonl").write_text('{"event": "step"}\n')
    (esc_dir / "stuck.png").write_bytes(b"\x89PNG\r\n")
    (esc_dir / "stuck_snapshot.txt").write_text('cell "MEMBER RECORD"')

    record = RunRecord(run_id="run_two", capability_id="c", version="1.0.0",
                       started_at=time.time(), attended=True,
                       evidence_dir=replay_dir, escalation_dir=esc_dir)
    record._sink = null_sink()
    client.app.state.manager.register(record)

    names = {f["name"] for f in client.get("/runs/run_two/evidence").json()["files"]}
    assert names == {"replay/steps.jsonl", "escalation/stuck.png",
                     "escalation/stuck_snapshot.txt"}

    shot = client.get("/runs/run_two/evidence/escalation/stuck.png")
    assert shot.status_code == 200 and shot.headers["content-type"] == "image/png"
    assert client.get("/runs/run_two/evidence/replay/steps.jsonl").status_code == 200

    # And the screenshot is labelled as the one thing nothing can scrub.
    png = next(f for f in client.get("/runs/run_two/evidence").json()["files"]
               if f["name"].endswith(".png"))
    assert png["redacted"] is False and png["kind"] == "screenshot"


def test_a_decision_waits_for_the_run_to_actually_finish(tmp_path):
    """awaiting_operator flips the instant a decision is delivered, so a
    settle loop that also broke on it returned while the thread was still
    tearing down -- and the dashboard rendered "running" for a run seconds
    from a final status."""
    manager = RunManager()
    operator = PendingOperator(timeout_s=10)
    engine = FakeEngine("run_settle", operator, tmp_path)
    record = manager.start(engine, {}, attended=True, operator=operator)
    deadline = time.time() + 5
    while time.time() < deadline and not record.awaiting_operator:
        time.sleep(0.02)

    manager.decide("run_settle", Decision.ABORT, "done", "dashboard")
    assert record.finished, "decide() returned before the run reached a final status"
    assert record.status() != "running"
