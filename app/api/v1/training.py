"""Training modules, assessments, completions, certificates.

Patch 4B — completes the Trainer / Reviewer / Worker content lifecycle:

  trainer (admin | reviewer)
    draft → submit_review → (approve | reject) → publish

Worker endpoints only ever expose ``status = published`` rows.

Reuses Patch 2 qualification engine — does NOT replace it. Passing an
assessment never auto-opts a worker in; the worker must still call the
opt-in endpoint from Patch 2.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import (
    CurrentUser,
    get_current_user,
    get_worker_profile,
    require_roles,
)
from app.models.enums import (
    AssessmentQuestionType,
    ContentStatus,
    UserRole,
)
from app.models.models import (
    AssessmentModule,
    TrainingCompletion,
    TrainingModule,
    User,
    WorkerAssessmentAttempt,
    WorkerProfile,
)

router = APIRouter(prefix="/training", tags=["training"])

# Trainer + Reviewer roles. Reviewer/clinical_training_lead can author
# training content and approve/publish assessments; admin is a superuser
# and can always do reviewer/trainer tasks too. clinical_trainer authors
# content but cannot approve its own submissions.
TRAINER_ROLES = (UserRole.admin, UserRole.reviewer, UserRole.clinical_trainer, UserRole.clinical_training_lead)
REVIEWER_ROLES = (UserRole.admin, UserRole.reviewer, UserRole.clinical_training_lead)
require_trainer = require_roles(*TRAINER_ROLES)
require_reviewer = require_roles(*REVIEWER_ROLES)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class TrainingModuleDraft(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    category: Optional[str] = None
    duration_minutes: int = 0
    required_for_tiers: Optional[List[str]] = None
    content_url: Optional[str] = None
    video_url: Optional[str] = None
    assessment: Optional[List[Dict[str, Any]]] = None
    pass_percent: int = 70
    is_mandatory: bool = False


class TrainingModuleUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None
    duration_minutes: Optional[int] = None
    required_for_tiers: Optional[List[str]] = None
    content_url: Optional[str] = None
    video_url: Optional[str] = None
    assessment: Optional[List[Dict[str, Any]]] = None
    pass_percent: Optional[int] = None
    is_mandatory: Optional[bool] = None


class AssessmentDraft(BaseModel):
    code: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    description: Optional[str] = None
    pass_score: int = 70
    questions: List[Dict[str, Any]]
    linked_training_module_code: Optional[str] = None


class AssessmentUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    pass_score: Optional[int] = None
    questions: Optional[List[Dict[str, Any]]] = None
    linked_training_module_code: Optional[str] = None


class ReviewBody(BaseModel):
    notes: Optional[str] = None


class AssessmentSubmit(BaseModel):
    answers: List[Any] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Question / answer scoring
# ---------------------------------------------------------------------------
_QTYPES = {t.value for t in AssessmentQuestionType}


def _validate_questions(questions: List[Dict[str, Any]]) -> None:
    if not isinstance(questions, list) or not questions:
        raise HTTPException(status_code=400, detail="questions must be a non-empty list")
    seen_ids: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            raise HTTPException(status_code=400, detail=f"question[{i}] must be an object")
        qid = q.get("id") or f"q{i}"
        if qid in seen_ids:
            raise HTTPException(status_code=400, detail=f"duplicate question id {qid}")
        seen_ids.add(qid)
        qtype = q.get("type")
        if qtype not in _QTYPES:
            raise HTTPException(status_code=400, detail=f"question[{i}] type must be one of {_QTYPES}")
        if qtype == "single_select":
            if not isinstance(q.get("options"), list) or len(q["options"]) < 2:
                raise HTTPException(status_code=400, detail=f"question[{i}] options required for single_select")
            if not isinstance(q.get("correct_index"), int):
                raise HTTPException(status_code=400, detail=f"question[{i}] correct_index required")
        elif qtype == "multi_select":
            if not isinstance(q.get("options"), list) or len(q["options"]) < 2:
                raise HTTPException(status_code=400, detail=f"question[{i}] options required for multi_select")
            if not isinstance(q.get("correct_indices"), list):
                raise HTTPException(status_code=400, detail=f"question[{i}] correct_indices required")
        elif qtype == "boolean":
            if not isinstance(q.get("correct_bool"), bool):
                raise HTTPException(status_code=400, detail=f"question[{i}] correct_bool required")
        elif qtype == "text":
            # Text questions are not auto-scored (counted as correct).
            pass


def _score_attempt(questions: List[Dict[str, Any]], answers: List[Any]) -> int:
    """Return integer percentage score across the questions list.

    Scoring rules per type:
      - single_select: answer must equal correct_index (int)
      - multi_select: answer must be a list matching correct_indices set
      - boolean: answer must equal correct_bool
      - text: always counted as correct (manual review out of scope)
    """
    if not questions:
        return 0
    correct = 0
    for i, q in enumerate(questions):
        ans = answers[i] if i < len(answers) else None
        qtype = q.get("type")
        if qtype == "single_select":
            try:
                if int(ans) == int(q.get("correct_index", -1)):
                    correct += 1
            except (TypeError, ValueError):
                pass
        elif qtype == "multi_select":
            if isinstance(ans, list):
                if set(int(x) for x in ans) == set(int(x) for x in q.get("correct_indices") or []):
                    correct += 1
        elif qtype == "boolean":
            if isinstance(ans, bool) and ans == bool(q.get("correct_bool")):
                correct += 1
        elif qtype == "text":
            if isinstance(ans, str) and ans.strip():
                correct += 1
    return int((correct / len(questions)) * 100)


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def _serialize_module(m: TrainingModule, *, include_admin_fields: bool = False, include_full_assessment: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": str(m.id),
        "code": m.code,
        "title": m.title,
        "description": m.description,
        "category": m.category,
        "duration_minutes": m.duration_minutes,
        "video_url": m.video_url,
        "content_url": m.content_url,
        "pass_percent": m.pass_percent,
        "is_mandatory": m.is_mandatory,
        "is_active": m.is_active,
        "version": m.version,
        "status": m.status.value if m.status else None,
        "required_for_tiers": m.required_for_tiers or [],
    }
    if include_full_assessment:
        out["assessment"] = m.assessment or []
    else:
        # Strip correct answers when shown to non-admin
        out["assessment"] = [
            {"question": q.get("question"), "options": q.get("options")}
            for q in (m.assessment or [])
        ]
    if include_admin_fields:
        out.update({
            "created_by": str(m.created_by) if m.created_by else None,
            "updated_by": str(m.updated_by) if m.updated_by else None,
            "reviewed_by": str(m.reviewed_by) if m.reviewed_by else None,
            "reviewed_at": m.reviewed_at.isoformat() if m.reviewed_at else None,
            "review_notes": m.review_notes,
            "published_version": m.published_version,
            "published_at": m.published_at.isoformat() if m.published_at else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
            "updated_at": m.updated_at.isoformat() if m.updated_at else None,
        })
    return out


def _serialize_assessment(a: AssessmentModule, *, include_admin_fields: bool = False, include_correct: bool = False) -> Dict[str, Any]:
    if include_correct:
        questions = a.questions or []
    else:
        questions = [
            {k: v for k, v in (q or {}).items() if k not in {"correct_index", "correct_indices", "correct_bool"}}
            for q in (a.questions or [])
        ]
    out: Dict[str, Any] = {
        "id": str(a.id),
        "code": a.code,
        "title": a.title,
        "description": a.description,
        "version": a.version,
        "pass_score": a.pass_score,
        "questions": questions,
        "linked_training_module_code": a.linked_training_module_code,
        "status": a.status.value if a.status else None,
        "is_active": a.is_active,
    }
    if include_admin_fields:
        out.update({
            "created_by": str(a.created_by) if a.created_by else None,
            "updated_by": str(a.updated_by) if a.updated_by else None,
            "reviewed_by": str(a.reviewed_by) if a.reviewed_by else None,
            "reviewed_at": a.reviewed_at.isoformat() if a.reviewed_at else None,
            "review_notes": a.review_notes,
            "published_version": a.published_version,
            "published_at": a.published_at.isoformat() if a.published_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        })
    return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ===========================================================================
# WORKER ENDPOINTS — Published content only
# ===========================================================================
@router.get("/modules")
async def list_modules(
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Worker view — only published modules."""
    res = await db.execute(
        select(TrainingModule).where(
            TrainingModule.is_active.is_(True),
            TrainingModule.status == ContentStatus.published,
        )
    )
    modules = res.scalars().all()
    cres = await db.execute(select(TrainingCompletion).where(TrainingCompletion.worker_id == profile.id))
    comps = {c.module_id: c for c in cres.scalars().all()}
    out = []
    for m in modules:
        comp = comps.get(m.id)
        out.append({
            "id": str(m.id),
            "code": m.code,
            "title": m.title,
            "description": m.description,
            "category": m.category,
            "duration_minutes": m.duration_minutes,
            "video_url": m.video_url,
            "is_mandatory": m.is_mandatory,
            "completed": bool(comp and comp.completed_at),
            "passed": comp.assessment_passed if comp else None,
            "certificate_url": comp.certificate_url if comp else None,
        })
    return out


@router.get("/modules/{module_id}")
async def get_module(module_id: UUID, db: AsyncSession = Depends(get_db), _=Depends(get_worker_profile)):
    res = await db.execute(
        select(TrainingModule).where(
            TrainingModule.id == module_id,
            TrainingModule.status == ContentStatus.published,
        )
    )
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    return {
        "id": str(m.id),
        "code": m.code,
        "title": m.title,
        "description": m.description,
        "content_url": m.content_url,
        "video_url": m.video_url,
        "duration_minutes": m.duration_minutes,
        "pass_percent": m.pass_percent,
        # Include full question data for adaptive MCQ; correct_index and explanation
        # are needed for immediate per-question feedback in the adaptive flow.
        "assessment": [
            {
                "id": q.get("id", str(i)),
                "question": q.get("question"),
                "options": q.get("options", []),
                "correct_index": q.get("correct_index"),
                "explanation": q.get("explanation", ""),
                "difficulty": q.get("difficulty", 2),
                "type": q.get("type", "single_select"),
            }
            for i, q in enumerate(m.assessment or [])
        ],
    }


@router.post("/modules/{module_id}/assessment/submit")
async def submit_module_assessment(
    module_id: UUID,
    answers: List[Any] = Body(...),
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Submit module assessment. Accepts two formats:
    - Legacy flat list: [0, 2, 1, ...]  (answer index per question by position)
    - Adaptive list:    [{"id": "ic1", "answer": 2}, ...]  (per-question id + answer)
    Adaptive format scores based on answered questions only.
    """
    res = await db.execute(
        select(TrainingModule).where(
            TrainingModule.id == module_id,
            TrainingModule.status == ContentStatus.published,
        )
    )
    m = res.scalar_one_or_none()
    if not m or not m.assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")

    # Detect adaptive vs. legacy format
    is_adaptive = answers and isinstance(answers[0], dict)
    correct = 0
    if is_adaptive:
        qmap = {q.get("id", str(i)): q for i, q in enumerate(m.assessment)}
        for item in answers:
            q = qmap.get(item.get("id"))
            if q and item.get("answer") == q.get("correct_index"):
                correct += 1
        score = int((correct / len(answers)) * 100) if answers else 0
    else:
        for idx, q in enumerate(m.assessment):
            if idx < len(answers) and answers[idx] == q.get("correct_index"):
                correct += 1
        score = int((correct / len(m.assessment)) * 100) if m.assessment else 0
    passed = score >= m.pass_percent
    cres = await db.execute(
        select(TrainingCompletion).where(
            TrainingCompletion.worker_id == profile.id,
            TrainingCompletion.module_id == module_id,
        )
    )
    comp = cres.scalar_one_or_none()
    if not comp:
        comp = TrainingCompletion(worker_id=profile.id, module_id=module_id)
        db.add(comp)
        await db.flush()
    comp.attempts = (comp.attempts or 0) + 1
    comp.assessment_score = score
    comp.assessment_passed = passed
    if passed:
        comp.completed_at = _now()
        comp.certificate_url = f"https://certs.nurseconnect.example/{comp.id}.pdf"
        await db.flush()
        try:
            from app.services.qualification import (
                evaluate_and_upsert_qualifications_for_module,
            )
            await evaluate_and_upsert_qualifications_for_module(db, profile, m, comp)
        except Exception:  # noqa: BLE001
            pass
    await db.commit()
    return {"score": score, "passed": passed, "certificate_url": comp.certificate_url, "attempts": comp.attempts}


# Worker — assessment lifecycle (standalone AssessmentModule rows)
@router.get("/assessments")
async def list_published_assessments(
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Published assessments visible to the worker."""
    res = await db.execute(
        select(AssessmentModule).where(
            AssessmentModule.is_active.is_(True),
            AssessmentModule.status == ContentStatus.published,
        )
    )
    items = res.scalars().all()
    # Latest attempts per assessment_id
    ares = await db.execute(
        select(WorkerAssessmentAttempt).where(
            WorkerAssessmentAttempt.worker_id == profile.id,
        )
    )
    attempts: Dict[UUID, WorkerAssessmentAttempt] = {}
    for att in ares.scalars().all():
        prev = attempts.get(att.assessment_id)
        if not prev or att.submitted_at > prev.submitted_at:
            attempts[att.assessment_id] = att
    out = []
    for a in items:
        att = attempts.get(a.id)
        d = _serialize_assessment(a, include_admin_fields=False, include_correct=False)
        d.update({
            "attempted": bool(att),
            "latest_score": att.score if att else None,
            "latest_passed": att.passed if att else None,
            "latest_submitted_at": att.submitted_at.isoformat() if att else None,
        })
        out.append(d)
    return out


@router.get("/assessments/{assessment_id}")
async def get_published_assessment(
    assessment_id: UUID,
    db: AsyncSession = Depends(get_db),
    _: WorkerProfile = Depends(get_worker_profile),
):
    res = await db.execute(
        select(AssessmentModule).where(
            AssessmentModule.id == assessment_id,
            AssessmentModule.status == ContentStatus.published,
        )
    )
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return _serialize_assessment(a, include_admin_fields=False, include_correct=False)


@router.post("/assessments/{assessment_id}/submit")
async def submit_assessment(
    assessment_id: UUID,
    body: AssessmentSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Worker submits answers → score computed + attempt persisted.

    Passing an assessment does NOT auto-opt the worker in for a service.
    Qualification status is upserted via Patch 2 qualification engine.
    """
    res = await db.execute(
        select(AssessmentModule).where(
            AssessmentModule.id == assessment_id,
            AssessmentModule.status == ContentStatus.published,
        )
    )
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found or not published")
    score = _score_attempt(a.questions or [], body.answers or [])
    passed = score >= int(a.pass_score or 0)
    attempt = WorkerAssessmentAttempt(
        worker_id=profile.id,
        assessment_id=a.id,
        assessment_version=int(a.published_version or a.version or 1),
        assessment_code_snapshot=a.code,
        answers=list(body.answers or []),
        score=score,
        passed=passed,
        pass_score_snapshot=int(a.pass_score or 0),
        submitted_at=_now(),
    )
    db.add(attempt)
    await db.flush()

    qualification_unlocked: list[str] = []
    if passed:
        try:
            from app.services.qualification import (
                evaluate_and_upsert_qualifications_for_assessment,
            )
            qualification_unlocked = await evaluate_and_upsert_qualifications_for_assessment(
                db, profile, a, attempt
            )
        except Exception:  # noqa: BLE001
            qualification_unlocked = []
        # Award/refresh the skill badge for this passed assessment.
        try:
            from app.services.badges import award_assessment_badge
            await award_assessment_badge(db, profile, a, attempt)
        except Exception:  # noqa: BLE001
            pass
    await db.commit()
    return {
        "score": score,
        "passed": passed,
        "pass_score": int(a.pass_score or 0),
        "completion_status": "passed" if passed else "failed",
        "submitted_at": attempt.submitted_at.isoformat(),
        "qualification_unlocked": qualification_unlocked,
    }


# ===========================================================================
# TRAINER / REVIEWER — training module lifecycle
# ===========================================================================
@router.post("/modules")
async def create_module_draft(
    payload: TrainingModuleDraft,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(TrainingModule).where(TrainingModule.code == payload.code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Module with this code already exists")
    if payload.assessment:
        # Embedded assessment is the legacy JSON-array shape. Accept any list of dicts.
        if not isinstance(payload.assessment, list):
            raise HTTPException(status_code=400, detail="assessment must be a list")
    m = TrainingModule(
        code=payload.code,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        duration_minutes=payload.duration_minutes,
        required_for_tiers=payload.required_for_tiers,
        content_url=payload.content_url,
        video_url=payload.video_url,
        assessment=payload.assessment,
        pass_percent=payload.pass_percent,
        is_mandatory=payload.is_mandatory,
        is_active=True,
        version=1,
        status=ContentStatus.draft,
        created_by=current.id,
        updated_by=current.id,
    )
    db.add(m)
    await db.commit()
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


@router.get("/admin/modules")
async def list_modules_admin(
    status: Optional[str] = None,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    """Trainer/reviewer view: all modules with full lifecycle metadata."""
    stmt = select(TrainingModule)
    if status:
        try:
            stmt = stmt.where(TrainingModule.status == ContentStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid status")
    res = await db.execute(stmt)
    return [_serialize_module(m, include_admin_fields=True, include_full_assessment=True) for m in res.scalars().all()]


@router.get("/admin/modules/{module_id}")
async def get_module_admin(
    module_id: UUID,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(TrainingModule).where(TrainingModule.id == module_id))
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


@router.put("/modules/{module_id}")
async def update_module_draft(
    module_id: UUID,
    payload: TrainingModuleUpdate,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(TrainingModule).where(TrainingModule.id == module_id))
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    if m.status not in (ContentStatus.draft, ContentStatus.rejected):
        raise HTTPException(status_code=409, detail=f"Cannot edit module in status {m.status.value}; revert to draft first")
    for k, v in payload.model_dump(exclude_unset=True).items():
        setattr(m, k, v)
    m.updated_by = current.id
    # Editing a rejected module re-arms the draft cycle
    if m.status == ContentStatus.rejected:
        m.status = ContentStatus.draft
    await db.commit()
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


@router.post("/{id}/submit-review")
async def submit_module_for_review(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(TrainingModule).where(TrainingModule.id == id))
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    if m.status not in (ContentStatus.draft, ContentStatus.rejected):
        raise HTTPException(status_code=409, detail=f"Cannot submit module in status {m.status.value}")
    m.status = ContentStatus.under_review
    m.updated_by = current.id
    m.review_notes = body.notes
    await db.commit()
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


@router.post("/{id}/approve")
async def approve_module(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """Approving a module publishes it immediately — approved content
    reaches nurses automatically, no separate publish step needed."""
    res = await db.execute(select(TrainingModule).where(TrainingModule.id == id))
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    if m.status != ContentStatus.under_review:
        raise HTTPException(status_code=409, detail=f"Cannot approve module in status {m.status.value}")
    m.status = ContentStatus.published
    m.reviewed_by = current.id
    m.reviewed_at = _now()
    m.review_notes = body.notes
    m.published_at = _now()
    m.published_version = m.version
    await db.commit()
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


@router.post("/{id}/reject")
async def reject_module(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(TrainingModule).where(TrainingModule.id == id))
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    if m.status != ContentStatus.under_review:
        raise HTTPException(status_code=409, detail=f"Cannot reject module in status {m.status.value}")
    m.status = ContentStatus.rejected
    m.reviewed_by = current.id
    m.reviewed_at = _now()
    m.review_notes = body.notes
    await db.commit()
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


@router.post("/{id}/publish")
async def publish_module(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(TrainingModule).where(TrainingModule.id == id))
    m = res.scalar_one_or_none()
    if not m:
        raise HTTPException(status_code=404, detail="Module not found")
    if m.status != ContentStatus.approved:
        raise HTTPException(status_code=409, detail=f"Cannot publish module in status {m.status.value}")
    m.status = ContentStatus.published
    m.published_at = _now()
    m.published_version = m.version
    m.review_notes = body.notes or m.review_notes
    await db.commit()
    return _serialize_module(m, include_admin_fields=True, include_full_assessment=True)


# ===========================================================================
# TRAINER / REVIEWER — Assessment module lifecycle
# ===========================================================================
@router.post("/assessments")
async def create_assessment_draft(
    payload: AssessmentDraft,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(select(AssessmentModule).where(AssessmentModule.code == payload.code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Assessment with this code already exists")
    _validate_questions(payload.questions)
    a = AssessmentModule(
        code=payload.code,
        title=payload.title,
        description=payload.description,
        pass_score=payload.pass_score,
        questions=payload.questions,
        linked_training_module_code=payload.linked_training_module_code,
        version=1,
        status=ContentStatus.draft,
        created_by=current.id,
        updated_by=current.id,
    )
    db.add(a)
    await db.commit()
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)


@router.get("/admin/assessments")
async def list_assessments_admin(
    status: Optional[str] = None,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(AssessmentModule)
    if status:
        try:
            stmt = stmt.where(AssessmentModule.status == ContentStatus(status))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid status")
    res = await db.execute(stmt)
    return [
        _serialize_assessment(a, include_admin_fields=True, include_correct=True)
        for a in res.scalars().all()
    ]


@router.get("/admin/assessments/{assessment_id}")
async def get_assessment_admin(
    assessment_id: UUID,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(AssessmentModule).where(AssessmentModule.id == assessment_id))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)


@router.put("/assessments/{assessment_id}")
async def update_assessment_draft(
    assessment_id: UUID,
    payload: AssessmentUpdate,
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(AssessmentModule).where(AssessmentModule.id == assessment_id))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if a.status not in (ContentStatus.draft, ContentStatus.rejected):
        raise HTTPException(status_code=409, detail=f"Cannot edit assessment in status {a.status.value}; revert to draft first")
    data = payload.model_dump(exclude_unset=True)
    if "questions" in data and data["questions"] is not None:
        _validate_questions(data["questions"])
    for k, v in data.items():
        setattr(a, k, v)
    a.updated_by = current.id
    if a.status == ContentStatus.rejected:
        a.status = ContentStatus.draft
    await db.commit()
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)


# ===========================================================================
# REVIEWER — audit worker assessment attempts
# ===========================================================================
@router.get("/reviewer/attempts")
async def list_worker_assessment_attempts(
    assessment_id: Optional[UUID] = None,
    worker_id: Optional[UUID] = None,
    passed: Optional[bool] = None,
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """Reviewer view: attempts taken by workers on assessments.

    Lets a reviewer check tests taken by nurses/caregivers — worker name,
    score, pass/fail, and submission timestamp — filtered by assessment,
    worker, and/or outcome.
    """
    stmt = (
        select(WorkerAssessmentAttempt, WorkerProfile, User)
        .join(WorkerProfile, WorkerProfile.id == WorkerAssessmentAttempt.worker_id)
        .join(User, User.id == WorkerProfile.user_id)
    )
    if assessment_id:
        stmt = stmt.where(WorkerAssessmentAttempt.assessment_id == assessment_id)
    if worker_id:
        stmt = stmt.where(WorkerAssessmentAttempt.worker_id == worker_id)
    if passed is not None:
        stmt = stmt.where(WorkerAssessmentAttempt.passed == passed)
    stmt = stmt.order_by(WorkerAssessmentAttempt.submitted_at.desc())

    res = await db.execute(stmt)
    return [
        {
            "attempt_id": str(attempt.id),
            "assessment_id": str(attempt.assessment_id),
            "assessment_code": attempt.assessment_code_snapshot,
            "worker_id": str(wp.id),
            "worker_name": user.full_name,
            "score": attempt.score,
            "passed": attempt.passed,
            "submitted_at": attempt.submitted_at.isoformat() if attempt.submitted_at else None,
        }
        for attempt, wp, user in res.all()
    ]


# Lifecycle endpoints (these are mounted under /assessments/{id}/...)
assessments_router = APIRouter(prefix="/assessments", tags=["assessments"])


@assessments_router.post("/{id}/submit-review")
async def submit_assessment_for_review(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_trainer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(AssessmentModule).where(AssessmentModule.id == id))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if a.status not in (ContentStatus.draft, ContentStatus.rejected):
        raise HTTPException(status_code=409, detail=f"Cannot submit assessment in status {a.status.value}")
    a.status = ContentStatus.under_review
    a.updated_by = current.id
    a.review_notes = body.notes
    await db.commit()
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)


@assessments_router.post("/{id}/approve")
async def approve_assessment(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    """Approving an assessment publishes it immediately — same
    auto-publish-on-approve behavior as training modules."""
    res = await db.execute(select(AssessmentModule).where(AssessmentModule.id == id))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if a.status != ContentStatus.under_review:
        raise HTTPException(status_code=409, detail=f"Cannot approve assessment in status {a.status.value}")
    a.status = ContentStatus.published
    a.reviewed_by = current.id
    a.reviewed_at = _now()
    a.review_notes = body.notes
    a.published_at = _now()
    a.published_version = a.version
    await db.commit()
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)


@assessments_router.post("/{id}/reject")
async def reject_assessment(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(AssessmentModule).where(AssessmentModule.id == id))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if a.status != ContentStatus.under_review:
        raise HTTPException(status_code=409, detail=f"Cannot reject assessment in status {a.status.value}")
    a.status = ContentStatus.rejected
    a.reviewed_by = current.id
    a.reviewed_at = _now()
    a.review_notes = body.notes
    await db.commit()
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)


@assessments_router.post("/{id}/publish")
async def publish_assessment(
    id: UUID,
    body: ReviewBody = Body(default_factory=ReviewBody),
    current: CurrentUser = Depends(require_reviewer),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(select(AssessmentModule).where(AssessmentModule.id == id))
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")
    if a.status != ContentStatus.approved:
        raise HTTPException(status_code=409, detail=f"Cannot publish assessment in status {a.status.value}")
    a.status = ContentStatus.published
    a.published_at = _now()
    a.published_version = a.version
    a.review_notes = body.notes or a.review_notes
    await db.commit()
    return _serialize_assessment(a, include_admin_fields=True, include_correct=True)