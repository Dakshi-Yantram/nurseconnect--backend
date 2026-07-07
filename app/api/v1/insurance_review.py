"""Patch 5B — Insurance review queue + override endpoint.

Backend-only API (no UI). Reuses ``InsuranceCoverageAssessment`` rows
created by the existing insurance engine. Override actions are persisted
back into the same row and also produce a security audit entry.

Routes (mounted at ``/api/insurance``):
    GET  /insurance/review-queue
    GET  /insurance/review/{id}
    POST /insurance/review/{id}/override

Access (centralized helpers in ``app.security.access_control``):
    * admin_clinical
    * admin_super
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user
from app.models.enums import InsuranceCoverageStatus
from app.models.models import InsuranceCoverageAssessment
from app.security.access_control import assert_admin_clinical
from app.services.security_audit_service import log_insurance_override

router = APIRouter(prefix="/insurance", tags=["insurance-review"])


# ---------------------------------------------------------------------------
# Schemas (kept local — these are admin-only payloads)
# ---------------------------------------------------------------------------
class InsuranceOverrideRequest(BaseModel):
    new_coverage_status: InsuranceCoverageStatus = Field(..., description="New decision for the assessment")
    new_coverage_percent: Optional[Decimal] = Field(default=None, ge=0, le=100)
    justification: str = Field(..., min_length=8, max_length=2000)


def _serialize_assessment(a: InsuranceCoverageAssessment) -> dict:
    return {
        "id": str(a.id),
        "booking_id": str(a.booking_id),
        "worker_id": str(a.worker_id),
        "assessment_date": a.assessment_date.isoformat() if a.assessment_date else None,
        "coverage_status": a.coverage_status.value,
        "coverage_percent": float(a.coverage_percent),
        "checklist_complete": a.checklist_complete,
        "consent_obtained": a.consent_obtained,
        "prescription_valid": a.prescription_valid,
        "tier_appropriate": a.tier_appropriate,
        "gps_verified": a.gps_verified,
        "escalation_timely": a.escalation_timely,
        "registration_valid": a.registration_valid,
        "exclusion_reasons": list(a.exclusion_reasons or []),
        "rule_set_version": a.rule_set_version,
        "flagged_for_review": a.flagged_for_review,
        "reviewed_by": str(a.reviewed_by) if a.reviewed_by else None,
        "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
    }


# ---------------------------------------------------------------------------
# GET /insurance/review-queue
# ---------------------------------------------------------------------------
@router.get("/review-queue")
async def list_review_queue(
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = 100,
):
    """Assessments awaiting clinical review.

    Includes both rows explicitly ``flagged_for_review=True`` and rows whose
    coverage_status is ``under_review`` — these are the queue's two sources.
    """
    assert_admin_clinical(current)
    stmt = (
        select(InsuranceCoverageAssessment)
        .where(
            (InsuranceCoverageAssessment.flagged_for_review.is_(True))
            | (InsuranceCoverageAssessment.coverage_status == InsuranceCoverageStatus.under_review)
        )
        .order_by(InsuranceCoverageAssessment.assessment_date.desc())
        .limit(max(1, min(limit, 500)))
    )
    res = await db.execute(stmt)
    return [_serialize_assessment(a) for a in res.scalars().all()]


# ---------------------------------------------------------------------------
# GET /insurance/review/{id}
# ---------------------------------------------------------------------------
@router.get("/review/{assessment_id}")
async def get_review_item(
    assessment_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    assert_admin_clinical(current)
    res = await db.execute(
        select(InsuranceCoverageAssessment).where(InsuranceCoverageAssessment.id == assessment_id)
    )
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Insurance assessment not found")
    return _serialize_assessment(a)


# ---------------------------------------------------------------------------
# POST /insurance/review/{id}/override
# ---------------------------------------------------------------------------
@router.post("/review/{assessment_id}/override")
async def override_review(
    assessment_id: UUID,
    payload: InsuranceOverrideRequest,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    assert_admin_clinical(current)
    if not payload.justification or not payload.justification.strip():
        raise HTTPException(
            status_code=422,
            detail={"code": "JUSTIFICATION_REQUIRED", "message": "Override justification is required"},
        )
    res = await db.execute(
        select(InsuranceCoverageAssessment).where(InsuranceCoverageAssessment.id == assessment_id)
    )
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Insurance assessment not found")

    prev_status = a.coverage_status.value
    prev_percent = float(a.coverage_percent)

    new_percent = payload.new_coverage_percent
    if new_percent is None:
        # Sensible defaults per decision class
        new_percent = {
            InsuranceCoverageStatus.covered: Decimal("100"),
            InsuranceCoverageStatus.conditional: Decimal("50"),
            InsuranceCoverageStatus.not_covered: Decimal("0"),
            InsuranceCoverageStatus.under_review: a.coverage_percent,
        }[payload.new_coverage_status]

    a.coverage_status = payload.new_coverage_status
    a.coverage_percent = Decimal(new_percent)
    a.flagged_for_review = payload.new_coverage_status == InsuranceCoverageStatus.under_review
    a.reviewed_by = current.id
    a.reviewed_at = datetime.now(timezone.utc)
    await db.flush()

    await log_insurance_override(
        db,
        user_id=current.id,
        role=current.role.value,
        assessment_id=a.id,
        previous_decision=prev_status,
        new_decision=a.coverage_status.value,
        previous_coverage_status=prev_status,
        new_coverage_status=a.coverage_status.value,
        previous_coverage_percent=prev_percent,
        new_coverage_percent=float(a.coverage_percent),
        justification=payload.justification.strip(),
    )
    await db.commit()
    await db.refresh(a)
    return _serialize_assessment(a)
