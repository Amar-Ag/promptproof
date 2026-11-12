"""The promptproof API.

Deliberately boring: it validates, persists and enqueues. It makes no model call
and runs no scan (ADR-0001), which is what keeps request latency independent of
model latency.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from promptproof_core.db import check_connectivity, dispose_engine, init_engine
from promptproof_core.logging import configure_logging, get_logger
from promptproof_core.metrics import configure_metrics, shutdown_metrics
from promptproof_core.settings import Settings, get_settings
from promptproof_core.telemetry import configure_telemetry, shutdown_telemetry

from .alerting import run_alert_loop
from .routers import scans, suites, targets

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    configure_logging(settings.log_level, settings.environment)

    # Configuration errors should stop the process, not surface later as a
    # confusing runtime failure.
    settings.assert_judge_differs()
    settings.assert_production_safe()

    configure_telemetry(settings, service_name="promptproof-api")
    configure_metrics(settings, service_name="promptproof-api")

    init_engine(settings)

    # Alerting lives here because the API outlives the worker, and the worker
    # dying is precisely what the stalled-queue rule has to be able to report.
    stop_alerts = asyncio.Event()
    alerts = asyncio.create_task(run_alert_loop(settings, stop_alerts))

    log.info(
        "api.start",
        environment=settings.environment,
        target_model=settings.default_target_model,
        judge_model=settings.judge_model,
    )
    try:
        yield
    finally:
        stop_alerts.set()
        alerts.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await alerts
        await dispose_engine()
        shutdown_telemetry()
        shutdown_metrics()
        log.info("api.stop")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="promptproof",
        description="Adversarial testing for LLM system prompts.",
        version="0.1.0",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allow_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

    app.include_router(suites.router, prefix="/api")
    app.include_router(targets.router, prefix="/api")
    app.include_router(scans.router, prefix="/api")

    @app.get("/healthz", tags=["health"], summary="Liveness")
    async def healthz() -> dict[str, str]:
        """The process is up. Deliberately checks nothing else."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"], summary="Readiness")
    async def readyz() -> JSONResponse:
        """Ready to serve traffic: the database answers and the suite registry loaded.

        Separate from liveness so an orchestrator restarts a dead process but
        merely withholds traffic from one whose dependency is down.
        """
        checks: dict[str, str] = {}
        ok = True

        try:
            await check_connectivity()
            checks["database"] = "ok"
        except Exception as exc:  # reported to the caller, not handled
            checks["database"] = f"unavailable: {type(exc).__name__}"
            ok = False

        from promptproof_core.suites import SUITES

        if SUITES:
            checks["suites"] = f"ok ({len(SUITES)})"
        else:
            checks["suites"] = "empty"
            ok = False

        return JSONResponse(
            status_code=200 if ok else 503,
            content={"status": "ready" if ok else "not ready", "checks": checks},
        )

    # Last, so every API and health route is matched before the catch-all.
    _mount_frontend(app)

    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the built frontend, when one is bundled.

    In the free deployment tier the API serves the UI from its own origin, so
    there is one public URL and no CORS (ADR-0005). Under Compose the frontend
    is its own container, `FRONTEND_DIR` is unset, and this does nothing.

    Two things here are easy to get wrong and were, the first time:

    * This runs **last**. Mounting at `/` before the health routes are declared
      makes the mount swallow `/healthz` and `/readyz`.
    * `StaticFiles(html=True)` serves `index.html` for a *directory*, not for an
      arbitrary path, so `/scans/<id>` would 404 even though it is a valid route
      in the app. The catch-all below is what makes client-side routing work on
      a hard refresh or a shared link.
    """
    configured = get_settings().frontend_dir
    if not configured:
        return

    static = Path(configured)
    if not (static / "index.html").is_file():
        # Loud, because a deployment that silently serves no UI looks like a
        # routing bug for as long as it takes someone to check the image.
        log.warning("frontend.missing", path=str(static))
        return

    assets = static / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def frontend(full_path: str) -> FileResponse:
        # An unmatched /api path is a genuine 404, not the app shell. Returning
        # index.html with a 200 would turn a typo in a client call into a
        # baffling "unexpected token <" at the other end.
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")

        candidate = static / full_path
        if full_path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(static / "index.html")

    log.info("frontend.mounted", path=str(static))


app = create_app()
