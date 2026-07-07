"""Patch 4 — Dynamic checklist / documentation workflow API.

All endpoints are mounted under ``/api/care/workflow``. The runtime resolves
checklist + documentation templates from the database (package → service →
fallback). The application never hardcodes service names, question IDs, or
documentation requirements: this router is intentionally a thin shell over
``app.services.care_workflow_engine``.
"""
from __future__ import annotations

import logging
import os
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, is_admin
from app.models.enums import (
    ConsentType,
    EscalationLevel,
    EscalationStatus,
)
from app.models.models import (
    Booking,
    ClinicalRuleSet,
    Escalation,
    VisitRecord,
    WorkerProfile,
)
from app.services.care_workflow_engine import (
    WorkflowError,
    list_existing_responses,
    resolve_workflow_for_booking,
    upsert_checklist_response,
    upsert_documentation_item,
    validate_checklist_response,
    validate_documentation_completion,
    validate_documentation_payload,
)
from app.services.clinical_engine import (
    compute_sla_breach,
    evaluate_checklist_payload,
    get_escalation_metadata,
)
from app.services.common_services import audit, notify_parties
from app.services.consent_service import (
    ConsentMissingError,
    require_consent,
)
from app.websockets.manager import booking_topic, manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/care/workflow", tags=["care-workflow"])

# Local file storage root — Patch 4 stores uploads on disk and returns a
# /api/uploads/<file> URL. Cloud storage is intentionally NOT introduced here
# (per problem-statement choice 3b).
UPLOAD_ROOT = os.environ.get("UPLOAD_DIR", "./uploads")
DOC_UPLOAD_DIR = os.path.join(UPLOAD_ROOT, "documentation")
os.makedirs(DOC_UPLOAD_DIR, exist_ok=True)

_PUBLIC_URL_PREFIX = "/api/uploads/documentation"


async def _require_assigned_worker_or_admin(
    booking_id: UUID, current: CurrentUser, db: AsyncSession
) -> tuple[Booking, Optional[WorkerProfile]]:
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    if is_admin(current.role):
        return booking, None
    wres = await db.execute(select(WorkerProfile).where(WorkerProfile.user_id == current.id))
    worker = wres.scalar_one_or_none()
    if not worker or booking.worker_id != worker.id:
        raise HTTPException(status_code=403, detail="Not the assigned worker for this booking")
    return booking, worker


async def _require_worker_for_write(booking_id: UUID, current: CurrentUser, db: AsyncSession) -> tuple[Booking, WorkerProfile]:
    booking, worker = await _require_assigned_worker_or_admin(booking_id, current, db)
    if worker is None:
        raise HTTPException(status_code=403, detail="Only the assigned worker can submit workflow data")
    return booking, worker


def _workflow_error_response(e: WorkflowError) -> JSONResponse:
    payload: Dict[str, Any] = {
        "success": False,
        "code": e.code,
        "message": e.message,
    }
    if e.missing_items:
        payload["missing_items"] = e.missing_items
    return JSONResponse(status_code=e.http_status, content=payload)


# ---------------------------------------------------------------------------
# GET /care/workflow/{booking_id}
# ---------------------------------------------------------------------------
@router.get("/{booking_id}")
async def get_workflow(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_assigned_worker_or_admin(booking_id, current, db)
    try:
        wf = await resolve_workflow_for_booking(booking_id, db)
        status = await validate_documentation_completion(booking_id, None, db)
    except WorkflowError as e:
        return _workflow_error_response(e)

    existing_checklist, existing_doc = await list_existing_responses(db, booking_id)
    return {
        "booking_id": str(booking_id),
        "workflow_source": wf.source,
        "risk_level": wf.risk_level.value,
        "service": {
            "id": str(wf.service.id),
            "service_code": wf.service.service_code,
            "name": wf.service.name,
            "risk_level": wf.service.risk_level.value,
        }
        if wf.service
        else None,
        "package": {
            "id": str(wf.package.id),
            "package_code": wf.package.package_code,
            "name": wf.package.name,
        }
        if wf.package
        else None,
        "checklist_template": status["checklist_template"],
        "documentation_template": status["documentation_template"],
        "family_summary_template": wf.family_summary_template,
        "existing_responses": {
            "checklist": [
                {
                    "id": str(r.id),
                    "question_id": r.question_id,
                    "question_text_snapshot": r.question_text_snapshot,
                    "answer_json": r.answer_json,
                    "is_required": r.is_required,
                    "is_completed": r.is_completed,
                    "answered_at": r.answered_at.isoformat() if r.answered_at else None,
                    "template_version": r.template_version,
                    "phase": r.phase,
                }
                for r in existing_checklist
            ],
            "documentation": [
                {
                    "id": str(r.id),
                    "field_id": r.field_id,
                    "field_label_snapshot": r.field_label_snapshot,
                    "field_type": r.field_type,
                    "value_json": r.value_json,
                    "file_url": r.file_url,
                    "is_required": r.is_required,
                    "blocks_checkout": r.blocks_checkout,
                    "is_completed": r.is_completed,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "template_version": r.template_version,
                }
                for r in existing_doc
            ],
        },
        "completion_status": {
            "can_checkout": status["can_checkout"],
            "missing_items": status["missing_items"],
            "blocking_items": status["blocking_items"],
        },
    }


# ---------------------------------------------------------------------------
# POST /care/workflow/{booking_id}/responses
# ---------------------------------------------------------------------------
@router.post("/{booking_id}/responses")
async def submit_checklist_responses(
    booking_id: UUID,
    payload: Dict[str, Any],
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking, worker = await _require_worker_for_write(booking_id, current, db)
    # Patch 5A — service consent gate
    try:
        await require_consent(
            db,
            patient_id=booking.patient_id,
            consent_type=ConsentType.service,
            booking_id=booking.id,
            action="submit clinical checklist responses",
        )
    except ConsentMissingError as ce:
        raise HTTPException(
            status_code=403,
            detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
        ) from None
    try:
        wf = await resolve_workflow_for_booking(booking_id, db)
    except WorkflowError as e:
        return _workflow_error_response(e)
    if not wf.checklist_template:
        raise HTTPException(status_code=400, detail={"code": "NO_CHECKLIST_TEMPLATE", "message": "No checklist template configured for this booking"})

    # payload may be a single response or a list of responses for batch submit.
    raw_items: List[Dict[str, Any]] = []
    if isinstance(payload.get("responses"), list):
        raw_items = list(payload["responses"])
    elif isinstance(payload.get("response"), dict):
        raw_items = [payload["response"]]
    elif "question_id" in payload:
        raw_items = [payload]
    else:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAYLOAD", "message": "Expected 'question_id'/'response'/'responses'"})

    is_offline_submitted = bool(payload.get("is_offline_submitted", False))

    # Pull (or lazily create) the visit record so responses link to it.
    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()

    saved: List[Dict[str, Any]] = []
    validated_items: List[Dict[str, Any]] = []
    try:
        for item in raw_items:
            validated, _ = validate_checklist_response(item, wf.checklist_template)
            validated_items.append(validated)
            row = await upsert_checklist_response(
                db,
                booking=booking,
                visit=visit,
                worker_id=worker.id,
                template=wf.checklist_template,
                validated=validated,
                is_offline_submitted=is_offline_submitted,
            )
            await db.flush()
            saved.append(
                {
                    "id": str(row.id),
                    "question_id": row.question_id,
                    "is_completed": row.is_completed,
                }
            )
    except WorkflowError as e:
        await db.rollback()
        return _workflow_error_response(e)

    # Patch 5A — Clinical Rule Engine execution on submitted responses
    rule_set = None
    if booking.rule_set_id_snapshot:
        rres = await db.execute(select(ClinicalRuleSet).where(ClinicalRuleSet.id == booking.rule_set_id_snapshot))
        rule_set = rres.scalar_one_or_none()
    rule_eval = evaluate_checklist_payload(rule_set, validated_items)
    if rule_eval["escalation_triggered"] and rule_set is not None and visit is not None:
        level = rule_eval["escalation_level"]
        meta = get_escalation_metadata(rule_set, level)
        esc = Escalation(
            booking_id=booking.id,
            visit_record_id=visit.id,
            worker_id=worker.id,
            patient_id=booking.patient_id,
            level=EscalationLevel(level),
            status=EscalationStatus.open,
            trigger_type="checklist_response",
            trigger_details={
                "abnormal_flags": rule_eval["abnormal_flags"],
                "notified_parties": rule_eval["notified_parties"],
                "question_ids": [it["question_id"] for it in validated_items],
            },
            notes=f"Auto-escalation from checklist responses: {', '.join(rule_eval['abnormal_flags'])}",
            notified_parties=meta.get("notify"),
            sla_minutes=meta.get("sla_minutes"),
            sla_breach_at=compute_sla_breach(meta.get("sla_minutes")),
            auto_call_112=bool(meta.get("auto_call_112")),
            rule_set_id=rule_set.id,
            rule_set_version=rule_set.version,
        )
        db.add(esc)
        visit.escalation_triggered = True
        await db.flush()
        await notify_parties(
            db,
            meta.get("notify", []) or [],
            {"booking_id": str(booking.id), "escalation_id": str(esc.id)},
            template_code="checklist_escalation",
            title=f"Clinical alert: {level}",
            body=f"Flags: {', '.join(rule_eval['abnormal_flags'])}",
        )
        await manager.broadcast(
            booking_topic(booking.id),
            {"type": "escalation.created", "level": level, "flags": rule_eval["abnormal_flags"]},
        )

    await audit(
        db, current.id, current.role.value, "care_workflow.checklist", "booking", booking.id,
        {
            "items": [s["question_id"] for s in saved],
            "template_version": wf.checklist_template.version,
            "rule_evaluation": rule_eval,
        },
    )
    await db.commit()
    status = await validate_documentation_completion(booking_id, visit.id if visit else None, db)
    return {"saved": saved, "completion_status": status, "rule_evaluation": rule_eval}


# ---------------------------------------------------------------------------
# POST /care/workflow/{booking_id}/documentation
# ---------------------------------------------------------------------------
@router.post("/{booking_id}/documentation")
async def submit_documentation_item(
    booking_id: UUID,
    payload: Dict[str, Any],
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking, worker = await _require_worker_for_write(booking_id, current, db)
    try:
        wf = await resolve_workflow_for_booking(booking_id, db)
    except WorkflowError as e:
        return _workflow_error_response(e)
    if not wf.documentation_template:
        raise HTTPException(status_code=400, detail={"code": "NO_DOCUMENTATION_TEMPLATE", "message": "No documentation template configured for this booking"})

    vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
    visit = vres.scalar_one_or_none()

    items: List[Dict[str, Any]] = []
    if isinstance(payload.get("items"), list):
        items = list(payload["items"])
    elif "field_id" in payload:
        items = [payload]
    else:
        raise HTTPException(status_code=400, detail={"code": "INVALID_PAYLOAD", "message": "Expected 'field_id' or 'items'"})

    # Patch 5A — if any submitted item is a clinical photo OR the template
    # demands photo_consent_required, gate on photo consent.
    template_requires_photo_consent = bool(getattr(wf.documentation_template, "photo_consent_required", False))
    item_has_photo = False
    for it in items:
        fid = it.get("field_id")
        fdef = next((f for f in (wf.documentation_template.mandatory_fields or []) if f.get("field_id") == fid), None)
        ftype = (fdef or {}).get("type") if fdef else None
        if ftype == "photo" or it.get("file_url"):
            item_has_photo = True
            break
    if template_requires_photo_consent or item_has_photo:
        try:
            await require_consent(
                db,
                patient_id=booking.patient_id,
                consent_type=ConsentType.photo,
                booking_id=booking.id,
                action="upload clinical photos",
            )
        except ConsentMissingError as ce:
            raise HTTPException(
                status_code=403,
                detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
            ) from None

    is_offline_submitted = bool(payload.get("is_offline_submitted", False))
    saved: List[Dict[str, Any]] = []
    try:
        for it in items:
            validated = validate_documentation_payload(it, wf.documentation_template)
            row = await upsert_documentation_item(
                db,
                booking=booking,
                visit=visit,
                worker_id=worker.id,
                template=wf.documentation_template,
                validated=validated,
                is_offline_submitted=is_offline_submitted,
            )
            await db.flush()
            saved.append(
                {
                    "id": str(row.id),
                    "field_id": row.field_id,
                    "is_completed": row.is_completed,
                    "file_url": row.file_url,
                }
            )
    except WorkflowError as e:
        await db.rollback()
        return _workflow_error_response(e)

    await audit(
        db, current.id, current.role.value, "care_workflow.documentation", "booking", booking.id,
        {"items": [s["field_id"] for s in saved], "template_version": wf.documentation_template.version},
    )
    await db.commit()
    status = await validate_documentation_completion(booking_id, visit.id if visit else None, db)
    return {"saved": saved, "completion_status": status}


# ---------------------------------------------------------------------------
# POST /care/workflow/{booking_id}/documentation/file
# Multipart file upload helper: stores the file locally and returns the URL.
# ---------------------------------------------------------------------------
@router.post("/{booking_id}/documentation/file")
async def upload_documentation_file(
    booking_id: UUID,
    file: UploadFile = File(...),
    field_id: str = Form(...),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    booking, _worker = await _require_worker_for_write(booking_id, current, db)
    # Patch 5A — clinical photo uploads require photo consent.
    try:
        await require_consent(
            db,
            patient_id=booking.patient_id,
            consent_type=ConsentType.photo,
            booking_id=booking.id,
            action="upload clinical photos",
        )
    except ConsentMissingError as ce:
        raise HTTPException(
            status_code=403,
            detail={"code": ce.code, "message": ce.message, "consent_type": ce.consent_type.value},
        ) from None
    safe_name = file.filename or "upload.bin"
    ext = ""
    if "." in safe_name:
        ext = "." + safe_name.rsplit(".", 1)[-1].lower()[:10]
    fname = f"{booking_id}_{field_id}_{_uuid.uuid4().hex}{ext}"
    fpath = os.path.join(DOC_UPLOAD_DIR, fname)
    contents = await file.read()
    with open(fpath, "wb") as fh:
        fh.write(contents)
    public_url = f"{_PUBLIC_URL_PREFIX}/{fname}"
    return {"file_url": public_url, "field_id": field_id, "size_bytes": len(contents)}


# ---------------------------------------------------------------------------
# GET /care/workflow/{booking_id}/completion-status
# ---------------------------------------------------------------------------
@router.get("/{booking_id}/completion-status")
async def get_completion_status(
    booking_id: UUID,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _require_assigned_worker_or_admin(booking_id, current, db)
    try:
        vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
        visit = vres.scalar_one_or_none()
        status = await validate_documentation_completion(booking_id, visit.id if visit else None, db)
    except WorkflowError as e:
        return _workflow_error_response(e)
    return status
