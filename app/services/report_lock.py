"""Report immutability.

ROOT CAUSE: nothing marked a report as final. ``PUT /visits/{id}/report`` was
*deliberately* allowed after checkout, and vitals / medication / checklist /
documentation writes only checked "is this your booking". The apps hid the Edit
button after completion, but any client (or a replayed request) could still
rewrite a completed clinical record.

Rules
-----
* A visit's report is finalized exactly once, at checkout
  (``finalize_report``): ``report_finalized_at/by`` + a SHA-256 of the content.
* Every write path that mutates report content calls ``assert_report_editable``
  (or ``assert_booking_report_editable`` when it only has a booking id) and gets
  HTTP 409 ``REPORT_FINALIZED`` afterwards — for nurses AND admins.
* Defence in depth: the migration also installs a Postgres trigger that rejects
  such UPDATEs at the database, so a future code path cannot bypass this.
* Legacy rows (completed before the column existed) are treated as finalized
  via ``check_out_at`` / ``status == completed``.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import VisitStatus
from app.models.models import VisitRecord

REPORT_FINALIZED_CODE = "REPORT_FINALIZED"


def is_finalized(visit: Optional[VisitRecord]) -> bool:
    if visit is None:
        return False
    return bool(
        visit.report_finalized_at
        or visit.check_out_at
        or visit.status == VisitStatus.completed
    )


def report_finalized_error() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": REPORT_FINALIZED_CODE,
            "message": "This visit report has been finalized and can no longer be changed.",
        },
    )


def assert_report_editable(visit: Optional[VisitRecord]) -> None:
    if is_finalized(visit):
        raise report_finalized_error()


async def assert_booking_report_editable(db: AsyncSession, booking_id: UUID) -> None:
    res = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    assert_report_editable(res.scalar_one_or_none())


def compute_report_hash(visit: VisitRecord) -> str:
    payload = {
        "care_notes": visit.care_notes,
        "family_summary": visit.family_summary,
        "checklist_responses": visit.checklist_responses,
        "documentation_responses": visit.documentation_responses,
        "check_in_at": visit.check_in_at.isoformat() if visit.check_in_at else None,
        "check_out_at": visit.check_out_at.isoformat() if visit.check_out_at else None,
    }
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def finalize_report(visit: VisitRecord, actor_user_id: Optional[UUID]) -> None:
    """Stamp finalization. Idempotent: never overwrites an existing stamp."""
    if visit.report_finalized_at is not None:
        return
    visit.report_finalized_at = visit.check_out_at or datetime.now(timezone.utc)
    visit.report_finalized_by = actor_user_id
    visit.report_content_hash = compute_report_hash(visit)
