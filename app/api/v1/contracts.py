"""Provider contract endpoints — dynamic Stage 1 clickwrap + Stage 2
e-stamp Master Agreement, rendered per provider type (see
app/core/contracts.py), plus the OCR-suggestion-apply endpoint used by the
document upload flow to fill in a worker's name/registration number.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import contracts as contract_templates
from app.core.config import settings
from app.core.database import get_db
from app.core.deps import CurrentUser, get_current_user, get_worker_profile, require_operations
from app.core.provider_types import LICENSED_PROVIDER_TYPES, PROVIDER_TYPE_LABELS
from app.core.rate_limit import client_ip, enforce_rate_limit
from app.core.security import hash_password, verify_password
from app.models.models import (
    OtpCode,
    User,
    WorkerAgreement,
    WorkerDocument,
    WorkerEsignSession,
    WorkerPayout,
    WorkerProfile,
)
from app.services import ocr_service

logger = logging.getLogger(__name__)

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
    # No esign fields here anymore — see the ROOT-CAUSE NOTE above
    # accept_stage2(). Whether the signature is genuine is decided entirely
    # server-side, from our own WorkerEsignSession record, never from
    # anything the client sends in this body.
    address: Optional[str] = None  # allow a final address confirmation at signing time


class EsignInitiateOut(BaseModel):
    session_id: str
    status: str
    sign_url: Optional[str] = None
    expires_at: Optional[datetime] = None


class EsignStatusOut(BaseModel):
    session_id: str
    status: str  # created | sent | signed | failed
    sign_url: Optional[str] = None
    failure_reason: Optional[str] = None


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
    onboarding_fee_collected: float = 0.0
    onboarding_fee_target: float = 200.0


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

    # Legacy rows created before this column existed can be NULL in the DB
    # even though the ORM default is 0 (that default only applies to new
    # inserts made through SQLAlchemy) — guard so a NULL here can't turn a
    # routine GET into a 500 (`None >= 1` raises TypeError).
    completed_visits_count = worker.completed_visits_count or 0

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
    stage2_unlocked = completed_visits_count >= 1
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
# Aadhaar eSign on state e-Stamp paper, via Digio (app/integrations/
# providers.py::DigioClient).
#
# ROOT-CAUSE NOTE: this used to be a single endpoint that trusted whatever
# `esign_reference_id`/`esign_document_url` the client body claimed —
# nothing was ever actually verified with an eSign provider, so any worker
# could POST an arbitrary string (the mobile app literally sent the literal
# string "PENDING_ASP_INTEGRATION") and have Stage 2 marked "accepted" and
# executed without ever signing anything. That's a legal-enforceability
# problem, not just a data-quality one — the whole point of Stage 2 is a
# provider having genuinely executed a Master Agreement.
#
# The fix splits this into three steps, none of which trust the client's
# say-so about the outcome:
#   1. POST .../esign/initiate  — we render the agreement, upload it to
#      Digio ourselves, and hand back Digio's own hosted sign_url.
#   2. Digio's webhook (or, as a fallback, a status poll) tells US whether
#      it was actually signed — this is the only thing that can ever move
#      a WorkerEsignSession to "signed".
#   3. POST .../stage2/accept finalizes — it looks up the caller's OWN
#      session and requires session.status == "signed" before creating the
#      executed WorkerAgreement row. The request body carries no esign
#      fields anymore; there is nothing left in it that could forge a
#      signature.
# ---------------------------------------------------------------------------
async def _latest_esign_session(db: AsyncSession, worker_id: UUID, stage: int) -> Optional[WorkerEsignSession]:
    res = await db.execute(
        select(WorkerEsignSession)
        .where(WorkerEsignSession.worker_id == worker_id, WorkerEsignSession.stage == stage)
        .order_by(WorkerEsignSession.created_at.desc())
        .limit(1)
    )
    return res.scalar_one_or_none()


def _session_expired(session: WorkerEsignSession) -> bool:
    return bool(
        session.expires_at
        and session.status in ("created", "sent")
        and datetime.now(timezone.utc) >= session.expires_at
    )


@router.post("/me/stage2/esign/initiate", response_model=EsignInitiateOut)
async def initiate_stage2_esign(
    worker: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Render the Stage 2 agreement and send it to Digio for signing.

    Returns Digio's own sign_url — the mobile app opens this in an in-app
    browser/WebView. Nothing here marks anything signed; that only happens
    via the webhook or a status poll, both of which check with Digio
    directly (see get_document_status / verify_webhook_signature).
    """
    if (worker.completed_visits_count or 0) < 1:
        raise HTTPException(status_code=403, detail="Stage 2 unlocks only after your first completed booking.")

    existing_agreement = await _get_stage(db, worker.id, 2)
    if existing_agreement and existing_agreement.status == "accepted":
        raise HTTPException(status_code=409, detail="Stage 2 agreement already executed.")

    # Reuse an in-flight, not-yet-expired session rather than spamming Digio
    # with a fresh signing request every time the screen is reopened.
    current_session = await _latest_esign_session(db, worker.id, 2)
    if current_session and current_session.status in ("created", "sent") and not _session_expired(current_session):
        return EsignInitiateOut(
            session_id=str(current_session.id),
            status=current_session.status,
            sign_url=current_session.sign_url,
            expires_at=current_session.expires_at,
        )

    rendered = contract_templates.render_stage2(
        full_name=current.user.full_name or "",
        address=worker.home_address or "",
        worker_type=worker.worker_type,
        registration_no=worker.registration_no,
        registration_authority=worker.registration_authority,
        execution_date=datetime.now(timezone.utc).date(),
    )

    from app.integrations.providers import ExternalProviderError, digio_client
    from app.services.agreement_pdf import render_agreement_pdf

    pdf_bytes = render_agreement_pdf(
        title="Master Independent Contractor Agreement",
        body_text=rendered,
    )

    signer_identifier = current.user.email or current.user.phone_e164
    try:
        digio_resp = await digio_client.create_esign_request(
            pdf_bytes=pdf_bytes,
            signer_name=current.user.full_name or "Provider",
            signer_identifier=signer_identifier,
            reason="Master Independent Contractor Agreement",
        )
    except ExternalProviderError as exc:
        raise HTTPException(status_code=502, detail=f"Could not start e-Sign: {exc}") from None

    sign_url = None
    parties = digio_resp.get("signing_parties") or []
    if parties:
        sign_url = parties[0].get("sign_url")

    session = WorkerEsignSession(
        worker_id=worker.id,
        stage=2,
        provider="digio",
        status="sent" if sign_url else "created",
        digio_document_id=digio_resp.get("id"),
        sign_url=sign_url,
        rendered_text=rendered,
        template_version=contract_templates.TEMPLATE_VERSION,
        last_provider_payload=digio_resp,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=settings.DIGIO_SESSION_EXPIRE_MINUTES),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    return EsignInitiateOut(
        session_id=str(session.id),
        status=session.status,
        sign_url=session.sign_url,
        expires_at=session.expires_at,
    )


@router.get("/me/stage2/esign/status", response_model=EsignStatusOut)
async def stage2_esign_status(
    worker: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Current status of the worker's latest e-Sign session.

    The mobile app polls this after returning from the Digio WebView (in
    case its own redirect/deep-link is missed) and while waiting for the
    webhook. If Digio hasn't told us anything new via webhook yet, this
    actively polls Digio's own status API rather than just returning
    whatever we last stored — a missed webhook must not leave a worker
    stuck looking "pending" forever after they've actually signed.
    """
    session = await _latest_esign_session(db, worker.id, 2)
    if not session:
        raise HTTPException(status_code=404, detail="No e-Sign session found. Start one first.")

    if session.status in ("created", "sent") and session.digio_document_id:
        from app.integrations.providers import ExternalProviderError, digio_client

        try:
            status_payload = await digio_client.get_document_status(session.digio_document_id)
            await _apply_esign_status(db, session, status_payload)
            await db.commit()
        except ExternalProviderError:
            # Digio being briefly unreachable shouldn't be reported to the
            # worker as their signature having failed — just report our
            # last known state and let the next poll try again.
            pass

    if _session_expired(session) and session.status in ("created", "sent"):
        session.status = "failed"
        session.failure_reason = "Signing session expired before completion."
        await db.commit()

    return EsignStatusOut(
        session_id=str(session.id),
        status=session.status,
        sign_url=session.sign_url,
        failure_reason=session.failure_reason,
    )


async def _apply_esign_status(db: AsyncSession, session: WorkerEsignSession, status_payload: dict) -> None:
    """Update `session` from a Digio status payload (webhook or poll).

    The only place in this file that is allowed to move a session to
    "signed" — everything downstream (accept_stage2) trusts this, and this
    trusts only what Digio itself reported.
    """
    from app.integrations.providers import digio_client

    session.last_provider_payload = status_payload
    if digio_client.is_signed(status_payload):
        if session.status != "signed":
            session.status = "signed"
            session.signed_at = datetime.now(timezone.utc)
    elif digio_client.is_failed(status_payload):
        session.status = "failed"
        session.failure_reason = str(status_payload.get("agreement_status") or "Signing failed")


@router.post("/webhook/digio")
async def digio_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Digio's signing-completion callback.

    Configure the exact header name your Digio account sends the signature
    in against settings.DIGIO_WEBHOOK_SECRET's dashboard counterpart — the
    verification logic (HMAC-SHA256 over the raw body) is correct regardless
    of which header carries it; only the header name below may need
    adjusting per your Digio integration.
    """
    from app.integrations.providers import digio_client

    body = await request.body()
    signature = request.headers.get("x-digio-signature") or request.headers.get("x-webhook-signature")
    if not digio_client.verify_webhook_signature(body, signature):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    payload = await request.json()
    document_id = payload.get("id") or payload.get("document_id")
    if not document_id:
        return {"received": True, "matched": False}

    res = await db.execute(
        select(WorkerEsignSession).where(WorkerEsignSession.digio_document_id == document_id)
    )
    session = res.scalar_one_or_none()
    if not session:
        logger.warning("digio webhook for unknown document_id=%s", document_id)
        return {"received": True, "matched": False}

    await _apply_esign_status(db, session, payload)
    await db.commit()
    return {"received": True, "matched": True, "status": session.status}


@router.post("/me/stage2/esign/mock-complete", response_model=EsignStatusOut)
async def mock_complete_stage2_esign(
    worker: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Simulates the Digio WebView finishing successfully — MOCK MODE ONLY.

    This is what the mobile app's built-in mock signing screen calls when
    MOCK_EXTERNAL_PROVIDERS is on and there's no real Digio sandbox to
    redirect to. It is hard-gated below: with mock mode off this 403s
    unconditionally, so it can never become a way to skip signing in
    production regardless of what a compromised or modified client sends.
    """
    if not settings.MOCK_EXTERNAL_PROVIDERS:
        raise HTTPException(status_code=403, detail="Not available outside mock mode.")

    session = await _latest_esign_session(db, worker.id, 2)
    if not session:
        raise HTTPException(status_code=404, detail="No e-Sign session found. Start one first.")

    session.status = "signed"
    session.signed_at = datetime.now(timezone.utc)
    session.last_provider_payload = {"id": session.digio_document_id, "agreement_status": "completed", "mock": True}
    await db.commit()

    return EsignStatusOut(session_id=str(session.id), status=session.status, sign_url=session.sign_url)


@router.post("/me/stage2/accept", response_model=ContractPreviewOut)
async def accept_stage2(
    payload: Stage2AcceptRequest,
    request: Request,
    worker: WorkerProfile = Depends(get_worker_profile),
    current: CurrentUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if (worker.completed_visits_count or 0) < 1:
        raise HTTPException(status_code=403, detail="Stage 2 unlocks only after your first completed booking.")

    existing = await _get_stage(db, worker.id, 2)
    if existing and existing.status == "accepted":
        raise HTTPException(status_code=409, detail="Stage 2 agreement already executed.")

    session = await _latest_esign_session(db, worker.id, 2)
    if not session:
        raise HTTPException(
            status_code=409,
            detail={"code": "ESIGN_NOT_STARTED", "message": "Start e-Sign before accepting Stage 2."},
        )

    # Re-check with Digio one last time in case the webhook hasn't landed
    # yet — never finalize on a stale in-memory status.
    if session.status in ("created", "sent") and session.digio_document_id:
        from app.integrations.providers import ExternalProviderError, digio_client

        try:
            status_payload = await digio_client.get_document_status(session.digio_document_id)
            await _apply_esign_status(db, session, status_payload)
        except ExternalProviderError:
            pass

    if session.status != "signed":
        raise HTTPException(
            status_code=409,
            detail={
                "code": "ESIGN_NOT_SIGNED",
                "message": "e-Sign has not been completed yet.",
                "status": session.status,
            },
        )

    if payload.address:
        worker.home_address = payload.address

    # Sign off on the EXACT text the session was created for — not a fresh
    # render — so a template edit made after the worker started signing can
    # never be silently substituted into the finalized agreement.
    agreement = WorkerAgreement(
        worker_id=worker.id,
        stage=2,
        status="accepted",
        provider_type_snapshot=worker.worker_type.value,
        rendered_text=session.rendered_text,
        template_version=session.template_version,
        accepted_at=datetime.now(timezone.utc),
        ip_address=client_ip(request),
        esign_provider=session.provider,
        esign_reference_id=session.digio_document_id,
        esign_document_url=session.sign_url,
    )
    db.add(agreement)

    # The onboarding enablement fee is no longer taken in one lump sum here.
    # It's now collected in small increments (settings.ONBOARDING_FEE_INCREMENT,
    # e.g. ₹50/booking) automatically from each booking's payout as it's
    # created — see payout_service.apply_onboarding_fee_increment(), called
    # from create_payout_for_booking() — until the running total reaches
    # settings.ONBOARDING_ENABLEMENT_FEE. This avoids one single booking
    # bearing the full ₹200 hit.
    await db.commit()

    return ContractPreviewOut(stage=2, status="accepted", rendered_text=session.rendered_text, unlocked=False)


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
            stage2.status if stage2 else ("pending" if (worker.completed_visits_count or 0) >= 1 else "not_applicable")
        )
        agreement_for_fee = stage2
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
                completed_visits_count=worker.completed_visits_count or 0,
                onboarding_fee_collected=float(agreement_for_fee.onboarding_fee_collected) if agreement_for_fee else 0.0,
                onboarding_fee_target=float(settings.ONBOARDING_ENABLEMENT_FEE),
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
    stage2_unlocked = (worker.completed_visits_count or 0) >= 1
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