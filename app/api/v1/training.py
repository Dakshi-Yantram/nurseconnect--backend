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

import random
from datetime import datetime, timedelta, timezone
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
    require_clinical_trainer,
    require_roles,
)
from app.models.enums import (
    AssessmentQuestionType,
    AssessmentSessionStatus,
    ContentStatus,
    UserRole,
)
from app.models.models import (
    AssessmentModule,
    CarePackage,
    PracticalSignOff,
    ServiceCatalogue,
    TrainingCompletion,
    TrainingModule,
    User,
    WorkerAssessmentAttempt,
    WorkerAssessmentSession,
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
        # The anti-cheat session engine keys everything off `id` — assign a
        # stable positional fallback here (not just for the uniqueness
        # check) so every question is guaranteed a real id once persisted.
        qid = q.get("id") or f"q{i}"
        q["id"] = qid
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

        # Optional variants — same shape rules as the base question, minus
        # `id`/`type` (inherited from the parent). Lets a question have
        # several equivalent versions with different numbers/wording so the
        # anti-cheat session engine can hand different workers different
        # values for "the same" question.
        variants = q.get("variants")
        if variants is not None:
            if not isinstance(variants, list) or not variants:
                raise HTTPException(status_code=400, detail=f"question[{i}] variants must be a non-empty list if present")
            for vi, v in enumerate(variants):
                if not isinstance(v, dict):
                    raise HTTPException(status_code=400, detail=f"question[{i}] variant[{vi}] must be an object")
                if qtype == "single_select":
                    if not isinstance(v.get("options"), list) or len(v["options"]) < 2:
                        raise HTTPException(status_code=400, detail=f"question[{i}] variant[{vi}] options required")
                    if not isinstance(v.get("correct_index"), int):
                        raise HTTPException(status_code=400, detail=f"question[{i}] variant[{vi}] correct_index required")
                elif qtype == "multi_select":
                    if not isinstance(v.get("options"), list) or len(v["options"]) < 2:
                        raise HTTPException(status_code=400, detail=f"question[{i}] variant[{vi}] options required")
                    if not isinstance(v.get("correct_indices"), list):
                        raise HTTPException(status_code=400, detail=f"question[{i}] variant[{vi}] correct_indices required")
                elif qtype == "boolean":
                    if not isinstance(v.get("correct_bool"), bool):
                        raise HTTPException(status_code=400, detail=f"question[{i}] variant[{vi}] correct_bool required")


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
        # Anti-cheat config — safe to show upfront (no answers), lets the
        # worker know what they're about to attempt before starting.
        "randomize_options": a.randomize_options,
        "questions_per_attempt": a.questions_per_attempt or len(a.questions or []),
        "time_limit_minutes": a.time_limit_minutes,
        "max_attempts": a.max_attempts,
        "cooldown_hours": a.cooldown_hours,
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
    now = _now()
    out = []
    for a in items:
        att = attempts.get(a.id)
        eligibility = await _assessment_start_eligibility(db, profile, a, now)
        d = _serialize_assessment(a, include_admin_fields=False, include_correct=False)
        d.update({
            "attempted": bool(att),
            "latest_score": att.score if att else None,
            "latest_passed": att.passed if att else None,
            "latest_submitted_at": att.submitted_at.isoformat() if att else None,
            "can_start": eligibility["can_start"],
            "locked_reason": eligibility["reason"],
            "attempts_used": eligibility["attempts_used"],
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


async def _finalize_scored_attempt(
    db: AsyncSession,
    profile: WorkerProfile,
    a: AssessmentModule,
    answers: list,
    score: int,
    passed: bool,
) -> tuple[WorkerAssessmentAttempt, list[str]]:
    """Shared tail end of scoring an attempt: persist the attempt row, then
    (if passed) run it through the qualification engine and award a badge.
    Used by both the legacy flat-submit endpoint and the anti-cheat session
    engine so qualification unlock logic only lives in one place."""
    attempt = WorkerAssessmentAttempt(
        worker_id=profile.id,
        assessment_id=a.id,
        assessment_version=int(a.published_version or a.version or 1),
        assessment_code_snapshot=a.code,
        answers=answers,
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
        try:
            from app.services.badges import award_assessment_badge
            await award_assessment_badge(db, profile, a, attempt)
        except Exception:  # noqa: BLE001
            pass
    return attempt, qualification_unlocked


@router.post("/assessments/{assessment_id}/submit")
async def submit_assessment(
    assessment_id: UUID,
    body: AssessmentSubmit,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    """Legacy flat-submit path — answers all sent at once, no anti-cheat
    protections. Kept for assessments that don't need Gate 2/3 rigor.
    New anti-cheat assessments should use POST /assessments/{id}/start
    + POST /assessments/{id}/sessions/{session_id}/answer instead.

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
    attempt, qualification_unlocked = await _finalize_scored_attempt(
        db, profile, a, list(body.answers or []), score, passed
    )
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
# ANTI-CHEAT ASSESSMENT SESSION ENGINE (Gate 2/3 "theory-verified")
#
# Questions are delivered one at a time; correct answers, unpicked question
# variants, and true option order NEVER reach the client. All scoring
# happens server-side against `question_order` recorded at session start.
# ===========================================================================
def _build_question_order(questions: List[Dict[str, Any]], count: Optional[int]) -> List[Dict[str, Any]]:
    # Defensive fallback for assessments authored before question ids were
    # made mandatory — positional id, stable for the lifetime of this list.
    indexed = [(q, q.get("id") or f"q{i}") for i, q in enumerate(questions or [])]
    random.shuffle(indexed)
    if count is not None and count > 0:
        indexed = indexed[:count]
    order: List[Dict[str, Any]] = []
    for q, qid in indexed:
        variants = q.get("variants") or []
        variant_index = random.randrange(len(variants)) if variants else None
        active = variants[variant_index] if variant_index is not None else q
        n_options = len(active.get("options") or [])
        option_order = list(range(n_options))
        random.shuffle(option_order)
        order.append({
            "question_id": qid,
            "variant_index": variant_index,
            "option_order": option_order,
        })
    return order


def _resolve_question(assessment_questions: List[Dict[str, Any]], entry: Dict[str, Any]) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return (base_question, active_question) — active is the picked variant, or base if none."""
    base = next(
        (q for i, q in enumerate(assessment_questions) if (q.get("id") or f"q{i}") == entry["question_id"]),
        None,
    )
    if base is None:
        raise HTTPException(status_code=500, detail="Assessment question no longer exists")
    variant_index = entry.get("variant_index")
    if variant_index is not None:
        variants = base.get("variants") or []
        if variant_index < len(variants):
            return base, variants[variant_index]
    return base, base


def _sanitize_question(assessment_questions: List[Dict[str, Any]], entry: Dict[str, Any], index: int, total: int) -> Dict[str, Any]:
    base, active = _resolve_question(assessment_questions, entry)
    options = active.get("options") or []
    option_order = entry["option_order"]
    shown_options = [options[i] for i in option_order if i < len(options)]
    return {
        "question_number": index + 1,
        "total_questions": total,
        "question_id": entry["question_id"],
        "type": base.get("type", "single_select"),
        "text": active.get("text") or active.get("question") or base.get("text") or base.get("question"),
        "options": shown_options,
        "difficulty": base.get("difficulty"),
    }


def _grade_answer(assessment_questions: List[Dict[str, Any]], entry: Dict[str, Any], answer: Any) -> bool:
    base, active = _resolve_question(assessment_questions, entry)
    qtype = base.get("type", "single_select")
    option_order = entry["option_order"]

    if qtype == "single_select":
        try:
            shown_idx = int(answer)
        except (TypeError, ValueError):
            return False
        if shown_idx < 0 or shown_idx >= len(option_order):
            return False
        original_idx = option_order[shown_idx]
        return original_idx == active.get("correct_index")
    if qtype == "boolean":
        try:
            shown_idx = int(answer)
        except (TypeError, ValueError):
            return False
        if shown_idx < 0 or shown_idx >= len(option_order):
            return False
        original_idx = option_order[shown_idx]
        # boolean questions are authored as a 2-option list ["True","False"]
        # with correct_bool telling us which one is right.
        correct_idx = 0 if active.get("correct_bool") else 1
        return original_idx == correct_idx
    if qtype == "multi_select":
        if not isinstance(answer, list):
            return False
        try:
            shown_indices = {int(x) for x in answer}
        except (TypeError, ValueError):
            return False
        original_indices = {option_order[i] for i in shown_indices if 0 <= i < len(option_order)}
        return original_indices == set(active.get("correct_indices") or [])
    if qtype == "text":
        return isinstance(answer, str) and bool(answer.strip())
    return False


class SessionAnswerRequest(BaseModel):
    answer: Any


async def _assessment_start_eligibility(
    db: AsyncSession, profile: WorkerProfile, a: AssessmentModule, now: datetime,
) -> Dict[str, Any]:
    """Single source of truth for max_attempts / cooldown gating — used by
    both /start (enforces it) and the list endpoint (reports it)."""
    completed_count = 0
    if a.max_attempts or a.cooldown_hours:
        cres = await db.execute(
            select(WorkerAssessmentSession).where(
                WorkerAssessmentSession.worker_id == profile.id,
                WorkerAssessmentSession.assessment_id == a.id,
                WorkerAssessmentSession.status == AssessmentSessionStatus.completed,
            ).order_by(WorkerAssessmentSession.completed_at.desc())
        )
        completed_sessions = cres.scalars().all()
        completed_count = len(completed_sessions)
    else:
        completed_sessions = []

    if a.max_attempts and completed_count >= a.max_attempts:
        return {
            "can_start": False,
            "reason": f"Maximum attempts ({a.max_attempts}) reached for this assessment.",
            "unlock_at": None,
            "attempts_used": completed_count,
        }

    if a.cooldown_hours and completed_sessions:
        last = completed_sessions[0]
        if last.passed is False and last.completed_at:
            unlock_at = last.completed_at + timedelta(hours=a.cooldown_hours)
            if now < unlock_at:
                return {
                    "can_start": False,
                    "reason": f"Try again after {unlock_at.isoformat()} (cooldown after a failed attempt).",
                    "unlock_at": unlock_at,
                    "attempts_used": completed_count,
                }

    return {"can_start": True, "reason": None, "unlock_at": None, "attempts_used": completed_count}


@router.post("/assessments/{assessment_id}/start")
async def start_assessment_session(
    assessment_id: UUID,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    res = await db.execute(
        select(AssessmentModule).where(
            AssessmentModule.id == assessment_id,
            AssessmentModule.status == ContentStatus.published,
        )
    )
    a = res.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found or not published")

    now = _now()

    # Resume an unexpired in-progress session instead of starting a new one.
    sres = await db.execute(
        select(WorkerAssessmentSession).where(
            WorkerAssessmentSession.worker_id == profile.id,
            WorkerAssessmentSession.assessment_id == assessment_id,
            WorkerAssessmentSession.status == AssessmentSessionStatus.in_progress,
        ).order_by(WorkerAssessmentSession.started_at.desc())
    )
    existing = sres.scalar_one_or_none()
    if existing:
        if existing.expires_at and existing.expires_at < now:
            existing.status = AssessmentSessionStatus.expired
            await db.commit()
        else:
            question = _sanitize_question(a.questions or [], existing.question_order[existing.current_index], existing.current_index, len(existing.question_order))
            return {
                "session_id": str(existing.id),
                "expires_at": existing.expires_at.isoformat() if existing.expires_at else None,
                "question": question,
            }

    eligibility = await _assessment_start_eligibility(db, profile, a, now)
    if not eligibility["can_start"]:
        raise HTTPException(status_code=403, detail=eligibility["reason"])

    order = _build_question_order(a.questions or [], a.questions_per_attempt)
    if not order:
        raise HTTPException(status_code=500, detail="Assessment has no questions")

    expires_at = now + timedelta(minutes=a.time_limit_minutes) if a.time_limit_minutes else None
    session = WorkerAssessmentSession(
        worker_id=profile.id,
        assessment_id=a.id,
        assessment_version=int(a.published_version or a.version or 1),
        question_order=order,
        current_index=0,
        answers=[],
        started_at=now,
        expires_at=expires_at,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)

    question = _sanitize_question(a.questions or [], order[0], 0, len(order))
    return {
        "session_id": str(session.id),
        "expires_at": expires_at.isoformat() if expires_at else None,
        "question": question,
    }


@router.post("/assessments/{assessment_id}/sessions/{session_id}/answer")
async def answer_assessment_session(
    assessment_id: UUID,
    session_id: UUID,
    payload: SessionAnswerRequest,
    profile: WorkerProfile = Depends(get_worker_profile),
    db: AsyncSession = Depends(get_db),
):
    ares = await db.execute(
        select(AssessmentModule).where(AssessmentModule.id == assessment_id)
    )
    a = ares.scalar_one_or_none()
    if not a:
        raise HTTPException(status_code=404, detail="Assessment not found")

    sres = await db.execute(
        select(WorkerAssessmentSession).where(
            WorkerAssessmentSession.id == session_id,
            WorkerAssessmentSession.worker_id == profile.id,
            WorkerAssessmentSession.assessment_id == assessment_id,
        )
    )
    session = sres.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != AssessmentSessionStatus.in_progress:
        raise HTTPException(status_code=409, detail=f"Session is {session.status.value}, not in progress")

    now = _now()
    if session.expires_at and session.expires_at < now:
        session.status = AssessmentSessionStatus.expired
        await db.commit()
        raise HTTPException(status_code=410, detail="Time limit exceeded — session expired")

    if session.current_index >= len(session.question_order):
        raise HTTPException(status_code=409, detail="Session already has all questions answered")

    entry = session.question_order[session.current_index]
    correct = _grade_answer(a.questions or [], entry, payload.answer)

    # Rebuild JSONB list — SQLAlchemy won't track in-place mutation on JSONB columns.
    new_answers = list(session.answers or []) + [{
        "question_id": entry["question_id"],
        "selected": payload.answer,
        "correct": correct,
    }]
    session.answers = new_answers
    session.current_index += 1

    total = len(session.question_order)
    if session.current_index >= total:
        # Finished — score, persist a WorkerAssessmentAttempt, run qualification.
        correct_count = sum(1 for x in new_answers if x["correct"])
        score = int((correct_count / total) * 100) if total else 0
        passed = score >= int(a.pass_score or 0)
        session.status = AssessmentSessionStatus.completed
        session.score = score
        session.passed = passed
        session.completed_at = now
        await db.flush()

        attempt, qualification_unlocked = await _finalize_scored_attempt(
            db, profile, a,
            [x["selected"] for x in new_answers],
            score, passed,
        )
        await db.commit()
        return {
            "finished": True,
            "correct": correct,
            "score": score,
            "passed": passed,
            "pass_score": int(a.pass_score or 0),
            "qualification_unlocked": qualification_unlocked,
        }

    await db.commit()
    next_entry = session.question_order[session.current_index]
    next_question = _sanitize_question(a.questions or [], next_entry, session.current_index, total)
    return {
        "finished": False,
        "correct": correct,
        "question": next_question,
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


# ===========================================================================
# GATE 3 — PRACTICAL SIGN-OFF ("practical_verified" services/packages)
#
# A clinical_trainer or clinical_training_lead observes the worker perform
# the skill and ticks off the checklist authored on the service/package
# (ServiceCatalogue.practical_checklist_items / CarePackage.practical_checklist_items).
# Only a passed sign-off counts toward qualification (see qualification.py).
# ===========================================================================
class PracticalSignOffRequest(BaseModel):
    worker_id: UUID
    target_type: str  # "service" | "package"
    target_id: UUID
    checklist_responses: Dict[str, bool]
    passed: bool
    notes: Optional[str] = None


def _serialize_signoff(s: PracticalSignOff, target_name: Optional[str] = None, signer_name: Optional[str] = None) -> dict:
    return {
        "id": str(s.id),
        "worker_id": str(s.worker_id),
        "target_type": "service" if s.service_id else "package",
        "target_id": str(s.service_id or s.package_id),
        "target_name": target_name,
        "checklist_responses": s.checklist_responses,
        "passed": s.passed,
        "notes": s.notes,
        "signed_by": str(s.signed_by),
        "signer_name": signer_name,
        "signed_at": s.signed_at.isoformat(),
    }


@router.get("/practical-targets")
async def list_practical_targets(
    current: CurrentUser = Depends(require_clinical_trainer),
    db: AsyncSession = Depends(get_db),
):
    """Every active Gate 3 ("practical_verified") service/package, with its
    checklist items — populates the sign-off form's target picker."""
    from app.models.enums import QualificationGate as _QG
    from app.models.models import CarePackage as _CP, ServiceCatalogue as _SC

    sres = await db.execute(select(_SC).where(_SC.is_active.is_(True), _SC.gate == _QG.practical_verified))
    services = sres.scalars().all()
    pres = await db.execute(select(_CP).where(_CP.is_active.is_(True), _CP.gate == _QG.practical_verified))
    packages = pres.scalars().all()

    return [
        {"target_type": "service", "target_id": str(s.id), "name": s.name, "checklist_items": s.practical_checklist_items or []}
        for s in services
    ] + [
        {"target_type": "package", "target_id": str(p.id), "name": p.name, "checklist_items": p.practical_checklist_items or []}
        for p in packages
    ]


@router.get("/workers/search")
async def search_workers_for_signoff(
    q: str = "",
    current: CurrentUser = Depends(require_clinical_trainer),
    db: AsyncSession = Depends(get_db),
):
    """Minimal worker lookup so a trainer can find who they're signing off
    for by name/phone, without needing the consumer/admin-only /workers/search."""
    from app.models.models import WorkerProfile as WP
    stmt = select(WP, User).join(User, User.id == WP.user_id)
    if q.strip():
        like = f"%{q.strip()}%"
        stmt = stmt.where((User.full_name.ilike(like)) | (User.phone_e164.ilike(like)))
    stmt = stmt.limit(20)
    rows = (await db.execute(stmt)).all()
    return [
        {"worker_id": str(wp.id), "full_name": u.full_name, "phone_e164": u.phone_e164, "tier": wp.tier.value if wp.tier else None}
        for wp, u in rows
    ]


@router.post("/practical-signoff")
async def create_practical_signoff(
    payload: PracticalSignOffRequest,
    current: CurrentUser = Depends(require_clinical_trainer),
    db: AsyncSession = Depends(get_db),
):
    if payload.target_type not in ("service", "package"):
        raise HTTPException(status_code=400, detail="target_type must be 'service' or 'package'")

    target_name: Optional[str] = None
    checklist_items: List[str] = []
    if payload.target_type == "service":
        res = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == payload.target_id))
        target = res.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Service not found")
        target_name = target.name
        checklist_items = list(target.practical_checklist_items or [])
    else:
        res = await db.execute(select(CarePackage).where(CarePackage.id == payload.target_id))
        target = res.scalar_one_or_none()
        if not target:
            raise HTTPException(status_code=404, detail="Care package not found")
        target_name = target.name
        checklist_items = list(target.practical_checklist_items or [])

    if checklist_items:
        missing = [item for item in checklist_items if item not in payload.checklist_responses]
        if missing:
            raise HTTPException(status_code=400, detail=f"Missing checklist responses for: {missing}")

    signoff = PracticalSignOff(
        worker_id=payload.worker_id,
        service_id=payload.target_id if payload.target_type == "service" else None,
        package_id=payload.target_id if payload.target_type == "package" else None,
        checklist_responses=payload.checklist_responses,
        passed=payload.passed,
        notes=payload.notes,
        signed_by=current.id,
    )
    db.add(signoff)
    await db.commit()
    await db.refresh(signoff)

    if payload.passed:
        try:
            from app.services.qualification import evaluate_and_upsert_qualification_for_practical_signoff
            wres = await db.execute(select(WorkerProfile).where(WorkerProfile.id == payload.worker_id))
            worker = wres.scalar_one_or_none()
            if worker:
                await evaluate_and_upsert_qualification_for_practical_signoff(db, worker, target)
                await db.commit()
        except Exception:  # noqa: BLE001
            pass

    return _serialize_signoff(signoff, target_name=target_name, signer_name=current.user.full_name or current.user.email)


@router.get("/practical-signoff")
async def list_practical_signoffs(
    worker_id: Optional[UUID] = None,
    target_type: Optional[str] = None,
    target_id: Optional[UUID] = None,
    current: CurrentUser = Depends(require_clinical_trainer),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(PracticalSignOff).order_by(PracticalSignOff.signed_at.desc())
    if worker_id:
        stmt = stmt.where(PracticalSignOff.worker_id == worker_id)
    if target_type == "service" and target_id:
        stmt = stmt.where(PracticalSignOff.service_id == target_id)
    elif target_type == "package" and target_id:
        stmt = stmt.where(PracticalSignOff.package_id == target_id)
    rows = (await db.execute(stmt)).scalars().all()

    signer_ids = {r.signed_by for r in rows}
    names: Dict[UUID, str] = {}
    if signer_ids:
        ures = await db.execute(select(User).where(User.id.in_(signer_ids)))
        names = {u.id: (u.full_name or u.email or str(u.id)) for u in ures.scalars().all()}

    return [_serialize_signoff(r, signer_name=names.get(r.signed_by)) for r in rows]