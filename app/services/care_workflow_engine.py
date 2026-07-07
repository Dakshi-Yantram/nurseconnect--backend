"""Care workflow engine — Patch 4.

Dynamic, DB-driven resolution of checklist + documentation templates linked to
either the booking's care package or its primary service. Validation logic is
template-shape aware so application code never hardcodes question IDs, field
labels, wound-photo rules, vitals requirements, etc.

Schema is offline-sync ready: every persisted response carries
``template_version`` plus ``question_text_snapshot`` / ``field_label_snapshot``
so editing a template later does not silently mutate historical visits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ServiceRiskLevel
from app.models.models import (
    Booking,
    CarePackage,
    ChecklistTemplate,
    DocumentationTemplate,
    Patient,
    ServiceCatalogue,
    VisitChecklistResponse,
    VisitDocumentationItem,
    VisitRecord,
)

# Question/field types supported by the dynamic renderer + validator.
SUPPORTED_QUESTION_TYPES = {
    "text",
    "textarea",
    "number",
    "boolean",
    "single_select",
    "multi_select",
    "photo",
    "vitals_entry",
    "medication_entry",
    "consent_confirmation",
}

# Risk levels at which a missing template must hard-block the workflow.
# Low-risk services are allowed to fall through to a minimal safe-default
# (see resolve_workflow_for_booking()).
_HIGH_RISK = {ServiceRiskLevel.MEDIUM, ServiceRiskLevel.HIGH, ServiceRiskLevel.CRITICAL}


class WorkflowError(Exception):
    """Domain error with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, http_status: int = 422, missing_items: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.missing_items = missing_items or []


@dataclass
class ResolvedWorkflow:
    booking: Booking
    service: Optional[ServiceCatalogue]
    package: Optional[CarePackage]
    checklist_template: Optional[ChecklistTemplate]
    documentation_template: Optional[DocumentationTemplate]
    risk_level: ServiceRiskLevel
    source: str  # "package" | "service" | "fallback"
    family_summary_template: Optional[str]


# ---------------------------------------------------------------------------
# Safe-default fallback shapes (intentionally minimal). Used ONLY for LOW-risk
# services lacking explicit templates. Persisted with a synthetic template id
# is impossible (no DB row), so the engine surfaces these inline and never
# accepts checklist/documentation submissions against them — checkout is
# allowed because nothing is required.
# ---------------------------------------------------------------------------
_FALLBACK_FAMILY_SUMMARY = (
    "Visit completed for {{patient_name}}. Care delivered as planned. "
    "Please contact us if you have any questions."
)


def _effective_risk(service: Optional[ServiceCatalogue], package: Optional[CarePackage]) -> ServiceRiskLevel:
    if package and package.risk_level:
        return package.risk_level
    if service and service.risk_level:
        return service.risk_level
    return ServiceRiskLevel.LOW


async def resolve_workflow_for_booking(booking_id: UUID, db: AsyncSession) -> ResolvedWorkflow:
    """Resolve the checklist + documentation templates for a booking.

    Resolution order (highest priority first):
      1. Booking has package_id → use the package's templates if present.
      2. Otherwise → use the primary service's templates.
      3. If neither template exists:
         - LOW-risk service → safe fallback (engine returns Nones).
         - MEDIUM/HIGH/CRITICAL-risk → raise CLINICAL_TEMPLATE_MISSING.
    """
    bres = await db.execute(select(Booking).where(Booking.id == booking_id))
    booking = bres.scalar_one_or_none()
    if not booking:
        raise WorkflowError("BOOKING_NOT_FOUND", "Booking not found", http_status=404)

    package: Optional[CarePackage] = None
    service: Optional[ServiceCatalogue] = None
    if booking.package_id:
        pkg_res = await db.execute(select(CarePackage).where(CarePackage.id == booking.package_id))
        package = pkg_res.scalar_one_or_none()
        # Fall back to the package's primary service for risk + family summary
        if package and package.primary_service_id:
            sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == package.primary_service_id))
            service = sres.scalar_one_or_none()
    if booking.service_id and not service:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == booking.service_id))
        service = sres.scalar_one_or_none()

    checklist_tpl: Optional[ChecklistTemplate] = None
    doc_tpl: Optional[DocumentationTemplate] = None
    source = "fallback"

    # Try the package first
    if package:
        if package.checklist_template_id:
            cres = await db.execute(select(ChecklistTemplate).where(ChecklistTemplate.id == package.checklist_template_id))
            checklist_tpl = cres.scalar_one_or_none()
        if package.documentation_template_id:
            dres = await db.execute(select(DocumentationTemplate).where(DocumentationTemplate.id == package.documentation_template_id))
            doc_tpl = dres.scalar_one_or_none()
        if checklist_tpl or doc_tpl:
            source = "package"

    # Then the service
    if not checklist_tpl and service and service.checklist_template_id:
        cres = await db.execute(select(ChecklistTemplate).where(ChecklistTemplate.id == service.checklist_template_id))
        checklist_tpl = cres.scalar_one_or_none()
        if checklist_tpl:
            source = "service"
    if not doc_tpl and service and service.documentation_template_id:
        dres = await db.execute(select(DocumentationTemplate).where(DocumentationTemplate.id == service.documentation_template_id))
        doc_tpl = dres.scalar_one_or_none()
        if doc_tpl and source == "fallback":
            source = "service"

    risk = _effective_risk(service, package)

    # Enforce template-required for high-risk clinical services
    if checklist_tpl is None and doc_tpl is None and risk in _HIGH_RISK:
        raise WorkflowError(
            "CLINICAL_TEMPLATE_MISSING",
            f"No checklist/documentation template is configured for this {risk.value.lower()}-risk service.",
        )

    family_summary_template = None
    if package and package.family_summary_template:
        family_summary_template = package.family_summary_template
    elif service and service.family_summary_template:
        family_summary_template = service.family_summary_template

    return ResolvedWorkflow(
        booking=booking,
        service=service,
        package=package,
        checklist_template=checklist_tpl,
        documentation_template=doc_tpl,
        risk_level=risk,
        source=source,
        family_summary_template=family_summary_template,
    )


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
def _coerce_answer(qtype: str, raw: Any) -> Any:
    """Lightweight type coercion + shape validation per question type.

    Raises ValueError with a stable message on shape mismatch so the API layer
    can surface a 400 with details. We never store the raw payload blindly.
    """
    if raw is None:
        return None
    if qtype in ("text", "textarea"):
        if not isinstance(raw, str):
            raise ValueError("expected string")
        return raw.strip()
    if qtype == "number":
        if isinstance(raw, bool):
            raise ValueError("expected number")
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:  # noqa: BLE001
            raise ValueError("expected numeric value") from exc
    if qtype == "boolean":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return raw.lower() in {"true", "yes", "1"}
        raise ValueError("expected boolean")
    if qtype == "single_select":
        if not isinstance(raw, (str, int)):
            raise ValueError("expected single option value")
        return raw
    if qtype == "multi_select":
        if not isinstance(raw, list):
            raise ValueError("expected list of option values")
        return list(raw)
    if qtype == "photo":
        # The frontend uploads file separately and submits {"file_url": "..."}.
        # Accept either a URL string or an object containing file_url.
        if isinstance(raw, str):
            return {"file_url": raw}
        if isinstance(raw, dict) and ("file_url" in raw or "url" in raw):
            return {"file_url": raw.get("file_url") or raw.get("url"), **{k: v for k, v in raw.items() if k not in ("file_url", "url")}}
        raise ValueError("expected file_url or {file_url}")
    if qtype == "vitals_entry":
        if not isinstance(raw, dict):
            raise ValueError("expected vitals object")
        return raw
    if qtype == "medication_entry":
        if not isinstance(raw, dict):
            raise ValueError("expected medication object")
        return raw
    if qtype == "consent_confirmation":
        if not isinstance(raw, dict) or "consented" not in raw:
            raise ValueError("expected {consented: bool, ...}")
        return {"consented": bool(raw.get("consented")), **{k: v for k, v in raw.items() if k != "consented"}}
    raise ValueError(f"unsupported question type: {qtype}")


def _is_question_complete(qtype: str, answer: Any) -> bool:
    if answer is None:
        return False
    if qtype in ("text", "textarea"):
        return isinstance(answer, str) and bool(answer.strip())
    if qtype == "number":
        return isinstance(answer, (int, float))
    if qtype == "boolean":
        # Boolean answers are considered complete once a value is recorded,
        # regardless of whether the answer is True or False. A False answer is
        # still a meaningful response and should not be treated as missing.
        return isinstance(answer, bool)
    if qtype == "single_select":
        return answer not in (None, "")
    if qtype == "multi_select":
        return isinstance(answer, list) and len(answer) > 0
    if qtype == "photo":
        return isinstance(answer, dict) and bool(answer.get("file_url"))
    if qtype in ("vitals_entry", "medication_entry"):
        return isinstance(answer, dict) and len(answer) > 0
    if qtype == "consent_confirmation":
        return isinstance(answer, dict) and bool(answer.get("consented"))
    return False


def validate_checklist_response(
    payload: Dict[str, Any],
    template: ChecklistTemplate,
) -> Tuple[Dict[str, Any], List[str]]:
    """Validate one checklist response payload against the template.

    Expected payload shape::

        {"question_id": "wound_photo_captured", "answer": {...}}

    Returns ``(normalised_payload, validation_warnings)``. Raises
    ``WorkflowError`` (code=INVALID_CHECKLIST_PAYLOAD) on structural problems.
    """
    qid = payload.get("question_id")
    if not qid:
        raise WorkflowError("INVALID_CHECKLIST_PAYLOAD", "Missing question_id")
    questions = template.questions or []
    question = next((q for q in questions if q.get("id") == qid), None)
    if not question:
        raise WorkflowError(
            "UNKNOWN_QUESTION_ID",
            f"Question '{qid}' is not part of template '{template.code}' v{template.version}",
        )
    qtype = question.get("type", "text")
    if qtype not in SUPPORTED_QUESTION_TYPES:
        raise WorkflowError(
            "UNSUPPORTED_QUESTION_TYPE",
            f"Question type '{qtype}' is not supported by the engine",
        )
    try:
        normalised = _coerce_answer(qtype, payload.get("answer"))
    except ValueError as ve:
        raise WorkflowError("INVALID_ANSWER_SHAPE", f"Question '{qid}': {ve}") from ve

    is_required = bool(question.get("required", False))
    completed = _is_question_complete(qtype, normalised)
    return {
        "question_id": qid,
        "question_text_snapshot": question.get("text", qid),
        "phase": question.get("phase", template.phase.value if hasattr(template.phase, "value") else (template.phase or "all")),
        "answer_json": {"value": normalised} if normalised is not None else None,
        "is_required": is_required,
        "is_completed": completed,
    }, []


def validate_documentation_payload(
    payload: Dict[str, Any],
    template: DocumentationTemplate,
) -> Dict[str, Any]:
    fid = payload.get("field_id")
    if not fid:
        raise WorkflowError("INVALID_DOCUMENTATION_PAYLOAD", "Missing field_id")
    fields = template.mandatory_fields or []
    field = next((f for f in fields if f.get("field_id") == fid), None)
    if not field:
        raise WorkflowError(
            "UNKNOWN_FIELD_ID",
            f"Field '{fid}' is not part of documentation template '{template.template_code}' v{template.version}",
        )
    ftype = field.get("type", "text")
    if ftype not in SUPPORTED_QUESTION_TYPES:
        raise WorkflowError("UNSUPPORTED_FIELD_TYPE", f"Field type '{ftype}' not supported")

    raw_value = payload.get("value")
    file_url = payload.get("file_url")
    if ftype == "photo" and file_url and raw_value is None:
        raw_value = {"file_url": file_url}
    try:
        normalised = _coerce_answer(ftype, raw_value)
    except ValueError as ve:
        raise WorkflowError("INVALID_FIELD_SHAPE", f"Field '{fid}': {ve}") from ve

    is_required = bool(field.get("required", False))
    blocks_checkout = bool(field.get("blocks_checkout", False))
    completed = _is_question_complete(ftype, normalised) or bool(file_url)
    return {
        "field_id": fid,
        "field_label_snapshot": field.get("label", fid),
        "field_type": ftype,
        "value_json": {"value": normalised} if normalised is not None else None,
        "file_url": file_url,
        "is_required": is_required,
        "blocks_checkout": blocks_checkout,
        "is_completed": completed,
    }


# ---------------------------------------------------------------------------
# Completion status
# ---------------------------------------------------------------------------
async def _existing_checklist_rows(
    db: AsyncSession,
    booking_id: UUID,
    template_id: UUID,
    template_version: int,
) -> Dict[str, VisitChecklistResponse]:
    res = await db.execute(
        select(VisitChecklistResponse).where(
            VisitChecklistResponse.booking_id == booking_id,
            VisitChecklistResponse.checklist_template_id == template_id,
            VisitChecklistResponse.template_version == template_version,
        )
    )
    return {row.question_id: row for row in res.scalars().all()}


async def _existing_doc_rows(
    db: AsyncSession,
    booking_id: UUID,
    template_id: UUID,
    template_version: int,
) -> Dict[str, VisitDocumentationItem]:
    res = await db.execute(
        select(VisitDocumentationItem).where(
            VisitDocumentationItem.booking_id == booking_id,
            VisitDocumentationItem.documentation_template_id == template_id,
            VisitDocumentationItem.template_version == template_version,
        )
    )
    return {row.field_id: row for row in res.scalars().all()}


async def validate_documentation_completion(
    booking_id: UUID,
    visit_record_id: Optional[UUID],
    db: AsyncSession,
) -> Dict[str, Any]:
    """Compute checkout-readiness for a booking.

    Returns::

        {
            "can_checkout": bool,
            "missing_items": [{type, id, label, kind, blocks_checkout}],
            "blocking_items": [...subset where blocks_checkout=True...],
            "checklist_template": {...optional summary...},
            "documentation_template": {...optional summary...},
            "workflow_source": "package" | "service" | "fallback"
        }
    """
    wf = await resolve_workflow_for_booking(booking_id, db)
    missing: List[Dict[str, Any]] = []
    blocking: List[Dict[str, Any]] = []

    if wf.checklist_template:
        existing = await _existing_checklist_rows(
            db, booking_id, wf.checklist_template.id, wf.checklist_template.version
        )
        for q in wf.checklist_template.questions or []:
            if not q.get("required"):
                continue
            qid = q.get("id")
            row = existing.get(qid)
            if not row or not row.is_completed:
                item = {
                    "type": "checklist",
                    "id": qid,
                    "label": q.get("text") or qid,
                    "kind": q.get("type", "text"),
                    # Per spec, required checklist items always block checkout.
                    "blocks_checkout": True,
                }
                missing.append(item)
                blocking.append(item)

    if wf.documentation_template:
        existing_d = await _existing_doc_rows(
            db, booking_id, wf.documentation_template.id, wf.documentation_template.version
        )
        for f in wf.documentation_template.mandatory_fields or []:
            if not f.get("required"):
                continue
            fid = f.get("field_id")
            row = existing_d.get(fid)
            if not row or not row.is_completed:
                item = {
                    "type": "documentation",
                    "id": fid,
                    "label": f.get("label") or fid,
                    "kind": f.get("type", "text"),
                    "blocks_checkout": bool(f.get("blocks_checkout", True)),
                }
                missing.append(item)
                if item["blocks_checkout"]:
                    blocking.append(item)

    can_checkout = len(blocking) == 0
    return {
        "can_checkout": can_checkout,
        "missing_items": missing,
        "blocking_items": blocking,
        "workflow_source": wf.source,
        "risk_level": wf.risk_level.value,
        "checklist_template": _template_summary(wf.checklist_template) if wf.checklist_template else None,
        "documentation_template": _doc_template_summary(wf.documentation_template) if wf.documentation_template else None,
    }


def _template_summary(t: ChecklistTemplate) -> Dict[str, Any]:
    return {
        "id": str(t.id),
        "code": t.code,
        "name": t.name,
        "version": t.version,
        "phase": t.phase.value if hasattr(t.phase, "value") else t.phase,
        "questions": t.questions or [],
    }


def _doc_template_summary(t: DocumentationTemplate) -> Dict[str, Any]:
    return {
        "id": str(t.id),
        "code": t.template_code,
        "name": t.name,
        "version": t.version,
        "mandatory_fields": t.mandatory_fields or [],
        "wound_image_mandatory": bool(t.wound_image_mandatory),
        "photo_consent_required": bool(t.photo_consent_required),
    }


# ---------------------------------------------------------------------------
# Family summary rendering
# ---------------------------------------------------------------------------
async def render_family_summary(
    booking_id: UUID,
    visit_record_id: Optional[UUID],
    db: AsyncSession,
) -> str:
    wf = await resolve_workflow_for_booking(booking_id, db)
    template_str = wf.family_summary_template or _FALLBACK_FAMILY_SUMMARY

    # Build a context dict from booking + patient + visit
    ctx: Dict[str, str] = {}
    pres = await db.execute(select(Patient).where(Patient.id == wf.booking.patient_id))
    patient = pres.scalar_one_or_none()
    ctx["patient_name"] = patient.full_name if patient else "the patient"

    if visit_record_id:
        vres = await db.execute(select(VisitRecord).where(VisitRecord.id == visit_record_id))
        visit = vres.scalar_one_or_none()
    else:
        vres = await db.execute(select(VisitRecord).where(VisitRecord.booking_id == booking_id))
        visit = vres.scalar_one_or_none()

    # Find latest vitals row for this visit, if any. Imported lazily to avoid
    # circulars in case downstream tests stub the engine.
    if visit is not None:
        from app.models.models import VitalSignReading  # local import
        v_q = await db.execute(
            select(VitalSignReading)
            .where(VitalSignReading.visit_record_id == visit.id)
            .order_by(VitalSignReading.recorded_at.desc())
            .limit(1)
        )
        latest = v_q.scalar_one_or_none()
        if latest:
            ctx.update(
                {
                    "bp_systolic": str(latest.bp_systolic) if latest.bp_systolic is not None else "—",
                    "bp_diastolic": str(latest.bp_diastolic) if latest.bp_diastolic is not None else "—",
                    "pulse": str(latest.pulse) if latest.pulse is not None else "—",
                    "spo2": str(latest.spo2) if latest.spo2 is not None else "—",
                }
            )
    ctx.setdefault("bp_systolic", "—")
    ctx.setdefault("bp_diastolic", "—")
    ctx.setdefault("pulse", "—")
    ctx.setdefault("spo2", "—")
    ctx.setdefault("next_visit_date", "TBD")

    rendered = template_str
    for k, v in ctx.items():
        rendered = rendered.replace("{{" + k + "}}", v)
    return rendered


# ---------------------------------------------------------------------------
# Persistence helpers used by the API layer
# ---------------------------------------------------------------------------
async def upsert_checklist_response(
    db: AsyncSession,
    *,
    booking: Booking,
    visit: Optional[VisitRecord],
    worker_id: UUID,
    template: ChecklistTemplate,
    validated: Dict[str, Any],
    is_offline_submitted: bool = False,
) -> VisitChecklistResponse:
    """Insert or update a single checklist response row."""
    res = await db.execute(
        select(VisitChecklistResponse).where(
            VisitChecklistResponse.booking_id == booking.id,
            VisitChecklistResponse.checklist_template_id == template.id,
            VisitChecklistResponse.template_version == template.version,
            VisitChecklistResponse.question_id == validated["question_id"],
        )
    )
    row = res.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = VisitChecklistResponse(
            booking_id=booking.id,
            visit_record_id=visit.id if visit else None,
            worker_id=worker_id,
            patient_id=booking.patient_id,
            checklist_template_id=template.id,
            template_version=template.version,
            phase=validated["phase"],
            question_id=validated["question_id"],
            question_text_snapshot=validated["question_text_snapshot"],
            answer_json=validated["answer_json"],
            is_required=validated["is_required"],
            is_completed=validated["is_completed"],
            answered_at=now if validated["is_completed"] else None,
            is_offline_submitted=is_offline_submitted,
            synced_at=None if is_offline_submitted else now,
        )
        db.add(row)
    else:
        # Don't overwrite historical snapshot — only update mutable fields.
        row.answer_json = validated["answer_json"]
        row.is_completed = validated["is_completed"]
        row.is_required = validated["is_required"]
        row.answered_at = now if validated["is_completed"] else row.answered_at
        row.is_offline_submitted = is_offline_submitted or row.is_offline_submitted
        row.synced_at = row.synced_at if is_offline_submitted else now
        if visit and row.visit_record_id is None:
            row.visit_record_id = visit.id
    return row


async def upsert_documentation_item(
    db: AsyncSession,
    *,
    booking: Booking,
    visit: Optional[VisitRecord],
    worker_id: UUID,
    template: DocumentationTemplate,
    validated: Dict[str, Any],
    is_offline_submitted: bool = False,
) -> VisitDocumentationItem:
    res = await db.execute(
        select(VisitDocumentationItem).where(
            VisitDocumentationItem.booking_id == booking.id,
            VisitDocumentationItem.documentation_template_id == template.id,
            VisitDocumentationItem.template_version == template.version,
            VisitDocumentationItem.field_id == validated["field_id"],
        )
    )
    row = res.scalar_one_or_none()
    now = datetime.now(timezone.utc)
    if row is None:
        row = VisitDocumentationItem(
            booking_id=booking.id,
            visit_record_id=visit.id if visit else None,
            worker_id=worker_id,
            patient_id=booking.patient_id,
            documentation_template_id=template.id,
            template_version=template.version,
            field_id=validated["field_id"],
            field_label_snapshot=validated["field_label_snapshot"],
            field_type=validated["field_type"],
            value_json=validated["value_json"],
            file_url=validated["file_url"],
            is_required=validated["is_required"],
            blocks_checkout=validated["blocks_checkout"],
            is_completed=validated["is_completed"],
            completed_at=now if validated["is_completed"] else None,
            is_offline_submitted=is_offline_submitted,
            synced_at=None if is_offline_submitted else now,
        )
        db.add(row)
    else:
        row.value_json = validated["value_json"]
        row.file_url = validated["file_url"] or row.file_url
        row.is_completed = validated["is_completed"]
        row.is_required = validated["is_required"]
        row.blocks_checkout = validated["blocks_checkout"]
        row.completed_at = now if validated["is_completed"] else row.completed_at
        row.is_offline_submitted = is_offline_submitted or row.is_offline_submitted
        row.synced_at = row.synced_at if is_offline_submitted else now
        if visit and row.visit_record_id is None:
            row.visit_record_id = visit.id
    return row


async def list_existing_responses(
    db: AsyncSession, booking_id: UUID
) -> Tuple[List[VisitChecklistResponse], List[VisitDocumentationItem]]:
    cres = await db.execute(
        select(VisitChecklistResponse).where(VisitChecklistResponse.booking_id == booking_id)
    )
    dres = await db.execute(
        select(VisitDocumentationItem).where(VisitDocumentationItem.booking_id == booking_id)
    )
    return list(cres.scalars().all()), list(dres.scalars().all())
