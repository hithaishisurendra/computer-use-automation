"""Serves the dashboard's static files. Nothing else.

The dashboard is a static page that talks to the capability API over HTTP and
has no other way to reach anything. That is a structural property, not a
convention: this module imports no engine, no perception, no artifact loader
and no filesystem walker, and the page itself is browser JavaScript whose only
capability is `fetch`. If the dashboard needs data, the API grows an endpoint.

`tests/test_redaction_chokepoint.py` and `tests/test_dashboard.py` assert both
halves -- that this module reaches nothing, and that the page renders only
what a redacted API response already contains.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_ROOT = Path(__file__).resolve().parent / "static"


def mount(app: FastAPI, path: str = "/ui") -> FastAPI:
    app.mount(f"{path}/static", StaticFiles(directory=STATIC_ROOT), name="dashboard-static")

    @app.get(path, include_in_schema=False)
    def dashboard() -> FileResponse:
        return FileResponse(STATIC_ROOT / "index.html")

    return app
