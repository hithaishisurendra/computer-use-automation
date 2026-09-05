"""The capability API: an agent invokes a capability by name and gets a
structured result. Under the hood, every invocation is a deterministic replay.

Design constraints this surface must not break, in order of how easy each
would be to break by accident:

1. **It is a wrapper, not a second engine.** Every invocation goes through
   `ReplayEngine`. There is no path here that resolves an element, drives a
   page, or decides an outcome -- if there were, the guardrails would have a
   second implementation to be absent from.

2. **The guardrails come with it.** The allowlist, the risky-step gate and
   the escalation path are properties of the artifact and the engine, so they
   apply here unchanged. Notably an API invocation is UNATTENDED by
   construction: nobody is at a terminal, so a risky step blocks and this
   surface reports that as its own outcome rather than pretending to have
   asked someone.

3. **Every response goes through the run's sink.** The result object carries
   declared-pii outputs and identifier inputs, and handing them to an HTTP
   caller is not safer than writing them to a file.

4. **A business outcome is not an error.** "No such member" comes back 200
   with `classification: business_outcome`, because the caller asked a
   question and got an answer. Reserving non-2xx for cases where the system
   could not answer is the whole point of the result contract.

HTTP status mapping, and why:

    success            200  the capability did what it says
    business_outcome   200  a legitimate answer the caller must handle
    caller_error       400  the arguments did not satisfy the contract
    escalation_required 202 accepted, stopped, needs a human -- not a failure
    auth_failure       502  our credentials are wrong; not the caller's fault
    hard_failure       502  the app or the flow broke
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from api import catalog as catalog_mod
from api.chat import Chat, describe
from api.runs import PendingOperator, RunManager, RunRecord
from capability.loader import ArtifactError
from capability.sink import null_sink
from escalation.operator import Decision
from replay.engine import ReplayEngine

# A run that stopped on a risky step is not a failure and not a success: it is
# accepted work awaiting a person. 202 is the honest code for that, and the
# body carries what an operator would need to pick it up.
ESCALATION_REQUIRED = "escalation_required"

STATUS = {
    "success": 200,
    "business_outcome": 200,
    ESCALATION_REQUIRED: 202,
    "caller_error": 400,
    "auth_failure": 502,
    "hard_failure": 502,
}


class StrictRequest(BaseModel):
    """Every request model forbids unknown fields.

    Silently ignoring an unrecognised key is how a caller sending
    `{"policy": {"allowed_origins": [...]}}` gets a 200 and reasonably
    concludes the override was honoured. It was not -- but the response gives
    no way to tell, and the next person to add a field may wire it up. A 422
    says plainly that this surface does not take policy.
    """

    model_config = ConfigDict(extra="forbid")


class InvokeRequest(StrictRequest):
    inputs: dict[str, Any] = Field(default_factory=dict)
    tenant: Optional[str] = None
    attended: bool = Field(
        default=False,
        description=(
            "Run with a human available. The browser is headed, a risky step "
            "PAUSES instead of failing, and the run holds its session until an "
            "operator resumes or aborts it. Default false: a plain API caller "
            "has nobody at a terminal, and a run that blocks forever waiting "
            "for one is worse than a run that stops cleanly."
        ),
    )


class ChatRequest(StrictRequest):
    """A request in plain English. Nothing else.

    Deliberately not `capability`, `inputs`, `policy` or `attended`: the chat
    surface chooses a capability through the model and invokes it through the
    same endpoint any other caller uses. A field here that let a caller name
    the capability directly would make this a second invoke path, with its
    own opportunity to skip a check.
    """

    message: str


class DecisionRequest(StrictRequest):
    """What an operator did, in their own words.

    `notes` is not decoration -- it is the only record of what a human
    actually performed on the live session, and the handoff evidence carries
    it forward.
    """

    notes: str = ""
    operator: str = "dashboard"


def _run_record(result, artifact) -> dict[str, Any]:
    """The response body. Built from the replay result rather than
    re-derived, so the API cannot disagree with the evidence on disk."""
    payload = result.as_dict()
    payload["capability"] = {
        "id": artifact.capability.id,
        "version": artifact.capability.version,
        "status": artifact.capability.status,
    }
    if result.classification == "hard_failure" and result.escalation_eligible:
        blocked = any(
            d.get("name") == "risky_action_blocked"
            for t in result.trace for d in (t.detections or [])
        )
        if blocked:
            payload["classification"] = ESCALATION_REQUIRED
            payload["escalation"] = {
                "reason": result.message,
                "step_id": result.failed_step,
                "expected_on_resume": result.expected,
                "how_to_proceed": (
                    "This capability contains an irreversible step and its policy "
                    "requires a person to perform it. An API invocation is unattended "
                    "by construction, so the run stopped before acting. Resume it "
                    "through the operator surface, on the same session."
                ),
            }
    return payload


def create_app(
    capabilities_root: str | Path = catalog_mod.DEFAULT_ROOT,
    evidence_root: str | Path = "evidence/replay",
) -> FastAPI:
    app = FastAPI(
        title="Capability API",
        description=(
            "Recorded capabilities, invocable by name with typed arguments. "
            "Each invocation runs a deterministic replay; no model is in the loop."
        ),
        version="1.0.0",
    )
    app.state.capabilities_root = Path(capabilities_root)
    app.state.evidence_root = Path(evidence_root)
    # Every run this process has served, so a caller can fetch a result and
    # its evidence after the fact. In-memory on purpose: this is a demo
    # surface, and a database would be scaling infrastructure the brief
    # explicitly does not reward.
    app.state.runs: dict[str, dict[str, Any]] = {}
    # Attended runs live on their own threads and can outlast a request.
    app.state.manager = RunManager()
    # One chat mapper for the process. The model client is built lazily, so
    # an API with no model key still serves every other endpoint.
    app.state.chat = Chat()

    def _evidence_dirs(record: RunRecord) -> dict[str, Path]:
        """Both trees a run writes to, keyed by the prefix a URL uses.

        The engine writes step logs and failure captures under evidence/replay
        and escalation captures under evidence/escalation. An operator picking
        up an intervention needs the second one -- that is where the stuck
        screenshot is.
        """
        dirs: dict[str, Path] = {}
        if record.evidence_dir is not None:
            dirs["replay"] = record.evidence_dir
        if record.escalation_dir is not None:
            dirs["escalation"] = record.escalation_dir
        return dirs

    def _evidence_index(record: RunRecord) -> list[dict[str, Any]]:
        files: list[dict[str, Any]] = []
        for prefix, directory in _evidence_dirs(record).items():
            if not directory.exists():
                continue
            for p in sorted(directory.iterdir()):
                if not p.is_file():
                    continue
                files.append({
                    "name": f"{prefix}/{p.name}",
                    "bytes": p.stat().st_size,
                    "kind": "screenshot" if p.suffix == ".png" else "text",
                    # Said out loud rather than left to be discovered: a
                    # screenshot is the one evidence artefact nothing can
                    # scrub.
                    "redacted": p.suffix != ".png",
                })
        return files

    def _live_run_body(record: RunRecord) -> dict[str, Any]:
        """One run, rendered for a caller -- through the run's own sink.

        Sinking the assembled body rather than each part is the point. Piece-
        by-piece scrubbing is how this project leaked six times: whoever adds
        the seventh field forgets, and nothing says so. `record.result` was
        already scrubbed when it was stored; running it through again is
        idempotent and costs nothing.
        """
        body: dict[str, Any] = dict(record.summary())
        if record.result is not None:
            body["result"] = record.result
        if record.error is not None:
            body["error"] = record.error
        if record.awaiting_operator and record.operator.request is not None:
            body["escalation"] = record.operator.request.as_dict()
        body["evidence"] = _evidence_index(record)
        return record._sink.payload(body)

    @app.get("/capabilities")
    def list_capabilities() -> dict[str, Any]:
        entries = catalog_mod.catalog(app.state.capabilities_root)
        return {"count": len(entries), "capabilities": entries}

    @app.get("/capabilities/{capability_id}/{version}")
    def get_capability(capability_id: str, version: str) -> dict[str, Any]:
        try:
            artifact = catalog_mod.load(capability_id, version, root=app.state.capabilities_root)
        except ArtifactError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return catalog_mod.describe(artifact)

    @app.post("/capabilities/{capability_id}/{version}/invoke")
    def invoke(capability_id: str, version: str, body: InvokeRequest) -> JSONResponse:
        try:
            artifact = catalog_mod.load(
                capability_id, version, tenant=body.tenant, root=app.state.capabilities_root
            )
        except ArtifactError as exc:
            # A capability that does not load is not a failed invocation --
            # there was nothing to invoke.
            return JSONResponse(status_code=404, content=null_sink().payload(
                {"classification": "caller_error", "message": str(exc)}))

        if not artifact.provenance.flow_completed:
            return JSONResponse(status_code=409, content=null_sink().payload({
                "classification": "caller_error",
                "message": (
                    f"capability {capability_id!r} was recorded from a flow that did not "
                    "complete -- discovery was blocked at an irreversible step and never "
                    "observed what follows it. It is a record for a human to finish."
                ),
            }))

        if body.attended:
            # A human is available. The browser is headed so they can drive
            # the same session, a risky step pauses rather than failing, and
            # the run keeps its thread until somebody decides.
            operator = PendingOperator()
            engine = ReplayEngine(
                artifact,
                evidence_root=app.state.evidence_root,
                escalate=True,
                operator=operator,
                headless=False,
            )
            record = app.state.manager.start(
                engine, body.inputs, attended=True, operator=operator
            )
            # Give the run a moment to reach its first pause or finish, so the
            # caller usually gets something better than "running".
            deadline = time.time() + 60
            while time.time() < deadline and not record.finished \
                    and not record.awaiting_operator:
                time.sleep(0.1)
            return JSONResponse(status_code=202, content=_live_run_body(record))

        engine = ReplayEngine(
            artifact,
            evidence_root=app.state.evidence_root,
            # Unattended by construction: there is no operator behind a plain
            # HTTP request, and offering one would be a lie the audit trail
            # keeps. The dashboard opts in explicitly instead.
            escalate=False,
        )
        result = asyncio.run(engine.run(dict(body.inputs)))
        payload = _run_record(result, artifact)

        # The run's own sink. Declared-pii outputs and identifier inputs are
        # masked on the way out for the same reason they are on the way to
        # disk -- an HTTP caller is not a more trusted destination than a file.
        body_out = engine.sink.payload(payload)
        app.state.runs[result.run_id] = body_out
        return JSONResponse(status_code=STATUS.get(payload["classification"], 502),
                            content=body_out)

    @app.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        record = app.state.manager.get(run_id)
        if record is not None:
            return _live_run_body(record)
        stored = app.state.runs.get(run_id)
        if stored is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r} in this process")
        return null_sink().payload(stored)

    @app.get("/runs")
    def list_runs() -> dict[str, Any]:
        live = {r.run_id: r.summary() for r in app.state.manager.list()}
        for run_id, stored in app.state.runs.items():
            live.setdefault(run_id, {
                "run_id": run_id, "capability": stored.get("capability"),
                "status": stored["classification"], "attended": False,
                "duration_ms": stored.get("duration_ms"),
                "inputs": stored.get("inputs", {}), "awaiting_operator": False,
            })
        runs = sorted(live.values(), key=lambda r: r.get("started_at") or 0, reverse=True)
        return null_sink().payload({"count": len(runs), "runs": runs})

    @app.get("/interventions")
    def list_interventions() -> dict[str, Any]:
        """Runs paused at an irreversible step, waiting for a person.

        Everything an operator needs to pick one up cold: which capability,
        which step, why it stopped, expected versus observed, and what the
        checkpoint will verify when they hand control back.
        """
        pending = []
        for record in app.state.manager.pending():
            request = record.operator.request
            pending.append(record._sink.payload({
                "run_id": record.run_id,
                "capability": {"id": record.capability_id, "version": record.version},
                "waiting_since": record.operator.waiting_since,
                "timeout_s": record.operator.timeout_s,
                "request": request.as_dict() if request else None,
                "evidence": _evidence_index(record),
            }))
        return {"count": len(pending), "interventions": pending}

    @app.post("/runs/{run_id}/resume")
    def resume_run(run_id: str, body: DecisionRequest) -> JSONResponse:
        """Signal that a human has performed the blocked step.

        The dashboard does NOT drive the browser. A person did the work in the
        live window; this tells the paused run to carry on, and the engine
        re-evaluates the blocked step's checkpoint before continuing -- a
        resume that leaves the checkpoint failing is not a recovery.
        """
        return _decide(run_id, Decision.RESUME, body)

    @app.post("/runs/{run_id}/abort")
    def abort_run(run_id: str, body: DecisionRequest) -> JSONResponse:
        return _decide(run_id, Decision.ABORT, body)

    def _decide(run_id: str, decision: Decision, body: DecisionRequest) -> JSONResponse:
        ok, message = app.state.manager.decide(
            run_id, decision, body.notes, body.operator
        )
        record = app.state.manager.get(run_id)
        if not ok:
            code = 404 if record is None else 409
            return JSONResponse(status_code=code, content=null_sink().payload(
                {"classification": "caller_error", "message": message}))
        return JSONResponse(status_code=200, content=_live_run_body(record))

    @app.get("/runs/{run_id}/evidence")
    def list_evidence(run_id: str) -> dict[str, Any]:
        record = app.state.manager.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r} in this process")
        return {"run_id": run_id, "files": _evidence_index(record)}

    @app.get("/runs/{run_id}/evidence/{tree}/{filename}")
    def get_evidence(run_id: str, tree: str, filename: str):
        """Serve one evidence file this run produced.

        Text files were scrubbed on the way to disk. Screenshots were NOT --
        an image of a member record shows everything the page showed and no
        text pass can mask it. The sink records them as unscrubbable and the
        dashboard labels them; serving them is a deliberate choice for a demo
        surface, not an oversight.
        """
        from fastapi.responses import FileResponse, PlainTextResponse

        record = app.state.manager.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r} in this process")

        # Two separate path segments, so a name can never smuggle a
        # separator: an evidence endpoint that accepts ../ is a file-read
        # gadget. The shape is validated before any path is built rather than
        # sanitised afterwards.
        dirs = _evidence_dirs(record)
        if tree not in dirs or not filename or "/" in filename \
                or "\\" in filename or filename.startswith("."):
            raise HTTPException(
                status_code=400,
                detail="evidence name must be '<replay|escalation>/<filename>'",
            )
        directory = dirs[tree].resolve()
        path = (directory / filename).resolve()
        if not path.is_file() or directory not in path.parents:
            raise HTTPException(status_code=404, detail=f"no evidence file {tree}/{filename}")
        if path.suffix == ".png":
            # An image cannot be text-scrubbed. The sink recorded it as
            # unscrubbable when it was written and the dashboard labels it.
            return FileResponse(path, media_type="image/png")
        # Already scrubbed on the way to disk. Scrubbed again on the way out
        # because the sink is idempotent and a second pass costs nothing --
        # and because "it was clean when we wrote it" is exactly the
        # assumption that failed at every previous surface.
        sink = record._sink or null_sink()
        return PlainTextResponse(
            sink.text(path.read_text(encoding="utf-8", errors="replace"))
        )

    from api.dashboard import mount as mount_dashboard

    mount_dashboard(app)

    @app.post("/chat")
    def chat(body: ChatRequest) -> JSONResponse:
        """Map a request to a capability, invoke it, and say what happened.

        A thin driver over this same API: it reads the catalogue, asks the
        model to pick, and calls the invoke endpoint. It cannot reach past
        that -- there is no path here that loads an artifact, touches policy
        or runs an engine.
        """
        catalog = catalog_mod.catalog(app.state.capabilities_root)
        try:
            choice = app.state.chat.choose(body.message, catalog)
        except Exception as exc:
            # A model that is unavailable must not look like a refusal of the
            # request, which would send the operator rewording a fine one.
            return JSONResponse(status_code=503, content=null_sink().payload({
                "reply": ("I could not reach the language model to interpret that. "
                          "The capabilities are still invocable directly from the "
                          "Catalog tab."),
                "error": f"{type(exc).__name__}: {exc}",
            }))

        if "capability" not in choice:
            invocable = [c for c in catalog if c.get("invocable")]
            return JSONResponse(status_code=200, content=null_sink().payload({
                "reply": choice.get("message"),
                "chose": None,
                # Listing what exists is the useful half of declining.
                "available": [
                    {"id": c["id"], "description": c.get("description"),
                     "required_role": c.get("required_role")}
                    for c in invocable
                ],
            }))

        entry = next((c for c in catalog if c["id"] == choice["capability"]), None)
        if entry is None:
            return JSONResponse(status_code=200, content=null_sink().payload({
                "reply": (f"I picked {choice['capability']!r}, which is not in the "
                          "catalogue. Nothing was run."),
                "chose": choice,
            }))

        invoked = invoke(
            entry["id"], entry["version"],
            InvokeRequest(inputs=choice["inputs"]),
        )
        result = json.loads(bytes(invoked.body).decode("utf-8"))

        # The mapping is shown alongside the result, so a demo viewer can see
        # WHICH capability was chosen and with what arguments rather than
        # inferring it from the outcome.
        return JSONResponse(status_code=200, content=null_sink().payload({
            "reply": describe(result, entry["id"], choice["inputs"]),
            "chose": {
                "capability": entry["id"],
                "version": entry["version"],
                "inputs": result.get("inputs", choice["inputs"]),
                "required_role": entry.get("required_role"),
                "status": entry.get("status"),
            },
            "classification": result.get("classification"),
            "run_id": result.get("run_id"),
            "result": result,
        }))

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
