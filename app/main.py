"""FastAPI application factory."""
import logging
import os
import subprocess
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
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
    care,
    care_workflow,
    catalog,
    escalations,
    insurance_review,
    notifications,
    offline_sync,
    payments,
    tracking,
    training,
    users,
    visits,
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
    _ensure_infra_running()
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
    debug=True,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    # Also allow the deployed Cloudflare Workers frontends (and localhost) so
    # doc-upload POSTs from *.workers.dev aren't blocked by CORS.
    allow_origin_regex=r"https://.*\.workers\.dev|http://localhost(:\d+)?",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

import traceback

@app.exception_handler(Exception)
async def debug_exception_handler(request: Request, exc: Exception):
    logger.exception("UNHANDLED ERROR on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Attach a request id for tracing + audit."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = rid
    response = await call_next(request)
    response.headers["x-request-id"] = rid
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
    overall = "ok" if (db_ok and redis_ok) else "degraded"
    return JSONResponse(
        status_code=200 if overall == "ok" else 503,
        content={
            "status": overall,
            "app": settings.APP_NAME,
            "env": settings.APP_ENV,
            "version": app.version,
            "checks": {"database": db_ok, "redis": redis_ok},
        },
    )


@app.get("/api/")
async def root():
    return {"name": settings.APP_NAME, "version": app.version, "docs": "/docs"}


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
    bookings.router,
    visits.router,
    visits.notes_router,
    care.router,
    care_workflow.router,
    escalations.router,
    payments.router,
    tracking.router,
    insurance_review.router,
    offline_sync.router,
    notifications.router,
    training.router,
    training_assessments_router,
    admin.router,
]:
    app.include_router(r, prefix=_API_PREFIX)


# Patch 4 — serve uploaded documentation files locally. Public URL prefix
# matches the urls returned by POST /care/workflow/{booking_id}/documentation/file.
_UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "./uploads")
os.makedirs(_UPLOAD_DIR, exist_ok=True)
app.mount("/api/uploads", StaticFiles(directory=_UPLOAD_DIR), name="uploads")