"""Provider contract endpoints — dynamic Stage 1 clickwrap + Stage 2
e-stamp Master Agreement, rendered per provider type (see
app/core/contracts.py), plus the OCR-suggestion-apply endpoint used by the
document upload flow to fill in a worker's name/registration number.
"""
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import contracts as contract_templates
from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, get_worker_profile, require_operations
from app.core.provider_types import LICENSED_PROVIDER_TYPES, PROVIDER_TYPE_LABELS
from app.core.rate_limit import client_ip, enforce_rate_limit
from app.core.security import hash_password, verify_password
from app.models.models import OtpCode, User, WorkerAgreement, WorkerDocument, WorkerPayout, WorkerProfile
from app.services import ocr_service

router = APIRouter(prefix="/contracts", tags=["contracts"])

OTP_PURPOSE_STAGE1 = "contract_stage1"


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class ContractPreviewOut(BaseModel):
    stage: int
    status: str  # "not_applicable" | "pending" | "accepted" | "voided"
    rendered_text: Optional[str] = None
    template_version: str = contract_templates.TEMPLATE_VERSION
    unlocked: bool  # whether this stage is currently actionable
    reason: Optional[str] = None  # why locked, if unlocked=False


class Stage1AcceptRequest(BaseModel):
    otp_code: str


class Stage2AcceptRequest(BaseModel):
    esign_reference_id: str
    esign_document_url: Optional[str] = None
    esign_provider: str = "digio"
    address: Optional[str] = None  # allow a final address confirmation at signing time


class ApplyOcrRequest(BaseModel):
    apply_name: bool = True
    apply_registration_no: bool = False  # off by default — a license number is higher-stakes than a name; require an explicit opt-in


class AdminAgreementRow(BaseModel):
    worker_id: str
    full_name: str
    phone_e164: Optional[str] = None
    provider_type: str
    provider_type_label: str
    stage1_status: str
    stage2_status: str
    stage1_accepted_at: Optional[datetime] = None
    stage2_accepted_at: Optional[datetime] = None
    completed_visits_count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _get_stage(db: AsyncSession, worker_id: UUID, stage: int) -> Optional[WorkerAgreement]:
    res = await db.execute(
        select(WorkerAgreement)
        .where(WorkerAgreement.worker_id == worker_id, WorkerAgreement.stage == stage)
        .order_by(WorkerAgreement.created_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


# ---------------------------------------------------------------------------
# GET current contract state for both stages — this is what the app/website
# renders on the onboarding + "complete your contract" screens.
# ---------------------------------------------------------------------------
@router.get("/me", response_model=list[ContractPreviewOut])
async def get_my_contracts(
    worker: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    stage1 = await _get_stage(db, worker.id, 1)
    stage2 = await _get_stage(db, worker.id, 2)

    full_name = current.user.full_name or ""
    stage1_text = contract_templates.render_stage1(full_name=full_name, worker_type=worker.worker_type)

    out = [
        ContractPreviewOut(
            stage=1,
            status=stage1.status if stage1 else "pending",
            rendered_text=stage1.rendered_text if stage1 else stage1_text,
            unlocked=not (stage1 and stage1.status == "accepted"),
            reason=None,
        )
    ]

    # Stage 2 unlocks only after the worker's first completed booking.
    stage2_unlocked = worker.completed_visits_count >= 1
    stage2_status = stage2.status if stage2 else ("pending" if stage2_unlocked else "not_applicable")
    stage2_text = None
    if stage2:
        stage2_text = stage2.rendered_text
    elif stage2_unlocked:
        stage2_text = contract_templates.render_stage2(
            full_name=full_name,
            address=worker.home_address or "",
            worker_type=worker.worker_type,
            registration_no=worker.registration_no,
            registration_authority=worker.registration_authority,
            execution_date=datetime.now(timezone.utc).date(),
        )

    out.append(
        ContractPreviewOut(
            stage=2,
            status=stage2_status,
            rendered_text=stage2_text,
            unlocked=stage2_unlocked and not (stage2 and stage2.status == "accepted"),
            reason=None if stage2_unlocked else "Complete your first booking to unlock the Master Agreement.",
        )
    )
    return out


# ---------------------------------------------------------------------------
# Stage 1 — clickwrap: checkbox + OTP.
# Reuses the existing OtpCode table/flow (purpose-scoped) rather than
# building a parallel OTP system.
# ---------------------------------------------------------------------------
@router.post("/me/stage1/send-otp")
async def send_stage1_otp(
    request: Request,
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    import secrets

    from app.core.config import settings

    phone = current.user.phone_e164
    if not phone:
        raise HTTPException(status_code=400, detail="No phone number on file for OTP verification.")

    await enforce_rate_limit("otp_send:phone", phone, 3, 10 * 60,
                              message="Too many codes requested. Wait a few minutes and try again.")
    await enforce_rate_limit("otp_send:ip", client_ip(request), 15, 60 * 60)

    code = settings.OTP_DEV_FIXED_CODE if settings.OTP_DEV_MODE else f"{secrets.randbelow(1000000):06d}"
    from datetime import timedelta
    db.add(OtpCode(
        phone_e164=phone,
        code_hash=hash_password(code),
        purpose=OTP_PURPOSE_STAGE1,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_EXPIRE_MINUTES),
    ))
    await db.commit()

    if not settings.OTP_DEV_MODE:
        try:
            from app.integrations.providers import msg91_client
            await msg91_client.send_otp(phone, code)
        except Exception:
            pass

    return {"sent": True, "dev_otp": code if settings.OTP_DEV_MODE else None}


@router.post("/me/stage1/accept", response_model=ContractPreviewOut)
async def accept_stage1(
    payload: Stage1AcceptRequest,
    request: Request,
    worker: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    existing = await _get_stage(db, worker.id, 1)
    if existing and existing.status == "accepted":
        raise HTTPException(status_code=409, detail="Stage 1 agreement already accepted.")

    phone = current.user.phone_e164
    otp_res = await db.execute(
        select(OtpCode)
        .where(OtpCode.phone_e164 == phone, OtpCode.purpose == OTP_PURPOSE_STAGE1, OtpCode.consumed.is_(False))
        .order_by(OtpCode.created_at.desc())
        .limit(1)
    )
    otp = otp_res.scalar_one_or_none()
    if not otp:
        raise HTTPException(status_code=400, detail="No active OTP. Request a new code.")
    if otp.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="OTP expired. Request a new code.")
    if otp.attempts >= 5:
        raise HTTPException(status_code=429, detail="Too many attempts. Request a new code.")
    otp.attempts += 1
    if not verify_password(payload.otp_code, otp.code_hash):
        await db.commit()
        raise HTTPException(status_code=400, detail="Invalid OTP.")
    otp.consumed = True

    rendered = contract_templates.render_stage1(full_name=current.user.full_name or "", worker_type=worker.worker_type)
    agreement = WorkerAgreement(
        worker_id=worker.id,
        stage=1,
        status="accepted",
        provider_type_snapshot=worker.worker_type.value,
        rendered_text=rendered,
        template_version=contract_templates.TEMPLATE_VERSION,
        accepted_at=datetime.now(timezone.utc),
        otp_verified=True,
        ip_address=client_ip(request),
    )
    db.add(agreement)
    await db.commit()

    return ContractPreviewOut(stage=1, status="accepted", rendered_text=rendered, unlocked=False)


# ---------------------------------------------------------------------------
# Stage 2 — Master Agreement, executed after first completed booking via
# Aadhaar eSign on state e-Stamp paper (Digio/Leegality/ASP integration —
# esign_reference_id is whatever that provider returns after the signing
# session completes; this endpoint just records the outcome).
# ---------------------------------------------------------------------------
@router.post("/me/stage2/accept", response_model=ContractPreviewOut)
async def accept_stage2(
    payload: Stage2AcceptRequest,
    request: Request,
    worker: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if worker.completed_visits_count < 1:
        raise HTTPException(status_code=403, detail="Stage 2 unlocks only after your first completed booking.")

    existing = await _get_stage(db, worker.id, 2)
    if existing and existing.status == "accepted":
        raise HTTPException(status_code=409, detail="Stage 2 agreement already executed.")

    if payload.address:
        worker.home_address = payload.address

    rendered = contract_templates.render_stage2(
        full_name=current.user.full_name or "",
        address=worker.home_address or "",
        worker_type=worker.worker_type,
        registration_no=worker.registration_no,
        registration_authority=worker.registration_authority,
        execution_date=datetime.now(timezone.utc).date(),
    )
    agreement = WorkerAgreement(
        worker_id=worker.id,
        stage=2,
        status="accepted",
        provider_type_snapshot=worker.worker_type.value,
        rendered_text=rendered,
        template_version=contract_templates.TEMPLATE_VERSION,
        accepted_at=datetime.now(timezone.utc),
        ip_address=client_ip(request),
        esign_provider=payload.esign_provider,
        esign_reference_id=payload.esign_reference_id,
        esign_document_url=payload.esign_document_url,
    )
    db.add(agreement)

    # Deduct/record the onboarding enablement fee against Booking #1's
    # payout the moment the nurse e-signs the Master Agreement.
    from app.services.payout_service import apply_onboarding_fee

    deducted = await apply_onboarding_fee(db, worker.id)
    if deducted is not None:
        agreement.onboarding_fee_deducted = True

    await db.commit()

    return ContractPreviewOut(stage=2, status="accepted", rendered_text=rendered, unlocked=False)


# ---------------------------------------------------------------------------
# Booking-gate helper — call this from the booking-accept flow (bookings.py)
# before allowing a worker to accept booking #2 onwards.
# ---------------------------------------------------------------------------
async def stage2_gate_passed(db: AsyncSession, worker_id: UUID) -> bool:
    if not (await db.get(WorkerProfile, worker_id)):
        return False
    stage2 = await _get_stage(db, worker_id, 2)
    return bool(stage2 and stage2.status == "accepted")


# ---------------------------------------------------------------------------
# OCR-suggestion apply — worker/admin confirms a suggestion produced when a
# degree/license document was uploaded (see hook in workers.py document
# upload endpoint). Never applied automatically.
# ---------------------------------------------------------------------------
@router.post("/me/documents/{document_id}/apply-ocr")
async def apply_ocr_suggestion(
    document_id: UUID,
    payload: ApplyOcrRequest,
    worker: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    doc = await db.get(WorkerDocument, document_id)
    if not doc or doc.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Document not found.")

    applied = {}
    if payload.apply_name and doc.ocr_extracted_name:
        current.user.full_name = doc.ocr_extracted_name
        applied["full_name"] = doc.ocr_extracted_name
    if payload.apply_registration_no and doc.ocr_extracted_registration_no:
        if worker.worker_type not in LICENSED_PROVIDER_TYPES:
            raise HTTPException(status_code=400, detail="This provider type does not carry a registration number.")
        worker.registration_no = doc.ocr_extracted_registration_no
        applied["registration_no"] = doc.ocr_extracted_registration_no

    if not applied:
        raise HTTPException(status_code=400, detail="No OCR suggestion available on this document to apply.")

    await db.commit()
    return {"applied": applied}


# ---------------------------------------------------------------------------
# Admin/ops view — powers the "Provider Agreements" screen in the web
# dashboard. Read-only: shows who has accepted which stage, so ops can chase
# down workers stuck on Stage 2 (booking #2 locked) without digging into the
# DB directly.
# ---------------------------------------------------------------------------
@router.get("/admin", response_model=list[AdminAgreementRow])
async def admin_list_agreements(
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(WorkerProfile, User).join(User, User.id == WorkerProfile.user_id))
    rows = res.all()

    worker_ids = [w.id for w, _u in rows]
    agreements: dict[UUID, dict[int, WorkerAgreement]] = {}
    if worker_ids:
        ag_res = await db.execute(select(WorkerAgreement).where(WorkerAgreement.worker_id.in_(worker_ids)))
        for ag in ag_res.scalars():
            agreements.setdefault(ag.worker_id, {})
            existing = agreements[ag.worker_id].get(ag.stage)
            if not existing or ag.created_at > existing.created_at:
                agreements[ag.worker_id][ag.stage] = ag

    out = []
    for worker, user in rows:
        stage_map = agreements.get(worker.id, {})
        stage1 = stage_map.get(1)
        stage2 = stage_map.get(2)
        stage2_status = (
            stage2.status if stage2 else ("pending" if worker.completed_visits_count >= 1 else "not_applicable")
        )
        out.append(
            AdminAgreementRow(
                worker_id=str(worker.id),
                full_name=user.full_name or "—",
                phone_e164=user.phone_e164,
                provider_type=worker.worker_type.value,
                provider_type_label=PROVIDER_TYPE_LABELS.get(worker.worker_type, worker.worker_type.value),
                stage1_status=stage1.status if stage1 else "pending",
                stage2_status=stage2_status,
                stage1_accepted_at=stage1.accepted_at if stage1 else None,
                stage2_accepted_at=stage2.accepted_at if stage2 else None,
                completed_visits_count=worker.completed_visits_count,
            )
        )
    return out


@router.get("/admin/{worker_id}", response_model=list[ContractPreviewOut])
async def admin_get_worker_agreements(
    worker_id: UUID,
    current: CurrentUser = Depends(require_operations),
    db: AsyncSession = Depends(get_db),
):
    worker = await db.get(WorkerProfile, worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found.")
    stage1 = await _get_stage(db, worker_id, 1)
    stage2 = await _get_stage(db, worker_id, 2)

    out = [
        ContractPreviewOut(
            stage=1,
            status=stage1.status if stage1 else "pending",
            rendered_text=stage1.rendered_text if stage1 else None,
            unlocked=False,
        )
    ]
    stage2_unlocked = worker.completed_visits_count >= 1
    out.append(
        ContractPreviewOut(
            stage=2,
            status=stage2.status if stage2 else ("pending" if stage2_unlocked else "not_applicable"),
            rendered_text=stage2.rendered_text if stage2 else None,
            unlocked=False,
            reason=None if stage2_unlocked else "Worker has not completed a first booking yet.",
        )
    )
    return out
