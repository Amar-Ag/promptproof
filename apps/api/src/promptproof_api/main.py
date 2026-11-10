"""The promptproof API.

Deliberately boring: it validates, persists and enqueues. It makes no model call
and runs no scan (ADR-0001), which is what keeps request latency independent of
model latency.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from promptproof_core.db import check_connectivity, dispose_engine, init_engine
from promptproof_core.logging import configure_logging, get_logger
from promptproof_core.settings import Settings, get_settings

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

    init_engine(settings)
    log.info(
        "api.start",
        environment=settings.environment,
        target_model=settings.default_target_model,
        judge_model=settings.judge_model,
    )
    try:
        yield
    finally:
        await dispose_engine()
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

    return app


app = create_app()
