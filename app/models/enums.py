"""All enum types used across the system."""
from enum import Enum


class UserRole(str, Enum):
    consumer = "consumer"
    worker = "worker"
    admin = "admin"          # single, full-access admin
    reviewer = "reviewer"    # reviews docs, authors training, checks assessments
    system = "system"
    # Internal staff roles — created only by admin (operations) or by
    # operations (support / clinical_training_lead / clinical_trainer).
    operations = "operations"
    support = "support"
    clinical_training_lead = "clinical_training_lead"
    clinical_trainer = "clinical_trainer"


class UserStatus(str, Enum):
    pending_verification = "pending_verification"
    onboarding = "onboarding"        # worker: email verified, awaiting doc review + approval
    active = "active"
    suspended = "suspended"
    deactivated = "deactivated"


class WorkerTier(str, Enum):
    tier1 = "tier1"
    tier2 = "tier2"
    tier3 = "tier3"
    tier4 = "tier4"
    tier5 = "tier5"


class WorkerOnboardingStatus(str, Enum):
    documents_pending = "documents_pending"
    pending_review = "pending_review"
    approved = "approved"
    rejected = "rejected"
    suspended = "suspended"


class WorkerAvailability(str, Enum):
    online = "online"
    offline = "offline"
    on_visit = "on_visit"
    busy = "busy"
    on_leave = "on_leave"


class Gender(str, Enum):
    male = "male"
    female = "female"
    other = "other"


class ServiceCategory(str, Enum):
    micro_visit = "micro_visit"
    shift = "shift"
    live_in = "live_in"


class BillingTrigger(str, Enum):
    on_checkin = "on_checkin"
    on_checkout = "on_checkout"
    on_completion = "on_completion"


class VisitFrequency(str, Enum):
    daily = "daily"
    alternate_days = "alternate_days"
    twice_weekly = "twice_weekly"
    weekly = "weekly"
    as_needed = "as_needed"


class FamilyReportFrequency(str, Enum):
    per_visit = "per_visit"
    daily = "daily"
    weekly = "weekly"


class GenderRestriction(str, Enum):
    any = "any"
    female_only = "female_only"
    male_only = "male_only"


class ChecklistPhase(str, Enum):
    pre_visit = "pre_visit"
    during_visit = "during_visit"
    post_visit = "post_visit"
    all = "all"


class DrugAllergyEscalation(str, Enum):
    block = "block"
    warn = "warn"
    emergency = "emergency"


class BookingStatus(str, Enum):
    draft = "draft"
    pending_payment = "pending_payment"
    confirmed = "confirmed"
    assigned = "assigned"
    worker_en_route = "worker_en_route"
    worker_arrived = "worker_arrived"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    missed = "missed"
    rematch_pending = "rematch_pending"
    disputed = "disputed"


class BookingType(str, Enum):
    one_time = "one_time"
    package = "package"
    urgent = "urgent"


class PackageBookingStatus(str, Enum):
    active = "active"
    paused = "paused"
    completed = "completed"
    cancelled = "cancelled"
    rematch_pending = "rematch_pending"


class VisitStatus(str, Enum):
    scheduled = "scheduled"
    in_progress = "in_progress"
    completed = "completed"
    cancelled = "cancelled"
    missed = "missed"


class MedicationRoute(str, Enum):
    oral = "oral"
    iv = "iv"
    im = "im"
    sc = "sc"
    sublingual = "sublingual"
    topical = "topical"
    inhalation = "inhalation"
    other = "other"


class PrescriptionStatus(str, Enum):
    pending_review = "pending_review"
    verified = "verified"
    rejected = "rejected"
    expired = "expired"


class ConsentType(str, Enum):
    service = "service"
    photo = "photo"
    abha = "abha"
    emergency = "emergency"
    family_proxy = "family_proxy"
    medication = "medication"
    data_retention = "data_retention"
    minor = "minor"
    recording = "recording"


class ConsentCaptureMethod(str, Enum):
    digital_checkbox = "digital_checkbox"
    verbal_confirmed_by_nurse = "verbal_confirmed_by_nurse"
    written_signature = "written_signature"
    family_app_confirmation = "family_app_confirmation"


class ConsentStatus(str, Enum):
    given = "given"
    revoked = "revoked"
    expired = "expired"


class EscalationLevel(str, Enum):
    none = "none"
    watch = "watch"
    inform_family = "inform_family"
    contact_doctor = "contact_doctor"
    emergency = "emergency"


class EscalationStatus(str, Enum):
    open = "open"
    acknowledged = "acknowledged"
    investigating = "investigating"
    resolved = "resolved"
    closed = "closed"


class LedgerEntryType(str, Enum):
    payment_collected = "payment_collected"
    subsidy_applied = "subsidy_applied"
    commission_retained = "commission_retained"
    platform_fee = "platform_fee"
    tds_deducted = "tds_deducted"
    worker_payout = "worker_payout"
    refund_full = "refund_full"
    refund_partial = "refund_partial"
    wallet_credit = "wallet_credit"
    wallet_debit = "wallet_debit"
    payout_hold = "payout_hold"
    payout_hold_release = "payout_hold_release"
    dispute_hold = "dispute_hold"
    dispute_resolution = "dispute_resolution"
    penalty = "penalty"
    correction_reversal = "correction_reversal"


class PayoutBatchStatus(str, Enum):
    created = "created"
    processing = "processing"
    partially_paid = "partially_paid"
    completed = "completed"
    failed = "failed"


class WorkerPayoutStatus(str, Enum):
    pending = "pending"
    on_hold = "on_hold"
    processing = "processing"
    paid = "paid"
    failed = "failed"
    reversed = "reversed"


class DisputeRaiserType(str, Enum):
    consumer = "consumer"
    worker = "worker"
    admin = "admin"


class DisputeType(str, Enum):
    payment = "payment"
    service_quality = "service_quality"
    no_show = "no_show"
    overcharge = "overcharge"
    other = "other"


class DisputeStatus(str, Enum):
    open = "open"
    investigating = "investigating"
    resolved_consumer_favour = "resolved_consumer_favour"
    resolved_worker_favour = "resolved_worker_favour"
    resolved_split = "resolved_split"
    closed = "closed"


class SubsidyType(str, Enum):
    bpl = "bpl"
    nhs_scheme = "nhs_scheme"
    state_scheme = "state_scheme"
    institutional = "institutional"
    none = "none"


class OfflineRecordType(str, Enum):
    checklist_response = "checklist_response"
    vital_signs = "vital_signs"
    medication_admin = "medication_admin"
    checkin = "checkin"
    checkout = "checkout"
    photo = "photo"
    consent = "consent"
    escalation = "escalation"


class OfflineSyncStatus(str, Enum):
    pending = "pending"
    processing = "processing"
    synced = "synced"
    failed = "failed"
    conflict = "conflict"


class RetentionAction(str, Enum):
    delete = "delete"
    anonymise = "anonymise"
    archive = "archive"
    archive_and_anonymise = "archive_and_anonymise"


class SupportTicketStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class SupportTicketCategory(str, Enum):
    booking = "booking"
    billing = "billing"
    clinical = "clinical"
    nurse_conduct = "nurse_conduct"
    technical = "technical"
    other = "other"


class InsuranceCoverageStatus(str, Enum):
    covered = "covered"
    not_covered = "not_covered"
    conditional = "conditional"
    under_review = "under_review"


class NotificationChannel(str, Enum):
    push = "push"
    sms = "sms"
    whatsapp = "whatsapp"
    email = "email"
    in_app = "in_app"


class NotificationStatus(str, Enum):
    queued = "queued"
    sent = "sent"
    delivered = "delivered"
    failed = "failed"
    read = "read"


class PaymentStatus(str, Enum):
    pending = "pending"
    initiated = "initiated"
    captured = "captured"
    failed = "failed"
    refunded = "refunded"
    partially_refunded = "partially_refunded"


class ComplaintStatus(str, Enum):
    submitted = "submitted"
    acknowledged = "acknowledged"
    investigating = "investigating"
    resolved_action_taken = "resolved_action_taken"
    resolved_no_action = "resolved_no_action"
    closed = "closed"


# ============================================================================
# Patch 2 — Worker package/service qualification + opt-in
# ============================================================================
class WorkerQualificationStatus(str, Enum):
    NOT_QUALIFIED = "NOT_QUALIFIED"
    TRAINING_REQUIRED = "TRAINING_REQUIRED"
    TEST_FAILED = "TEST_FAILED"
    QUALIFIED_PENDING_APPROVAL = "QUALIFIED_PENDING_APPROVAL"
    APPROVED = "APPROVED"
    SUSPENDED = "SUSPENDED"
    EXPIRED = "EXPIRED"


class WorkerQualificationSource(str, Enum):
    TIER = "TIER"
    TRAINING = "TRAINING"
    CERTIFICATE = "CERTIFICATE"
    ADMIN_APPROVAL = "ADMIN_APPROVAL"
    GRANDFATHERED = "GRANDFATHERED"


class WorkerPreferenceStatus(str, Enum):
    OPTED_IN = "OPTED_IN"
    OPTED_OUT = "OPTED_OUT"
    PAUSED = "PAUSED"


class ServiceRiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AssessmentSessionStatus(str, Enum):
    in_progress = "in_progress"
    completed = "completed"
    expired = "expired"


class QualificationGate(str, Enum):
    """Which verification track a service/care package requires.

    credential_only  — identity, basic training, behavioral suitability,
                        maybe background check. No formal exam — don't
                        over-test low-risk tasks (companionship, meal
                        assistance, walking support, ...).
    theory_verified   — a remote knowledge assessment is required (anti-cheat
                        assessment mechanics apply), no in-person practical.
    practical_verified — highest risk: theory must be passed AND a trainer
                        must observe and sign off a practical competency
                        checklist before the system unlocks the service.
    """
    credential_only = "credential_only"
    theory_verified = "theory_verified"
    practical_verified = "practical_verified"


# ============================================================================
# Patch 4B — Trainer / Reviewer content lifecycle
# ============================================================================
class ContentStatus(str, Enum):
    draft = "draft"
    under_review = "under_review"
    approved = "approved"
    rejected = "rejected"
    published = "published"


class AssessmentQuestionType(str, Enum):
    single_select = "single_select"
    multi_select = "multi_select"
    boolean = "boolean"
    text = "text"

class WorkerType(str, Enum):
    """A professionally-trained nurse vs. a non-clinical caregiver/helper.
    Drives which onboarding documents are required and which services they can
    be qualified for."""
    nurse = "nurse"
    caregiver = "caregiver"
