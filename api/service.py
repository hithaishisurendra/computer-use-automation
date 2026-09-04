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
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from api import catalog as catalog_mod
from capability.loader import ArtifactError
from capability.sink import null_sink
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


class InvokeRequest(BaseModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    tenant: Optional[str] = None


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

        engine = ReplayEngine(
            artifact,
            evidence_root=app.state.evidence_root,
            # Unattended by construction: there is no operator behind an HTTP
            # request, and offering one would be a lie the audit trail keeps.
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
        record = app.state.runs.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no run {run_id!r} in this process")
        return record

    @app.get("/runs")
    def list_runs() -> dict[str, Any]:
        return {
            "count": len(app.state.runs),
            "runs": [
                {"run_id": r["run_id"], "capability": r.get("capability"),
                 "classification": r["classification"], "duration_ms": r.get("duration_ms")}
                for r in app.state.runs.values()
            ],
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
