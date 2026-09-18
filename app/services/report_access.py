"""Access control + audit trail for the visit care summary.

Reuses the existing audit mechanism — `common_services.audit()` writing to
the `audit_log` table (which already has ip_address / request_id columns
that nothing was filling in). No new table.

Action codes written here (query `audit_log.action LIKE 'visit.report_%'`):

  visit.report_viewed             care summary data fetched for display
  visit.report_pdf_downloaded     a watermarked PDF was generated & served;
                                  the row id IS the "Ref" printed on the PDF
  visit.report_pdf_failed         PDF generation raised (no PHI in the row)
  visit.report_pdf_denied         bad / expired / re-used download link, or
                                  the viewer lost access since it was issued
  visit.report_client_event       browser-reported print / copy / screenshot-
                                  key attempts (client-side, so spoofable)

Audit rows never contain vitals, notes or summaries — only who, what, when,
where from, and which booking.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import CurrentUser
from app.core.rate_limit import client_ip
from app.core.redis_client import redis_client
from app.core.security import create_scoped_token, decode_token
from app.models.models import AuditLog
from app.services.common_services import audit

logger = logging.getLogger(__name__)

ACTION_VIEWED = "visit.report_viewed"
ACTION_PDF_DOWNLOADED = "visit.report_pdf_downloaded"
ACTION_PDF_FAILED = "visit.report_pdf_failed"
ACTION_PDF_DENIED = "visit.report_pdf_denied"
ACTION_CLIENT_EVENT = "visit.report_client_event"

DOWNLOAD_TOKEN_TYPE = "report_download"
VIEW_WORKER = "worker"
VIEW_CONSUMER = "consumer"

# Allow-list for browser-reported events; anything else is rejected (422).
CLIENT_EVENTS = {
    "print_shortcut",   # Ctrl/Cmd+P pressed while the section was mounted
    "print_dialog",     # beforeprint fired (menu print / print-to-PDF)
    "copy_attempt",     # copy / cut / context-menu inside the section
    "screenshot_key",   # PrintScreen key seen (capture itself can't be stopped)
}

_USED_JTI_PREFIX = "report_dl_used"


def request_meta(request: Request) -> Dict[str, Optional[str]]:
    ua = request.headers.get("user-agent") or ""
    return {
        "ip": client_ip(request),
        "request_id": getattr(request.state, "request_id", None),
        "user_agent": ua[:200] or None,
    }


async def audit_report_event(
    db: AsyncSession,
    *,
    actor_id: Optional[UUID],
    actor_type: str,
    action: str,
    booking_id: UUID,
    request: Request,
    details: Optional[Dict[str, Any]] = None,
) -> AuditLog:
    """Write one audit row (flushed, not committed — caller owns the txn)."""
    meta = request_meta(request)
    changes: Dict[str, Any] = {"booking_id": str(booking_id)}
    if meta["user_agent"]:
        changes["user_agent"] = meta["user_agent"]
    if details:
        changes.update(details)
    return await audit(
        db,
        actor_id,
        actor_type,
        action,
        "booking",
        booking_id,
        changes,
        ip_address=meta["ip"],
        request_id=meta["request_id"],
    )


def issue_download_token(current: CurrentUser, booking_id: UUID, view: str) -> str:
    """One-time, ~60s link bound to (user, booking, view)."""
    return create_scoped_token(
        str(current.id),
        DOWNLOAD_TOKEN_TYPE,
        settings.REPORT_DOWNLOAD_TOKEN_TTL_SECONDS,
        {"bid": str(booking_id), "view": view},
    )


class DownloadTokenError(Exception):
    def __init__(self, status: int, code: str, message: str, reason: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.reason = reason  # short machine reason for the audit row


def decode_download_token(token: str, booking_id: UUID) -> Dict[str, Any]:
    if not token or len(token) > 4096:
        raise DownloadTokenError(401, "DOWNLOAD_LINK_INVALID",
                                 "This download link is invalid. Please download the report again.",
                                 "malformed")
    try:
        claims = decode_token(token)
    except ValueError:
        # Covers bad signature AND expiry (jose raises for exp).
        raise DownloadTokenError(401, "DOWNLOAD_LINK_EXPIRED",
                                 "This download link has expired. Please download the report again.",
                                 "expired_or_bad_signature")
    if claims.get("type") != DOWNLOAD_TOKEN_TYPE or claims.get("bid") != str(booking_id) \
            or claims.get("view") not in (VIEW_WORKER, VIEW_CONSUMER) or not claims.get("sub") \
            or not claims.get("jti"):
        raise DownloadTokenError(401, "DOWNLOAD_LINK_INVALID",
                                 "This download link is invalid. Please download the report again.",
                                 "claims_mismatch")
    return claims


async def consume_download_token(claims: Dict[str, Any]) -> None:
    """Single use. Fails CLOSED if Redis is unavailable: better a retry than
    a replayable link to clinical data."""
    key = f"{_USED_JTI_PREFIX}:{claims['jti']}"
    ttl = max(int(settings.REPORT_DOWNLOAD_TOKEN_TTL_SECONDS) * 2, 120)
    try:
        first_use = await redis_client.set(key, "1", nx=True, ex=ttl)
    except Exception:  # noqa: BLE001
        logger.exception("report download: redis unavailable for single-use check")
        raise DownloadTokenError(503, "DOWNLOAD_TEMPORARILY_UNAVAILABLE",
                                 "Downloads are temporarily unavailable. Please try again in a minute.",
                                 "redis_unavailable")
    if not first_use:
        raise DownloadTokenError(410, "DOWNLOAD_LINK_USED",
                                 "This download link was already used. Please download the report again.",
                                 "reused")


def public_api_base(request: Request) -> str:
    """Absolute API origin for links handed to the mobile apps."""
    if settings.PUBLIC_API_URL:
        return settings.PUBLIC_API_URL.rstrip("/")
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "https").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc)
    host = host.split(",")[0].strip()
    return f"{proto}://{host}"


def http_error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message})
