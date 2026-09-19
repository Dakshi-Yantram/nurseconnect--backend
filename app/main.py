"""FastAPI application factory."""
import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from sqlalchemy import text
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.api.v1 import (
    addresses,
    auth_password_reset,
    review_tickets,
    admin,
    auth,
    bookings,
    calls,
    care,
    care_workflow,
    catalog,
    forms,
    composite_care,
    contracts,
    eprescriptions,
    escalations,
    insurance_review,
    messaging,
    notifications,
    offline_sync,
    payments,
    support,
    teleconsult,
    tracking,
    training,
    users,
    visits,
    whatsapp_webhooks,
    workers,
)
from app.api.v1.training import assessments_router as training_assessments_router
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.redis_client import redis_client

logger = logging.getLogger(__name__)
logging.basicConfig(level=getattr(logging, settings.LOG_LEVEL, "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
# Silence noisy passlib bcrypt version probe warning
logging.getLogger("passlib").setLevel(logging.ERROR)


def _ensure_infra_running() -> None:
    """Best-effort start of Postgres + Redis in dev container."""
    try:
        subprocess.run(["pg_isready", "-h", "127.0.0.1", "-p", "5432"], check=True, capture_output=True, timeout=5)
    except Exception:
        try:
            subprocess.run(["service", "postgresql", "start"], capture_output=True, timeout=15)
        except Exception:
            pass
    try:
        subprocess.run(["redis-cli", "ping"], check=True, capture_output=True, timeout=3)
    except Exception:
        try:
            subprocess.Popen(["redis-server", "--daemonize", "yes", "--port", "6379", "--bind", "127.0.0.1"])
            time.sleep(0.5)
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Config problems that silently break user-facing flows (dev-mode OTP
    # left on in production, no mail provider, missing Razorpay secrets).
    # Logged loudly at boot so they're caught on deploy rather than by a
    # customer who never receives a code or whose payment won't verify.
    for problem in settings.startup_warnings():
        logger.error("CONFIG: %s", problem)
    # Unsafe production configuration is fatal, not a log line: e.g. mock
    # payment verification accepts any signature and unsigned webhooks.
    fatal = settings.fatal_config_errors()
    if fatal:
        for problem in fatal:
            logger.critical("FATAL CONFIG: %s", problem)
        raise RuntimeError("Refusing to start with unsafe production configuration: " + " | ".join(fatal))

    if not settings.is_production:
        _ensure_infra_running()  # dev-container convenience only
    if settings.run_seed_on_startup:
        # Run seed (creates tables + initial config)
        from app.seed import main as seed
        try:
            await seed()
            logger.info("Seed completed")
        except Exception as e:
            logger.exception("Seed failed: %s", e)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    version="2.0.0",
    description="NurseConnect backend — production-grade healthcare marketplace platform",
    lifespan=lifespan,
    # Was hard-coded True: in Starlette's debug mode an unhandled exception
    # returns an HTML traceback (source lines, paths) to the caller and
    # bypasses the JSON handler below.
    debug=bool(settings.APP_DEBUG) and not settings.is_production,
    # Don't publish the full API map (every endpoint + schema) in production.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)


def _internal_error_response(request: Request) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    return JSONResponse(
        status_code=500,
        content={"detail": {
            "code": "INTERNAL_ERROR",
            "message": "Something went wrong on our side. Please try again.",
            "request_id": rid,
        }},
        headers={"Cache-Control": "no-store"},
    )


@app.middleware("http")
async def unhandled_exception_middleware(request: Request, call_next):
    """Registered BEFORE CORSMiddleware, so it sits INSIDE it.

    Starlette's ServerErrorMiddleware is the outermost layer — its 500s carry
    no CORS headers, so a cross-origin frontend (Cloudflare -> CloudFront)
    saw every server crash as an opaque "Failed to fetch"/network error and
    could never show the real message. Catching here keeps the response on
    the CORS path. The body is generic: exception text can contain SQL,
    column values or PHI and must not be returned to the client.
    """
    try:
        return await call_next(request)
    except Exception:  # noqa: BLE001
        logger.exception("UNHANDLED ERROR on %s %s rid=%s", request.method, request.url.path,
                         getattr(request.state, "request_id", None))
        return _internal_error_response(request)

app.add_middleware(
    CORSMiddleware,
    # SECURITY: explicit origins only. The old regex allowed ANY
    # *.workers.dev site (anyone can deploy one) with credentials. List your
    # real frontend origin(s) in CORS_ORIGINS / CORS_ORIGIN_REGEX instead.
    allow_origins=settings.cors_origin_list,
    allow_origin_regex=settings.cors_origin_regex,
    # Auth is a Bearer header, not cookies, so credentials aren't needed.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-Id"],
    expose_headers=["X-Request-Id", "Retry-After"],
)

import traceback

@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    # Fallback only (the middleware above normally catches first). Used to
    # return str(exc) — internal error text — straight to the client.
    logger.exception("UNHANDLED ERROR on %s %s", request.method, request.url.path)
    return _internal_error_response(request)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request id for tracing + audit."""
    # Only accept a client-supplied id if it looks like one (it is written to
    # logs and the audit trail).
    supplied = request.headers.get("x-request-id") or ""
    rid = supplied if (0 < len(supplied) <= 64 and supplied.replace("-", "").isalnum()) else uuid.uuid4().hex
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["x-request-id"] = rid
    # Baseline security headers for every API response.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if settings.is_production:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@app.get("/api/health")
async def health():
    """Liveness + dependency probe."""
    db_ok = False
    redis_ok = False
    try:
        async with AsyncSessionLocal() as s:
            await s.execute(text("SELECT 1"))
        db_ok = True
    except Exception as e:
        logger.warning("DB health check failed: %s", e)
    try:
        pong = await redis_client.ping()
        redis_ok = bool(pong)
    except Exception as e:
        logger.warning("Redis health check failed: %s", e)
    from app.integrations import cloudinary_client
    storage_mock = cloudinary_client.mock
    # Mock storage in production means uploads "succeed" but nothing is
    # actually stored — that's a degraded state worth surfacing here, not
    # just in logs.
    is_prod = settings.APP_ENV.lower() in ("production", "prod")
    overall = "ok" if (db_ok and redis_ok and not (storage_mock and is_prod)) else "degraded"
    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={
            "status": overall,
            "app": settings.APP_NAME,
            "env": settings.APP_ENV,
            "version": app.version,
            "checks": {
                "database": db_ok,
                "redis": redis_ok,
                "document_storage_configured": not storage_mock,
            },
        },
    )


@app.get("/api/")
async def root():
    return {"name": settings.APP_NAME, "version": app.version}


# Mount routers all under /api prefix
_API_PREFIX = "/api"
for r in [
    auth.router,
    users.router,
    workers.router,
    addresses.router,
    auth_password_reset.router,
    review_tickets.router,
    catalog.router,
    forms.router,
    bookings.router,
    visits.router,
    visits.notes_router,
    care.router,
    care_workflow.router,
    care_workflow.uploads_router,
    composite_care.router,
    contracts.router,
    eprescriptions.router,
    teleconsult.router,
    escalations.router,
    payments.router,
    tracking.router,
    insurance_review.router,
    offline_sync.router,
    notifications.router,
    training.router,
    training_assessments_router,
    admin.router,
    support.router,
    messaging.router,
    calls.router,
    calls.push_router,
    whatsapp_webhooks.router,
]:
    app.include_router(r, prefix=_API_PREFIX)


# SECURITY: uploaded clinical photos used to be served by a public
# StaticFiles mount at /api/uploads — anyone with (or guessing) a URL could
# download patient photos with no login. They are now served only by the
# authenticated route GET /api/uploads/documentation/{filename} in
# app/api/v1/care_workflow.py, which checks booking access first.