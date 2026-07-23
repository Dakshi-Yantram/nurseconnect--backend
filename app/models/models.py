"""All SQLAlchemy ORM models for NurseConnect platform.

Per the technical architecture v2, all rules are DB-driven.
This file consolidates all production models in one place for easy navigation.
"""
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import List, Optional
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.enums import (
    WorkerType,
    AssessmentQuestionType,
    BillingTrigger,
    BookingStatus,
    BookingType,
    ChecklistPhase,
    ComplaintStatus,
    ConsentCaptureMethod,
    ConsentStatus,
    ConsentType,
    ContentStatus,
    DisputeRaiserType,
    DisputeStatus,
    DisputeType,
    DrugAllergyEscalation,
    EscalationLevel,
    EscalationStatus,
    FamilyReportFrequency,
    Gender,
    GenderRestriction,
    InsuranceCoverageStatus,
    LedgerEntryType,
    MedicationRoute,
    NotificationChannel,
    NotificationStatus,
    OfflineRecordType,
    OfflineSyncStatus,
    PackageBookingStatus,
    PaymentStatus,
    AssessmentSessionStatus,
    PayoutBatchStatus,
    PrescriptionStatus,
    QualificationGate,
    RetentionAction,
    ServiceCategory,
    ServiceRiskLevel,
    SubsidyType,
    SupportTicketStatus,
    UserRole,
    UserStatus,
    VisitFrequency,
    VisitStatus,
    WorkerAvailability,
    WorkerOnboardingStatus,
    WorkerPayoutStatus,
    WorkerPreferenceStatus,
    WorkerQualificationSource,
    WorkerQualificationStatus,
    WorkerTier,
)


def _uuid() -> UUID:
    return uuid4()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ============================================================================
# AUTH
# ============================================================================
class User(Base):
    __tablename__ = "users"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_e164: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    email: Mapped[Optional[str]] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[Optional[str]] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(SQLEnum(UserRole, name="user_role"), nullable=False, index=True)
    status: Mapped[UserStatus] = mapped_column(SQLEnum(UserStatus, name="user_status"), default=UserStatus.pending_verification)
    password_hash: Mapped[Optional[str]] = mapped_column(String(255))
    email_verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    preferred_language: Mapped[str] = mapped_column(String(5), default="en")
    avatar_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class UserSession(Base):
    __tablename__ = "user_sessions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_jti: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_id: Mapped[Optional[str]] = mapped_column(String(255))
    device_platform: Mapped[Optional[str]] = mapped_column(String(50))
    fcm_token: Mapped[Optional[str]] = mapped_column(Text)
    ip_address: Mapped[Optional[str]] = mapped_column(String(50))
    user_agent: Mapped[Optional[str]] = mapped_column(Text)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class OtpCode(Base):
    __tablename__ = "otp_codes"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    phone_e164: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), default="login")  # login | signup | reset
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class EmailVerificationCode(Base):
    __tablename__ = "email_verification_codes"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    email: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


# ============================================================================
# CONSUMER / FAMILY
# ============================================================================
class ConsumerProfile(Base):
    __tablename__ = "consumer_profiles"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    address_line1: Mapped[Optional[str]] = mapped_column(String(255))
    address_line2: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    state: Mapped[Optional[str]] = mapped_column(String(100))
    pincode: Mapped[Optional[str]] = mapped_column(String(10))
    latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 8))
    longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(11, 8))
    emergency_contact_name: Mapped[Optional[str]] = mapped_column(String(255))
    emergency_contact_phone: Mapped[Optional[str]] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    consumer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    gender: Mapped[Optional[Gender]] = mapped_column(SQLEnum(Gender, name="gender"))
    relationship_to_consumer: Mapped[Optional[str]] = mapped_column(String(100))
    blood_group: Mapped[Optional[str]] = mapped_column(String(5))
    medical_conditions: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    allergies: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    current_medications: Mapped[Optional[list]] = mapped_column(JSONB)
    abha_id: Mapped[Optional[str]] = mapped_column(String(50))
    is_minor: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class FamilyMember(Base):
    __tablename__ = "family_members"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    consumer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id", ondelete="CASCADE"), index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    phone_e164: Mapped[str] = mapped_column(String(20))
    relationship: Mapped[Optional[str]] = mapped_column(String(100))
    can_book: Mapped[bool] = mapped_column(Boolean, default=False)
    can_receive_updates: Mapped[bool] = mapped_column(Boolean, default=True)
    is_emergency_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


# ============================================================================
# WORKER / NURSE
# ============================================================================
class WorkerProfile(Base):
    __tablename__ = "worker_profiles"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False)
    tier: Mapped[WorkerTier] = mapped_column(SQLEnum(WorkerTier, name="worker_tier"), default=WorkerTier.tier1, index=True)
    gender: Mapped[Optional[Gender]] = mapped_column(SQLEnum(Gender, name="gender"))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    onboarding_status: Mapped[WorkerOnboardingStatus] = mapped_column(
        SQLEnum(WorkerOnboardingStatus, name="worker_onboarding_status"),
        default=WorkerOnboardingStatus.documents_pending,
        index=True,
    )
    availability: Mapped[WorkerAvailability] = mapped_column(SQLEnum(WorkerAvailability, name="worker_availability"), default=WorkerAvailability.offline, index=True)
    bio: Mapped[Optional[str]] = mapped_column(Text)
    years_of_experience: Mapped[int] = mapped_column(Integer, default=0)
    languages_spoken: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    specialisations: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    registration_no: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    registration_authority: Mapped[Optional[str]] = mapped_column(String(255))
    registration_valid_until: Mapped[Optional[date]] = mapped_column(Date)
    worker_type: Mapped[WorkerType] = mapped_column(
        SQLEnum(WorkerType, name="worker_type_enum"),
        default=WorkerType.nurse, server_default="nurse", nullable=False, index=True,
    )
    base_city: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    service_radius_km: Mapped[int] = mapped_column(Integer, default=10)
    home_latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 8))
    home_longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(11, 8))
    # Patch 3 — Worker current-location awareness (for Haversine proximity).
    current_latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 8))
    current_longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(11, 8))
    current_location_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    current_location_accuracy: Mapped[Optional[int]] = mapped_column(Integer)
    rating_average: Mapped[Decimal] = mapped_column(Numeric(3, 2), default=0)
    rating_count: Mapped[int] = mapped_column(Integer, default=0)
    completed_visits_count: Mapped[int] = mapped_column(Integer, default=0)
    bank_account_holder: Mapped[Optional[str]] = mapped_column(String(255))
    bank_account_number: Mapped[Optional[str]] = mapped_column(String(50))
    bank_ifsc: Mapped[Optional[str]] = mapped_column(String(20))
    razorpay_fund_account_id: Mapped[Optional[str]] = mapped_column(String(100))
    kit_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    background_check_status: Mapped[str] = mapped_column(String(50), default="pending")
    onboarding_submitted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    onboarding_reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    onboarding_rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class WorkerDocument(Base):
    __tablename__ = "worker_documents"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True)
    document_type: Mapped[str] = mapped_column(String(100), nullable=False)  # aadhaar, registration, education, etc.
    document_number: Mapped[Optional[str]] = mapped_column(String(100))
    cloudinary_url: Mapped[str] = mapped_column(Text, nullable=False)
    cloudinary_public_id: Mapped[str] = mapped_column(Text, nullable=False)
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    verification_status: Mapped[str] = mapped_column(String(50), default="pending")
    verified_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class WorkerCertificate(Base):
    __tablename__ = "worker_certificates"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    issued_by: Mapped[Optional[str]] = mapped_column(String(255))
    issued_on: Mapped[Optional[date]] = mapped_column(Date)
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    cloudinary_url: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class WorkerKitItem(Base):
    __tablename__ = "worker_kit_items"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True)
    item_code: Mapped[str] = mapped_column(String(100), nullable=False)
    item_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_present: Mapped[bool] = mapped_column(Boolean, default=False)
    last_checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    notes: Mapped[Optional[str]] = mapped_column(Text)


# ============================================================================
# CARE CATALOGUE
# ============================================================================
class MissedVisitPolicy(Base):
    __tablename__ = "missed_visit_policies"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    policy_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    grace_period_minutes: Mapped[int] = mapped_column(Integer, default=30)
    worker_notified_at_minutes: Mapped[int] = mapped_column(Integer, default=15)
    consumer_notified_at_minutes: Mapped[int] = mapped_column(Integer, default=20)
    admin_notified_at_minutes: Mapped[int] = mapped_column(Integer, default=30)
    auto_rematch: Mapped[bool] = mapped_column(Boolean, default=True)
    rematch_attempts_max: Mapped[int] = mapped_column(Integer, default=3)
    rematch_radius_expansion_km: Mapped[int] = mapped_column(Integer, default=5)
    worker_penalty_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    consumer_refund_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=100)
    package_credit_issued: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class ChecklistTemplate(Base):
    __tablename__ = "checklist_templates"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    service_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    phase: Mapped[ChecklistPhase] = mapped_column(SQLEnum(ChecklistPhase, name="checklist_phase"), default=ChecklistPhase.all)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    questions: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    # Patch 4B — content lifecycle fields
    status: Mapped[ContentStatus] = mapped_column(
        SQLEnum(ContentStatus, name="content_status"),
        default=ContentStatus.published,
        server_default=ContentStatus.published.value,
        nullable=False,
        index=True,
    )
    updated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    published_version: Mapped[Optional[int]] = mapped_column(Integer)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class ClinicalRuleSet(Base):
    __tablename__ = "clinical_rule_sets"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    rule_set_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    vital_thresholds: Mapped[dict] = mapped_column(JSONB, nullable=False)
    red_flag_symptoms: Mapped[list] = mapped_column(JSONB, nullable=False)
    allergy_check_required: Mapped[bool] = mapped_column(Boolean, default=True)
    drug_allergy_escalation: Mapped[DrugAllergyEscalation] = mapped_column(SQLEnum(DrugAllergyEscalation, name="drug_allergy_escalation"), default=DrugAllergyEscalation.block)
    escalation_levels: Mapped[dict] = mapped_column(JSONB, nullable=False)
    refusal_of_care_protocol: Mapped[Optional[dict]] = mapped_column(JSONB)
    insurance_coverage_rules: Mapped[Optional[dict]] = mapped_column(JSONB)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class DocumentationTemplate(Base):
    __tablename__ = "documentation_templates"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    template_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    mandatory_fields: Mapped[Optional[list]] = mapped_column(JSONB)
    photo_consent_required: Mapped[bool] = mapped_column(Boolean, default=False)
    wound_image_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    # Patch 4B — content lifecycle fields
    status: Mapped[ContentStatus] = mapped_column(
        SQLEnum(ContentStatus, name="content_status"),
        default=ContentStatus.published,
        server_default=ContentStatus.published.value,
        nullable=False,
        index=True,
    )
    updated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    published_version: Mapped[Optional[int]] = mapped_column(Integer)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class ServiceCatalogue(Base):
    __tablename__ = "service_catalogue"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    service_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[ServiceCategory] = mapped_column(SQLEnum(ServiceCategory, name="service_category"), nullable=False)
    min_tier: Mapped[WorkerTier] = mapped_column(SQLEnum(WorkerTier, name="worker_tier"), default=WorkerTier.tier1)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    base_price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    max_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    commission_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    urgent_surge_pct: Mapped[int] = mapped_column(Integer, default=25)
    requires_prescription: Mapped[bool] = mapped_column(Boolean, default=False)
    prescription_drug_classes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    checklist_template_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("checklist_templates.id"))
    escalation_rule_set_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("clinical_rule_sets.id"))
    documentation_template_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("documentation_templates.id"))
    billing_trigger: Mapped[BillingTrigger] = mapped_column(SQLEnum(BillingTrigger, name="billing_trigger"), default=BillingTrigger.on_completion)
    missed_visit_policy_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("missed_visit_policies.id"))
    family_summary_template: Mapped[Optional[str]] = mapped_column(Text)
    insurance_covered: Mapped[bool] = mapped_column(Boolean, default=True)
    icon: Mapped[Optional[str]] = mapped_column(String(100))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # Patch 2 — service-level qualification gating
    required_training_module_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    required_certificate_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    required_specialty_tags: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    requires_admin_skill_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_level: Mapped[ServiceRiskLevel] = mapped_column(SQLEnum(ServiceRiskLevel, name="service_risk_level"), default=ServiceRiskLevel.LOW)
    lower_tier_override_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Patch 4B — assessment linkage (extends Patch 2 qualification engine)
    required_assessment_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    minimum_pass_score: Mapped[Optional[int]] = mapped_column(Integer)
    # Three-gate qualification model
    gate: Mapped[QualificationGate] = mapped_column(
        SQLEnum(QualificationGate, name="qualification_gate"),
        default=QualificationGate.credential_only,
        server_default=QualificationGate.credential_only.value,
        nullable=False,
    )
    practical_checklist_items: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class CarePackage(Base):
    __tablename__ = "care_packages"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    package_code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    tagline: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(Text)
    target_condition: Mapped[Optional[str]] = mapped_column(Text)
    min_tier: Mapped[WorkerTier] = mapped_column(SQLEnum(WorkerTier, name="worker_tier"), default=WorkerTier.tier1)
    gender_restriction: Mapped[GenderRestriction] = mapped_column(SQLEnum(GenderRestriction, name="gender_restriction"), default=GenderRestriction.any)
    included_service_ids: Mapped[Optional[list]] = mapped_column(ARRAY(PG_UUID(as_uuid=True)))
    primary_service_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("service_catalogue.id"))
    visit_frequency: Mapped[Optional[VisitFrequency]] = mapped_column(SQLEnum(VisitFrequency, name="visit_frequency"))
    visits_per_cycle: Mapped[Optional[int]] = mapped_column(Integer)
    cycle_duration_days: Mapped[Optional[int]] = mapped_column(Integer)
    shift_hours: Mapped[Optional[int]] = mapped_column(Integer)
    package_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    per_visit_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    subsidy_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    commission_pct: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    checklist_template_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("checklist_templates.id"))
    escalation_rule_set_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("clinical_rule_sets.id"))
    documentation_template_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("documentation_templates.id"))
    requires_prescription: Mapped[bool] = mapped_column(Boolean, default=False)
    prescription_review_required: Mapped[bool] = mapped_column(Boolean, default=False)
    missed_visit_policy_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("missed_visit_policies.id"))
    family_summary_template: Mapped[Optional[str]] = mapped_column(Text)
    family_report_frequency: Mapped[FamilyReportFrequency] = mapped_column(SQLEnum(FamilyReportFrequency, name="family_report_frequency"), default=FamilyReportFrequency.per_visit)
    insurance_covered: Mapped[bool] = mapped_column(Boolean, default=True)
    insurance_exclusions: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    version: Mapped[int] = mapped_column(Integer, default=1)
    previous_version_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_packages.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    available_cities: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    # Patch 2 — package-level qualification gating
    required_training_module_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    required_certificate_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    required_specialty_tags: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    requires_admin_skill_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    risk_level: Mapped[ServiceRiskLevel] = mapped_column(SQLEnum(ServiceRiskLevel, name="service_risk_level"), default=ServiceRiskLevel.LOW)
    lower_tier_override_allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Patch 4B — assessment linkage (extends Patch 2 qualification engine)
    required_assessment_codes: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    minimum_pass_score: Mapped[Optional[int]] = mapped_column(Integer)
    # Three-gate qualification model
    gate: Mapped[QualificationGate] = mapped_column(
        SQLEnum(QualificationGate, name="qualification_gate"),
        default=QualificationGate.credential_only,
        server_default=QualificationGate.credential_only.value,
        nullable=False,
    )
    practical_checklist_items: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


# ============================================================================
# Patch 2 — Worker service/package qualification + preference
# ============================================================================
class WorkerServiceQualification(Base):
    __tablename__ = "worker_service_qualifications"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False)
    service_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("service_catalogue.id"), index=True)
    package_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_packages.id"), index=True)
    qualification_status: Mapped[WorkerQualificationStatus] = mapped_column(
        SQLEnum(WorkerQualificationStatus, name="worker_qualification_status"),
        default=WorkerQualificationStatus.NOT_QUALIFIED,
        index=True,
    )
    qualification_source: Mapped[Optional[WorkerQualificationSource]] = mapped_column(
        SQLEnum(WorkerQualificationSource, name="worker_qualification_source")
    )
    training_module_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("training_modules.id"))
    training_completion_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("training_completions.id"))
    certificate_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_certificates.id"))
    assessment_score: Mapped[Optional[int]] = mapped_column(Integer)
    admin_approved_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    admin_approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    valid_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    suspension_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("worker_id", "service_id", name="uq_worker_service_qual_service"),
        UniqueConstraint("worker_id", "package_id", name="uq_worker_service_qual_package"),
    )


class PracticalSignOff(Base):
    """Gate 3 ("practical_verified") requirement: a trainer observes the
    worker perform the skill in person/video and signs off a checklist
    (ServiceCatalogue.practical_checklist_items / CarePackage.practical_checklist_items).
    Only counts toward qualification when `passed=True` — a failed sign-off
    is kept for audit history, the worker needs a new passing one.
    """
    __tablename__ = "practical_sign_offs"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False)
    service_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("service_catalogue.id"), index=True)
    package_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_packages.id"), index=True)
    # checklist_responses: {item_text: bool} — one entry per
    # practical_checklist_items entry on the service/package at sign-off time.
    checklist_responses: Mapped[dict] = mapped_column(JSONB, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    signed_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    __table_args__ = (
        Index("ix_practical_sign_offs_worker_service", "worker_id", "service_id"),
        Index("ix_practical_sign_offs_worker_package", "worker_id", "package_id"),
    )


class WorkerServicePreference(Base):
    __tablename__ = "worker_service_preferences"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False)
    service_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("service_catalogue.id"), index=True)
    package_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_packages.id"), index=True)
    preference_status: Mapped[WorkerPreferenceStatus] = mapped_column(
        SQLEnum(WorkerPreferenceStatus, name="worker_preference_status"),
        default=WorkerPreferenceStatus.OPTED_OUT,
        index=True,
    )
    willing_to_accept: Mapped[bool] = mapped_column(Boolean, default=False)
    preferred_radius_km: Mapped[Optional[int]] = mapped_column(Integer)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("worker_id", "service_id", name="uq_worker_service_pref_service"),
        UniqueConstraint("worker_id", "package_id", name="uq_worker_service_pref_package"),
    )


# ============================================================================
# BOOKINGS & VISITS
# ============================================================================
class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_ref: Mapped[str] = mapped_column(String(30), unique=True, index=True)
    consumer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    booking_type: Mapped[BookingType] = mapped_column(SQLEnum(BookingType, name="booking_type"), default=BookingType.one_time)
    service_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("service_catalogue.id"))
    package_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_packages.id"))
    package_booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_package_bookings.id"))
    worker_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    status: Mapped[BookingStatus] = mapped_column(SQLEnum(BookingStatus, name="booking_status"), default=BookingStatus.draft, index=True)
    scheduled_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scheduled_start_time: Mapped[time] = mapped_column(Time, nullable=False)
    scheduled_duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    is_urgent: Mapped[bool] = mapped_column(Boolean, default=False)
    address_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 8), nullable=False)
    longitude: Mapped[Decimal] = mapped_column(Numeric(11, 8), nullable=False)
    base_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    surge_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    subsidy_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    payment_status: Mapped[PaymentStatus] = mapped_column(SQLEnum(PaymentStatus, name="payment_status"), default=PaymentStatus.pending)
    razorpay_order_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    special_instructions: Mapped[Optional[str]] = mapped_column(Text)
    cancellation_reason: Mapped[Optional[str]] = mapped_column(Text)
    cancelled_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    cancelled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rematch_count: Mapped[int] = mapped_column(Integer, default=0)
    rule_set_id_snapshot: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True))
    checklist_template_id_snapshot: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True))
    documentation_template_id_snapshot: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True))
    # Patch 3 — Radius-wave dispatch tracking.
    assignment_wave: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    assignment_escalated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # When the booking became dispatchable to workers (payment captured →
    # status confirmed). The wave clock runs from here, NOT created_at:
    # workers only see confirmed bookings, so counting waves from creation
    # burned the whole 20-minute wave window while the consumer was still
    # on the payment screen.
    dispatch_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())

    __table_args__ = (
        Index("ix_bookings_worker_date", "worker_id", "scheduled_date"),
        Index("ix_bookings_consumer_date", "consumer_id", "scheduled_date"),
    )


class CarePackageBooking(Base):
    __tablename__ = "care_package_bookings"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    consumer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id"), index=True)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"))
    package_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_packages.id"))
    worker_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"))
    status: Mapped[PackageBookingStatus] = mapped_column(SQLEnum(PackageBookingStatus, name="package_booking_status"), default=PackageBookingStatus.active)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[Optional[date]] = mapped_column(Date)
    total_visits_planned: Mapped[Optional[int]] = mapped_column(Integer)
    total_visits_completed: Mapped[int] = mapped_column(Integer, default=0)
    total_visits_missed: Mapped[int] = mapped_column(Integer, default=0)
    package_price_paid: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    refund_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    current_booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True))
    rematch_reason: Mapped[Optional[str]] = mapped_column(Text)
    rematch_initiated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class VisitRecord(Base):
    __tablename__ = "visit_records"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), unique=True, index=True)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"))
    status: Mapped[VisitStatus] = mapped_column(SQLEnum(VisitStatus, name="visit_status"), default=VisitStatus.scheduled, index=True)
    en_route_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    arrived_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    check_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    check_in_latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 8))
    check_in_longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(11, 8))
    check_in_distance_metres: Mapped[Optional[int]] = mapped_column(Integer)
    check_out_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    check_out_latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 8))
    check_out_longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(11, 8))
    actual_duration_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    checklist_responses: Mapped[Optional[dict]] = mapped_column(JSONB)
    documentation_responses: Mapped[Optional[dict]] = mapped_column(JSONB)
    documentation_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    family_summary: Mapped[Optional[str]] = mapped_column(Text)
    care_notes: Mapped[Optional[str]] = mapped_column(Text)
    photo_urls: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    rating_by_consumer: Mapped[Optional[int]] = mapped_column(SmallInteger)
    rating_comment: Mapped[Optional[str]] = mapped_column(Text)
    rated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    escalation_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    is_offline_synced: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


# ============================================================================
# Patch 4 — Dynamic checklist / documentation engine
# Per-question + per-field response rows, kept versioned & immutable per visit.
# Schema is offline-sync ready (is_offline_submitted + synced_at + template_version).
# ============================================================================
class VisitChecklistResponse(Base):
    __tablename__ = "visit_checklist_responses"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True, nullable=False)
    visit_record_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("visit_records.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True, nullable=False)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True, nullable=False)
    checklist_template_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("checklist_templates.id"), index=True, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    phase: Mapped[str] = mapped_column(String(30), default="all")
    question_id: Mapped[str] = mapped_column(String(100), nullable=False)
    question_text_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    answer_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    answered_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_offline_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("booking_id", "checklist_template_id", "template_version", "question_id", name="uq_visit_checklist_resp_question"),
        Index("ix_visit_checklist_resp_visit", "visit_record_id"),
    )


class VisitDocumentationItem(Base):
    __tablename__ = "visit_documentation_items"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True, nullable=False)
    visit_record_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("visit_records.id", ondelete="CASCADE"), index=True)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True, nullable=False)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True, nullable=False)
    documentation_template_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("documentation_templates.id"), index=True, nullable=False)
    template_version: Mapped[int] = mapped_column(Integer, nullable=False)
    field_id: Mapped[str] = mapped_column(String(100), nullable=False)
    field_label_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    field_type: Mapped[str] = mapped_column(String(50), nullable=False)
    value_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    file_url: Mapped[Optional[str]] = mapped_column(Text)
    is_required: Mapped[bool] = mapped_column(Boolean, default=False)
    blocks_checkout: Mapped[bool] = mapped_column(Boolean, default=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_offline_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())
    __table_args__ = (
        UniqueConstraint("booking_id", "documentation_template_id", "template_version", "field_id", name="uq_visit_doc_field"),
        Index("ix_visit_doc_visit", "visit_record_id"),
    )




# ============================================================================
# CLINICAL DATA
# ============================================================================
class VitalSignReading(Base):
    __tablename__ = "vital_sign_readings"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    visit_record_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("visit_records.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    recorded_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"))
    bp_systolic: Mapped[Optional[int]] = mapped_column(SmallInteger)
    bp_diastolic: Mapped[Optional[int]] = mapped_column(SmallInteger)
    pulse: Mapped[Optional[int]] = mapped_column(SmallInteger)
    spo2: Mapped[Optional[int]] = mapped_column(SmallInteger)
    temperature_f: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2))
    respiratory_rate: Mapped[Optional[int]] = mapped_column(SmallInteger)
    blood_sugar_fasting: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    blood_sugar_random: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    weight_kg: Mapped[Optional[Decimal]] = mapped_column(Numeric(6, 2))
    pain_score: Mapped[Optional[int]] = mapped_column(SmallInteger)
    gcs_score: Mapped[Optional[int]] = mapped_column(SmallInteger)
    abnormal_flags: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    escalation_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    escalation_level: Mapped[EscalationLevel] = mapped_column(SQLEnum(EscalationLevel, name="escalation_level"), default=EscalationLevel.none)
    rule_set_version: Mapped[Optional[int]] = mapped_column(Integer)
    measurement_device: Mapped[Optional[str]] = mapped_column(String(100))
    is_offline_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Prescription(Base):
    __tablename__ = "prescriptions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    uploaded_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    cloudinary_url: Mapped[str] = mapped_column(Text, nullable=False)
    cloudinary_public_id: Mapped[str] = mapped_column(Text, nullable=False)
    prescribed_by_name: Mapped[Optional[str]] = mapped_column(String(255))
    prescribed_by_reg_no: Mapped[Optional[str]] = mapped_column(String(100))
    hospital_clinic: Mapped[Optional[str]] = mapped_column(String(255))
    prescribed_date: Mapped[Optional[date]] = mapped_column(Date)
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    drugs_listed: Mapped[Optional[list]] = mapped_column(JSONB)
    scheduled_drug: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[PrescriptionStatus] = mapped_column(SQLEnum(PrescriptionStatus, name="prescription_status"), default=PrescriptionStatus.pending_review)
    verified_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class MedicationAdministration(Base):
    __tablename__ = "medication_administrations"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    visit_record_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("visit_records.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"))
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    administered_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"))
    drug_name: Mapped[str] = mapped_column(String(255), nullable=False)
    drug_generic_name: Mapped[Optional[str]] = mapped_column(String(255))
    drug_class: Mapped[Optional[str]] = mapped_column(String(100))
    dose_amount: Mapped[str] = mapped_column(String(50), nullable=False)
    dose_unit: Mapped[Optional[str]] = mapped_column(String(20))
    route: Mapped[Optional[MedicationRoute]] = mapped_column(SQLEnum(MedicationRoute, name="medication_route"))
    site: Mapped[Optional[str]] = mapped_column(String(100))
    prescription_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("prescriptions.id"))
    prescription_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    allergy_check_done: Mapped[bool] = mapped_column(Boolean, default=False)
    allergy_confirmed_clear: Mapped[bool] = mapped_column(Boolean, default=False)
    patient_identified: Mapped[bool] = mapped_column(Boolean, default=False)
    expiry_checked: Mapped[bool] = mapped_column(Boolean, default=False)
    administered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    patient_response: Mapped[Optional[str]] = mapped_column(Text)
    adverse_reaction: Mapped[bool] = mapped_column(Boolean, default=False)
    adverse_reaction_notes: Mapped[Optional[str]] = mapped_column(Text)
    escalation_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    is_offline_submitted: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    batch_number: Mapped[Optional[str]] = mapped_column(String(100))
    manufacturer: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class ConsentRecord(Base):
    __tablename__ = "consent_records"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    package_booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_package_bookings.id"))
    consent_type: Mapped[ConsentType] = mapped_column(SQLEnum(ConsentType, name="consent_type"), nullable=False, index=True)
    consented_by_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    consented_by_name: Mapped[Optional[str]] = mapped_column(String(255))
    relationship_to_patient: Mapped[Optional[str]] = mapped_column(String(100))
    capture_method: Mapped[ConsentCaptureMethod] = mapped_column(SQLEnum(ConsentCaptureMethod, name="consent_capture_method"))
    consent_text_version: Mapped[Optional[str]] = mapped_column(String(50))
    consent_text_hash: Mapped[Optional[str]] = mapped_column(String(64))
    ip_address: Mapped[Optional[str]] = mapped_column(String(50))
    device_fingerprint: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[ConsentStatus] = mapped_column(SQLEnum(ConsentStatus, name="consent_status"), default=ConsentStatus.given)
    given_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    revoked_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    revocation_reason: Mapped[Optional[str]] = mapped_column(Text)
    revocation_impact: Mapped[Optional[str]] = mapped_column(Text)
    is_offline_captured: Mapped[bool] = mapped_column(Boolean, default=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ConsentTextVersion(Base):
    __tablename__ = "consent_text_versions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    consent_type: Mapped[str] = mapped_column(String(50))
    version: Mapped[str] = mapped_column(String(50))
    language: Mapped[str] = mapped_column(String(5))
    text_content: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[Optional[str]] = mapped_column(String(64))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


# ============================================================================
# ESCALATION
# ============================================================================
class Escalation(Base):
    __tablename__ = "escalations"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"), index=True)
    visit_record_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("visit_records.id"))
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"))
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"))
    level: Mapped[EscalationLevel] = mapped_column(SQLEnum(EscalationLevel, name="escalation_level"), nullable=False, index=True)
    status: Mapped[EscalationStatus] = mapped_column(SQLEnum(EscalationStatus, name="escalation_status"), default=EscalationStatus.open, index=True)
    trigger_type: Mapped[str] = mapped_column(String(100))  # vital_threshold | red_flag | manual | medication
    trigger_details: Mapped[Optional[dict]] = mapped_column(JSONB)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    notified_parties: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    sla_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    sla_breach_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    auto_call_112: Mapped[bool] = mapped_column(Boolean, default=False)
    rule_set_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("clinical_rule_sets.id"))
    rule_set_version: Mapped[Optional[int]] = mapped_column(Integer)
    acknowledged_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    # Patch 6 — Support dashboard
    assigned_to: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    internal_notes: Mapped[Optional[str]] = mapped_column(Text)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


# ============================================================================
# FINANCIAL
# ============================================================================
class FinancialLedger(Base):
    __tablename__ = "financial_ledger"
    # Hard concurrency guard: at most one 'payment_collected' row per razorpay_payment_id.
    # Partial unique index complements the application-level idempotency in
    # /payments/verify + webhook handlers — closes the race window between
    # simultaneous verify + webhook callbacks for the same payment.
    __table_args__ = (
        Index(
            "ux_financial_ledger_payment_collected_per_pid",
            "razorpay_payment_id",
            unique=True,
            postgresql_where=(
                "entry_type = 'payment_collected' AND razorpay_payment_id IS NOT NULL"
            ),
        ),
    )
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    entry_type: Mapped[LedgerEntryType] = mapped_column(SQLEnum(LedgerEntryType, name="ledger_entry_type"), nullable=False, index=True)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"), index=True)
    package_booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_package_bookings.id"))
    consumer_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id"), index=True)
    worker_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(5), default="INR")
    debit_account: Mapped[Optional[str]] = mapped_column(String(100))
    credit_account: Mapped[Optional[str]] = mapped_column(String(100))
    razorpay_payment_id: Mapped[Optional[str]] = mapped_column(Text)
    razorpay_payout_id: Mapped[Optional[str]] = mapped_column(Text)
    razorpay_refund_id: Mapped[Optional[str]] = mapped_column(Text)
    reference_ledger_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_ledger.id"))
    description: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    is_system_entry: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)


class PayoutBatch(Base):
    __tablename__ = "payout_batches"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    batch_reference: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    total_payouts: Mapped[int] = mapped_column(Integer, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    status: Mapped[PayoutBatchStatus] = mapped_column(SQLEnum(PayoutBatchStatus, name="payout_batch_status"), default=PayoutBatchStatus.created)
    initiated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    razorpay_batch_id: Mapped[Optional[str]] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class WorkerPayout(Base):
    __tablename__ = "worker_payouts"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    payout_batch_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("payout_batches.id"))
    gross_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    tds_deducted: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    net_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    razorpay_payout_id: Mapped[Optional[str]] = mapped_column(Text)
    status: Mapped[WorkerPayoutStatus] = mapped_column(SQLEnum(WorkerPayoutStatus, name="worker_payout_status"), default=WorkerPayoutStatus.pending, index=True)
    hold_reason: Mapped[Optional[str]] = mapped_column(Text)
    hold_initiated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    hold_initiated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    hold_released_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    scheduled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[Optional[str]] = mapped_column(Text)
    failure_code: Mapped[Optional[str]] = mapped_column(String(50))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_retry_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ledger_entry_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_ledger.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    raised_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    raiser_type: Mapped[DisputeRaiserType] = mapped_column(SQLEnum(DisputeRaiserType, name="dispute_raiser_type"))
    dispute_type: Mapped[DisputeType] = mapped_column(SQLEnum(DisputeType, name="dispute_type"))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_urls: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    hold_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    hold_ledger_entry_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_ledger.id"))
    status: Mapped[DisputeStatus] = mapped_column(SQLEnum(DisputeStatus, name="dispute_status"), default=DisputeStatus.open)
    assigned_to: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    consumer_refund_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    worker_penalty_amount: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    resolution_ledger_entry_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("financial_ledger.id"))
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sla_breach_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class SubsidyEligibility(Base):
    __tablename__ = "subsidy_eligibility"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    consumer_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id"), unique=True)
    subsidy_type: Mapped[SubsidyType] = mapped_column(SQLEnum(SubsidyType, name="subsidy_type"), default=SubsidyType.none)
    subsidy_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=0)
    scheme_name: Mapped[Optional[str]] = mapped_column(String(255))
    scheme_card_number: Mapped[Optional[str]] = mapped_column(String(100))
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    verified_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[Optional[date]] = mapped_column(Date)
    applicable_services: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    max_discount_per_booking: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


# ============================================================================
# OPERATIONAL
# ============================================================================
class Complaint(Base):
    __tablename__ = "complaints"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    raised_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    raiser_role: Mapped[str] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, nullable=False)
    attachments: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    status: Mapped[ComplaintStatus] = mapped_column(SQLEnum(ComplaintStatus, name="complaint_status"), default=ComplaintStatus.submitted, index=True)
    assigned_to: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    action_taken: Mapped[Optional[str]] = mapped_column(String(100))
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class RoleDefinition(Base):
    """Metadata (display name, description, permission list) for the
    admin-manageable staff roles: operations, support,
    clinical_training_lead, clinical_trainer. role_key must be one of
    those UserRole enum values — the enum itself is the source of truth
    for which roles can exist; this table lets admin configure how each
    one presents and what it's permitted to do, without a code deploy.
    """
    __tablename__ = "role_definitions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    role_key: Mapped[UserRole] = mapped_column(SQLEnum(UserRole, name="user_role"), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    permissions: Mapped[list] = mapped_column(JSONB, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class Message(Base):
    """In-app chat between the assigned worker and the consumer, scoped to
    either a single booking (one-off visit) or a care package booking
    (multi-visit package — same worker/consumer thread across the whole
    package). Exactly one of booking_id / package_booking_id is set.

    Sending is blocked once the booking/package reaches a terminal status —
    enforced at request time in app/api/v1/messaging.py by checking the
    live Booking/CarePackageBooking status, not a stored "closed" flag, so
    it can never drift out of sync with the booking's real status.
    """
    __tablename__ = "messages"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id", ondelete="CASCADE"), index=True)
    package_booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("care_package_bookings.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    sender_role: Mapped[str] = mapped_column(String(50))
    body: Mapped[str] = mapped_column(Text, nullable=False)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)


class Faq(Base):
    """Help-center FAQ entry. Managed by operations, shown to consumer
    and/or worker portals depending on `audience`."""
    __tablename__ = "faqs"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    audience: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # consumer | worker | all
    category: Mapped[Optional[str]] = mapped_column(String(100))
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(Text, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class SupportTicket(Base):
    """Consumer/nurse-raised help-center ticket, distinct from clinical
    escalations and the internal complaint/dispute workflow. Routes to the
    support role's queue."""
    __tablename__ = "support_tickets"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    ticket_ref: Mapped[str] = mapped_column(String(30), unique=True, nullable=False, index=True)
    raised_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    raiser_role: Mapped[str] = mapped_column(String(50))
    category: Mapped[str] = mapped_column(String(50), default="other")
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    status: Mapped["SupportTicketStatus"] = mapped_column(
        SQLEnum(SupportTicketStatus, name="support_ticket_status"),
        default=SupportTicketStatus.open, server_default=SupportTicketStatus.open.value,
        nullable=False, index=True,
    )
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    assigned_to: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    resolution_notes: Mapped[Optional[str]] = mapped_column(Text)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class SupportTicketMessage(Base):
    """Thread of replies on a support ticket — raiser and support staff
    exchange messages here."""
    __tablename__ = "support_ticket_messages"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    ticket_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("support_tickets.id", ondelete="CASCADE"), index=True)
    sender_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    sender_role: Mapped[str] = mapped_column(String(50))
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    actor_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    actor_type: Mapped[str] = mapped_column(String(50))
    action: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    entity_type: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    entity_id: Mapped[Optional[str]] = mapped_column(String(36), index=True)
    changes: Mapped[Optional[dict]] = mapped_column(JSONB)
    ip_address: Mapped[Optional[str]] = mapped_column(String(50))
    device_fingerprint: Mapped[Optional[str]] = mapped_column(String(255))
    request_id: Mapped[Optional[str]] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    recipient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    channel: Mapped[NotificationChannel] = mapped_column(SQLEnum(NotificationChannel, name="notification_channel"))
    template_code: Mapped[Optional[str]] = mapped_column(String(100))
    title: Mapped[Optional[str]] = mapped_column(String(255))
    body: Mapped[Optional[str]] = mapped_column(Text)
    payload: Mapped[Optional[dict]] = mapped_column(JSONB)
    status: Mapped[NotificationStatus] = mapped_column(SQLEnum(NotificationStatus, name="notification_status"), default=NotificationStatus.queued)
    provider_message_id: Mapped[Optional[str]] = mapped_column(Text)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)


class OfflineSyncQueue(Base):
    __tablename__ = "offline_sync_queue"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    device_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    record_type: Mapped[OfflineRecordType] = mapped_column(SQLEnum(OfflineRecordType, name="offline_record_type"))
    local_id: Mapped[str] = mapped_column(String(100), index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    locally_recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sync_status: Mapped[OfflineSyncStatus] = mapped_column(SQLEnum(OfflineSyncStatus, name="offline_sync_status"), default=OfflineSyncStatus.pending, index=True)
    conflict_resolution: Mapped[Optional[str]] = mapped_column(Text)
    server_record_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    __table_args__ = (UniqueConstraint("device_id", "local_id", name="uq_offline_sync_device_local"),)


class WorkerLocationLog(Base):
    __tablename__ = "worker_location_log"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"), index=True)
    latitude: Mapped[Decimal] = mapped_column(Numeric(10, 8))
    longitude: Mapped[Decimal] = mapped_column(Numeric(11, 8))
    accuracy_metres: Mapped[Optional[int]] = mapped_column(Integer)
    is_offline: Mapped[bool] = mapped_column(Boolean, default=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)
    synced_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class InsuranceCoverageAssessment(Base):
    __tablename__ = "insurance_coverage_assessments"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    booking_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"), unique=True)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"))
    assessment_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    coverage_status: Mapped[InsuranceCoverageStatus] = mapped_column(SQLEnum(InsuranceCoverageStatus, name="insurance_coverage_status"))
    coverage_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=100)
    checklist_complete: Mapped[Optional[bool]] = mapped_column(Boolean)
    consent_obtained: Mapped[Optional[bool]] = mapped_column(Boolean)
    prescription_valid: Mapped[Optional[bool]] = mapped_column(Boolean)
    tier_appropriate: Mapped[Optional[bool]] = mapped_column(Boolean)
    gps_verified: Mapped[Optional[bool]] = mapped_column(Boolean)
    escalation_timely: Mapped[Optional[bool]] = mapped_column(Boolean)
    registration_valid: Mapped[Optional[bool]] = mapped_column(Boolean)
    exclusion_reasons: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    rule_set_version: Mapped[Optional[int]] = mapped_column(Integer)
    flagged_for_review: Mapped[bool] = mapped_column(Boolean, default=False)
    reviewed_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


# ============================================================================
# TRAINING
# ============================================================================
class TrainingModule(Base):
    __tablename__ = "training_modules"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    category: Mapped[Optional[str]] = mapped_column(String(100))
    duration_minutes: Mapped[int] = mapped_column(Integer, default=0)
    required_for_tiers: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    content_url: Mapped[Optional[str]] = mapped_column(Text)
    video_url: Mapped[Optional[str]] = mapped_column(Text)
    assessment: Mapped[Optional[list]] = mapped_column(JSONB)  # [{question, options, correct_index}]
    pass_percent: Mapped[int] = mapped_column(Integer, default=70)
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    # Patch 4B — content lifecycle fields
    status: Mapped[ContentStatus] = mapped_column(
        SQLEnum(ContentStatus, name="content_status"),
        default=ContentStatus.published,
        server_default=ContentStatus.published.value,
        nullable=False,
        index=True,
    )
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    updated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    published_version: Mapped[Optional[int]] = mapped_column(Integer)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


# Patch 4B — Assessment / Test engine. Separate table from TrainingModule.assessment
# so assessments have their own lifecycle, versioning and worker attempt history.
class AssessmentModule(Base):
    __tablename__ = "assessment_modules"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    pass_score: Mapped[int] = mapped_column(Integer, default=70, server_default="70", nullable=False)
    # questions: [{id, type, text, options?, correct_index?, correct_indices?, correct_bool?, weight?,
    #              variants?: [{text, options, correct_index}]}]
    # `variants` lets a question have several equivalent versions with
    # different numbers/wording — the session engine picks one per worker
    # per attempt so two workers rarely see byte-identical questions.
    questions: Mapped[list] = mapped_column(JSONB, nullable=False)
    linked_training_module_code: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    # ── Anti-cheat assessment mechanics (Gate 2/3 "theory-verified") ──────
    randomize_options: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    questions_per_attempt: Mapped[Optional[int]] = mapped_column(Integer)  # null = use every question in the bank
    time_limit_minutes: Mapped[Optional[int]] = mapped_column(Integer)     # null = no time limit
    max_attempts: Mapped[Optional[int]] = mapped_column(Integer)           # null = unlimited
    cooldown_hours: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)  # wait after a failed attempt
    status: Mapped[ContentStatus] = mapped_column(
        SQLEnum(ContentStatus, name="content_status"),
        default=ContentStatus.draft,
        server_default=ContentStatus.draft.value,
        nullable=False,
        index=True,
    )
    created_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    updated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    review_notes: Mapped[Optional[str]] = mapped_column(Text)
    published_version: Mapped[Optional[int]] = mapped_column(Integer)
    published_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class WorkerAssessmentAttempt(Base):
    __tablename__ = "worker_assessment_attempts"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False)
    assessment_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("assessment_modules.id", ondelete="CASCADE"), index=True, nullable=False)
    assessment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    assessment_code_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    answers: Mapped[Optional[list]] = mapped_column(JSONB)
    score: Mapped[int] = mapped_column(Integer, default=0)
    passed: Mapped[bool] = mapped_column(Boolean, default=False)
    pass_score_snapshot: Mapped[int] = mapped_column(Integer, nullable=False)
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    __table_args__ = (
        Index("ix_worker_assessment_attempts_worker_ass", "worker_id", "assessment_id"),
    )


class WorkerAssessmentSession(Base):
    """Server-held state for one in-progress or completed anti-cheat
    assessment attempt. Questions are delivered one at a time; the client
    never receives correct answers, unshuffled option order, or unpicked
    question variants — everything needed to score fairly lives here.
    """
    __tablename__ = "worker_assessment_sessions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False)
    assessment_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("assessment_modules.id", ondelete="CASCADE"), index=True, nullable=False)
    assessment_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[AssessmentSessionStatus] = mapped_column(
        SQLEnum(AssessmentSessionStatus, name="assessment_session_status"),
        default=AssessmentSessionStatus.in_progress,
        server_default=AssessmentSessionStatus.in_progress.value,
        nullable=False,
        index=True,
    )
    # question_order: [{question_id, variant_index|null, option_order: [orig_idx,...]}]
    # — the picked variant + shuffled option order for every question in
    # this attempt, fixed at session start so scoring is deterministic.
    question_order: Mapped[list] = mapped_column(JSONB, nullable=False)
    current_index: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # answers: [{question_id, selected_option_index|null, correct: bool}] — appended as the worker answers.
    answers: Mapped[list] = mapped_column(JSONB, default=list)
    score: Mapped[Optional[int]] = mapped_column(Integer)
    passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_worker_assessment_sessions_worker_ass", "worker_id", "assessment_id"),
    )


class TrainingCompletion(Base):
    __tablename__ = "training_completions"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id"), index=True)
    module_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("training_modules.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    assessment_score: Mapped[Optional[int]] = mapped_column(Integer)
    assessment_passed: Mapped[Optional[bool]] = mapped_column(Boolean)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    certificate_url: Mapped[Optional[str]] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("worker_id", "module_id", name="uq_worker_module"),)


# ============================================================================
# SYSTEM / CONFIG
# ============================================================================
class SystemConfiguration(Base):
    __tablename__ = "system_configuration"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class FeatureFlag(Base):
    __tablename__ = "feature_flags"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    flag_code: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled_for_cities: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    enabled_for_tiers: Mapped[Optional[list]] = mapped_column(ARRAY(String))
    rollout_percent: Mapped[int] = mapped_column(Integer, default=100)
    updated_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class DataRetentionSchedule(Base):
    __tablename__ = "data_retention_schedules"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    data_type: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    retention_days: Mapped[int] = mapped_column(Integer, nullable=False)
    action_after: Mapped[RetentionAction] = mapped_column(SQLEnum(RetentionAction, name="retention_action"))
    last_run_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    records_processed: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


# ============================================================================
# ABHA / CARE NOTES
# ============================================================================
class AbhaRecord(Base):
    """ABHA-linked health records and uploaded medical documents."""
    __tablename__ = "abha_records"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(100))  # lab_report, prescription, discharge_summary, etc.
    title: Mapped[str] = mapped_column(String(255))
    cloudinary_url: Mapped[Optional[str]] = mapped_column(Text)
    cloudinary_public_id: Mapped[Optional[str]] = mapped_column(Text)
    abha_health_id: Mapped[Optional[str]] = mapped_column(String(50))
    issued_on: Mapped[Optional[date]] = mapped_column(Date)
    issued_by: Mapped[Optional[str]] = mapped_column(String(255))
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSONB)
    uploaded_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    is_synced_with_abha: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())


class CareNote(Base):
    """Standalone notes - generated from visits or added by family/admin."""
    __tablename__ = "care_notes"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    patient_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("patients.id"), index=True)
    booking_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("bookings.id"))
    visit_record_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("visit_records.id"))
    author_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("users.id"))
    author_role: Mapped[str] = mapped_column(String(50))
    title: Mapped[Optional[str]] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    note_type: Mapped[str] = mapped_column(String(50), default="general")  # general | observation | reminder | family_update
    visible_to_family: Mapped[bool] = mapped_column(Boolean, default=True)
    visible_to_worker: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now(), index=True)


class WorkerBadge(Base):
    """Skill-based badges awarded to a worker.

    A badge represents a proven capability: a tier level reached, an
    assessment passed, or a specific clinical skill cleared. Badges are what
    the nurse app displays and what gates which care packages/services the
    worker may opt into.
    """
    __tablename__ = "worker_badges"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    worker_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)          # e.g. TIER3, ASSESSMENT_IV_INFUSION
    label: Mapped[str] = mapped_column(String(255), nullable=False)         # human label shown in the app
    source: Mapped[str] = mapped_column(String(30), nullable=False, default="assessment")  # tier|assessment|certificate|manual
    tier: Mapped[Optional[WorkerTier]] = mapped_column(SQLEnum(WorkerTier, name="worker_tier"), nullable=True)
    service_scope: Mapped[Optional[list]] = mapped_column(ARRAY(String))     # service_codes this badge helps unlock
    awarded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        Index("ix_worker_badges_worker_code", "worker_id", "code", unique=True),
    )


class ConsumerAddress(Base):
    """Saved delivery/service addresses for a consumer (an address book).

    Works like Swiggy/Zepto: a consumer can save multiple addresses, mark one
    default, and book for someone else at a different address (recipient_name).
    """
    __tablename__ = "consumer_addresses"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    consumer_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("consumer_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(40), default="Home")          # Home / Work / Other
    recipient_name: Mapped[Optional[str]] = mapped_column(String(120))      # who the visit is for
    recipient_phone: Mapped[Optional[str]] = mapped_column(String(20))
    line1: Mapped[str] = mapped_column(String(255), nullable=False)
    line2: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    state: Mapped[Optional[str]] = mapped_column(String(100))
    pincode: Mapped[Optional[str]] = mapped_column(String(10))
    latitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 8))
    longitude: Mapped[Optional[Decimal]] = mapped_column(Numeric(11, 8))
    landmark: Mapped[Optional[str]] = mapped_column(String(255))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


# ---------------------------------------------------------------------------
# Reviewer assignment engine — models
# ---------------------------------------------------------------------------

class ReviewerProfile(Base):
    """Capacity + availability settings for a user with the reviewer role.
    Created by admin; controls who gets auto-assigned nurse review tickets."""
    __tablename__ = "reviewer_profiles"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    can_review_nurse_documents: Mapped[bool] = mapped_column(Boolean, default=True)
    max_open_tickets: Mapped[int] = mapped_column(Integer, default=20)
    specialization: Mapped[Optional[str]] = mapped_column(String(100))   # e.g. "nursing", "caregiver"
    last_assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    daily_assigned_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class NurseReviewTicket(Base):
    """One ticket per nurse onboarding submission.
    Tracks assignment, status, SLA, and priority."""
    __tablename__ = "nurse_review_tickets"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    nurse_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("worker_profiles.id", ondelete="CASCADE"), index=True, nullable=False
    )
    ticket_type: Mapped[str] = mapped_column(String(50), default="NURSE_DOCUMENT_REVIEW", nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="PENDING_REVIEW", nullable=False, index=True)
    # PENDING_REVIEW | IN_REVIEW | NEEDS_CLARIFICATION | APPROVED | REJECTED | CLOSED | CANCELLED | UNASSIGNED
    priority: Mapped[str] = mapped_column(String(20), default="NORMAL", nullable=False, index=True)
    # NORMAL | MEDIUM | HIGH
    assigned_reviewer_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("reviewer_profiles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    assigned_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    sla_due_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now())


class ReviewerAssignmentLog(Base):
    """Immutable audit log of every assignment action — auto or manual."""
    __tablename__ = "reviewer_assignment_logs"
    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=_uuid)
    ticket_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("nurse_review_tickets.id", ondelete="CASCADE"), index=True, nullable=False
    )
    reviewer_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("reviewer_profiles.id", ondelete="SET NULL"), nullable=True)
    old_reviewer_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    assignment_method: Mapped[str] = mapped_column(String(20), default="AUTO")   # AUTO | MANUAL
    assignment_reason: Mapped[Optional[str]] = mapped_column(String(500))
    assigned_by: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
