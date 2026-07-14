"""Pydantic v2 schemas for request/response payloads."""
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import (
    WorkerType,
    BillingTrigger,
    BookingStatus,
    BookingType,
    ConsentCaptureMethod,
    ConsentType,
    DisputeType,
    EscalationLevel,
    EscalationStatus,
    Gender,
    MedicationRoute,
    NotificationChannel,
    OfflineRecordType,
    PackageBookingStatus,
    PaymentStatus,
    PrescriptionStatus,
    ServiceCategory,
    UserRole,
    UserStatus,
    VisitStatus,
    WorkerAvailability,
    WorkerOnboardingStatus,
    WorkerTier,
)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True, arbitrary_types_allowed=True)


# ----- AUTH -----
class OtpSendRequest(BaseModel):
    phone_e164: str = Field(min_length=8, max_length=20)
    role: UserRole = UserRole.consumer
    purpose: str = "login"


class OtpSendResponse(BaseModel):
    sent: bool
    phone_e164: str
    expires_in_seconds: int
    dev_otp: Optional[str] = None  # populated in dev mode only


class OtpVerifyRequest(BaseModel):
    phone_e164: str
    code: str
    role: UserRole = UserRole.consumer
    device_id: Optional[str] = None
    device_platform: Optional[str] = None
    fcm_token: Optional[str] = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class RefreshRequest(BaseModel):
    refresh_token: str


class RegisterRequest(BaseModel):
    phone_e164: str
    full_name: str
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    role: UserRole = UserRole.consumer
    worker_type: Optional[WorkerType] = None  # 'nurse' or 'caregiver' when role=worker


class RegisterResponse(BaseModel):
    registered: bool
    email: EmailStr
    verification_required: bool = True
    expires_in_seconds: int
    dev_verification_code: Optional[str] = None



class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)



class ResendEmailVerificationRequest(BaseModel):
    email: EmailStr


class PasswordLoginRequest(BaseModel):
    email: EmailStr
    password: str
    device_id: Optional[str] = None
    device_platform: Optional[str] = None
    fcm_token: Optional[str] = None


class PhoneLoginRequest(BaseModel):
    """Passwordless phone login/register used by the mobile app (intapp).

    Distinct from PasswordLoginRequest — the mobile app has no password
    field and authenticates purely off phone number + role. If no account
    exists yet for this phone+role, one is created (and activated)
    immediately, same as `loginDirect` on the client expects.
    """
    phone_e164: str
    full_name: Optional[str] = None
    code: Optional[str] = None  # reserved for a future real OTP step; unused for now
    role: UserRole = UserRole.consumer
    device_id: Optional[str] = None
    device_platform: Optional[str] = None
    fcm_token: Optional[str] = None

class UserOut(ORMModel):
    id: UUID
    phone_e164: str
    email: Optional[str] = None
    full_name: Optional[str] = None
    role: UserRole
    status: UserStatus
    email_verified_at: Optional[datetime] = None
    avatar_url: Optional[str] = None
    preferred_language: str
    created_at: datetime
    email_verified_at: Optional[datetime] = None


class AuthResponse(BaseModel):
    user: UserOut
    tokens: TokenPair


# ----- CONSUMER / PATIENT -----
class ConsumerProfileUpdate(BaseModel):
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None


class ConsumerProfileOut(ORMModel):
    id: UUID
    user_id: UUID
    address_line1: Optional[str] = None
    address_line2: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    pincode: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    emergency_contact_name: Optional[str] = None
    emergency_contact_phone: Optional[str] = None


class PatientCreate(BaseModel):
    full_name: str
    date_of_birth: Optional[date] = None
    gender: Optional[Gender] = None
    relationship_to_consumer: Optional[str] = None
    blood_group: Optional[str] = None
    medical_conditions: Optional[List[str]] = None
    allergies: Optional[List[str]] = None
    current_medications: Optional[List[Dict[str, Any]]] = None
    abha_id: Optional[str] = None
    is_minor: bool = False
    notes: Optional[str] = None


class PatientOut(ORMModel):
    id: UUID
    consumer_id: UUID
    full_name: str
    date_of_birth: Optional[date] = None
    gender: Optional[Gender] = None
    relationship_to_consumer: Optional[str] = None
    blood_group: Optional[str] = None
    medical_conditions: Optional[List[str]] = None
    allergies: Optional[List[str]] = None
    current_medications: Optional[List[Dict[str, Any]]] = None
    abha_id: Optional[str] = None
    is_minor: bool
    notes: Optional[str] = None
    created_at: datetime


class FamilyMemberCreate(BaseModel):
    full_name: str
    phone_e164: str
    relationship: Optional[str] = None
    can_book: bool = False
    can_receive_updates: bool = True
    is_emergency_contact: bool = False


class FamilyMemberOut(ORMModel):
    id: UUID
    consumer_id: UUID
    full_name: str
    phone_e164: str
    relationship: Optional[str] = None
    can_book: bool
    can_receive_updates: bool
    is_emergency_contact: bool


# ----- WORKER -----
class WorkerProfileUpdate(BaseModel):
    gender: Optional[Gender] = None
    date_of_birth: Optional[date] = None
    bio: Optional[str] = None
    years_of_experience: Optional[int] = None
    languages_spoken: Optional[List[str]] = None
    specialisations: Optional[List[str]] = None
    registration_no: Optional[str] = None
    registration_authority: Optional[str] = None
    registration_valid_until: Optional[date] = None
    base_city: Optional[str] = None
    service_radius_km: Optional[int] = None
    home_latitude: Optional[Decimal] = None
    home_longitude: Optional[Decimal] = None


class WorkerProfileOut(ORMModel):
    id: UUID
    user_id: UUID
    tier: WorkerTier
    gender: Optional[Gender] = None
    onboarding_status: WorkerOnboardingStatus
    availability: WorkerAvailability
    bio: Optional[str] = None
    years_of_experience: int
    languages_spoken: Optional[List[str]] = None
    specialisations: Optional[List[str]] = None
    registration_no: Optional[str] = None
    registration_authority: Optional[str] = None
    registration_valid_until: Optional[date] = None
    base_city: Optional[str] = None
    service_radius_km: int
    rating_average: Decimal
    rating_count: int
    completed_visits_count: int
    kit_complete: bool


class WorkerPublicOut(BaseModel):
    """Worker info exposed to consumers during search."""
    id: UUID
    full_name: Optional[str] = None
    avatar_url: Optional[str] = None
    tier: WorkerTier
    gender: Optional[Gender] = None
    bio: Optional[str] = None
    years_of_experience: int
    languages_spoken: Optional[List[str]] = None
    specialisations: Optional[List[str]] = None
    rating_average: Decimal
    rating_count: int
    completed_visits_count: int
    availability: WorkerAvailability
    base_city: Optional[str] = None


class WorkerSearchQuery(BaseModel):
    city: Optional[str] = None
    service_code: Optional[str] = None
    min_tier: Optional[WorkerTier] = None
    gender: Optional[Gender] = None
    language: Optional[str] = None
    specialisation: Optional[str] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    radius_km: int = 10
    available_only: bool = False
    page: int = 1
    page_size: int = 20


class AvailabilityToggleRequest(BaseModel):
    availability: WorkerAvailability


class BankDetailsUpdate(BaseModel):
    bank_account_holder: str
    bank_account_number: str
    bank_ifsc: str


# ----- SERVICE / PACKAGE CATALOG -----
class ServiceOut(ORMModel):
    id: UUID
    service_code: str
    name: str
    description: Optional[str] = None
    category: ServiceCategory
    min_tier: WorkerTier
    duration_minutes: int
    base_price: Decimal
    max_price: Optional[Decimal] = None
    commission_pct: Decimal
    urgent_surge_pct: int
    requires_prescription: bool
    billing_trigger: BillingTrigger
    insurance_covered: bool
    icon: Optional[str] = None
    is_active: bool


class CarePackageOut(ORMModel):
    id: UUID
    package_code: str
    name: str
    tagline: Optional[str] = None
    description: Optional[str] = None
    target_condition: Optional[str] = None
    min_tier: WorkerTier
    visit_frequency: Optional[str] = None
    visits_per_cycle: Optional[int] = None
    cycle_duration_days: Optional[int] = None
    shift_hours: Optional[int] = None
    package_price: Optional[Decimal] = None
    per_visit_price: Optional[Decimal] = None
    subsidy_eligible: bool
    insurance_covered: bool
    available_cities: Optional[List[str]] = None
    is_active: bool


# ----- BOOKINGS -----
class AddressSnapshot(BaseModel):
    line1: str
    line2: Optional[str] = None
    city: str
    state: str
    pincode: str
    landmark: Optional[str] = None


class BookingCreate(BaseModel):
    patient_id: UUID
    service_id: Optional[UUID] = None
    package_id: Optional[UUID] = None
    booking_type: BookingType = BookingType.one_time
    scheduled_date: date
    scheduled_start_time: time
    is_urgent: bool = False
    # Either reference a saved address (preferred) OR pass inline address+coords.
    address_id: Optional[UUID] = None
    address: Optional[AddressSnapshot] = None
    latitude: Optional[Decimal] = None
    longitude: Optional[Decimal] = None
    special_instructions: Optional[str] = None
    preferred_worker_id: Optional[UUID] = None


class BookingOut(ORMModel):
    id: UUID
    booking_ref: str
    consumer_id: UUID
    patient_id: UUID
    booking_type: BookingType
    service_id: Optional[UUID] = None
    package_id: Optional[UUID] = None
    worker_id: Optional[UUID] = None
    status: BookingStatus
    scheduled_date: date
    scheduled_start_time: time
    scheduled_duration_minutes: int
    is_urgent: bool
    address_snapshot: dict
    latitude: Decimal
    longitude: Decimal
    base_amount: Decimal
    surge_amount: Decimal
    subsidy_amount: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    payment_status: PaymentStatus
    razorpay_order_id: Optional[str] = None
    special_instructions: Optional[str] = None
    cancellation_reason: Optional[str] = None
    cancelled_at: Optional[datetime] = None
    accepted_at: Optional[datetime] = None
    rematch_count: int
    created_at: datetime
    # Patch 3 — proximity dispatch (optional, populated when context allows).
    assignment_wave: Optional[int] = None
    assignment_escalated_at: Optional[datetime] = None
    distance_km: Optional[float] = None
    patient_name: Optional[str] = None
    service_name: Optional[str] = None


class BookingCancelRequest(BaseModel):
    reason: str


# ----- VISIT -----
class CheckInRequest(BaseModel):
    latitude: Decimal
    longitude: Decimal


class CheckOutRequest(BaseModel):
    latitude: Decimal
    longitude: Decimal
    family_summary: Optional[str] = None
    care_notes: Optional[str] = None


class VitalSignsSubmit(BaseModel):
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse: Optional[int] = None
    spo2: Optional[int] = None
    temperature_f: Optional[Decimal] = None
    respiratory_rate: Optional[int] = None
    blood_sugar_fasting: Optional[Decimal] = None
    blood_sugar_random: Optional[Decimal] = None
    weight_kg: Optional[Decimal] = None
    pain_score: Optional[int] = None
    gcs_score: Optional[int] = None
    measurement_device: Optional[str] = None
    recorded_at: Optional[datetime] = None
    is_offline_submitted: bool = False


class VitalSignsOut(ORMModel):
    id: UUID
    visit_record_id: UUID
    patient_id: UUID
    bp_systolic: Optional[int] = None
    bp_diastolic: Optional[int] = None
    pulse: Optional[int] = None
    spo2: Optional[int] = None
    temperature_f: Optional[Decimal] = None
    respiratory_rate: Optional[int] = None
    blood_sugar_fasting: Optional[Decimal] = None
    blood_sugar_random: Optional[Decimal] = None
    weight_kg: Optional[Decimal] = None
    pain_score: Optional[int] = None
    gcs_score: Optional[int] = None
    abnormal_flags: Optional[List[str]] = None
    escalation_triggered: bool
    escalation_level: EscalationLevel
    recorded_at: datetime


class MedicationSubmit(BaseModel):
    drug_name: str
    drug_generic_name: Optional[str] = None
    drug_class: Optional[str] = None
    dose_amount: str
    dose_unit: Optional[str] = None
    route: Optional[MedicationRoute] = None
    site: Optional[str] = None
    prescription_id: Optional[UUID] = None
    allergy_check_done: bool = False
    allergy_confirmed_clear: bool = False
    patient_identified: bool = False
    expiry_checked: bool = False
    administered_at: datetime
    patient_response: Optional[str] = None
    adverse_reaction: bool = False
    adverse_reaction_notes: Optional[str] = None
    batch_number: Optional[str] = None
    manufacturer: Optional[str] = None
    is_offline_submitted: bool = False


class ChecklistSubmit(BaseModel):
    responses: Dict[str, Any]
    is_offline_submitted: bool = False


class VisitRecordOut(ORMModel):
    id: UUID
    booking_id: UUID
    worker_id: UUID
    patient_id: UUID
    status: VisitStatus
    en_route_at: Optional[datetime] = None
    arrived_at: Optional[datetime] = None
    check_in_at: Optional[datetime] = None
    check_out_at: Optional[datetime] = None
    actual_duration_minutes: Optional[int] = None
    checklist_responses: Optional[dict] = None
    documentation_responses: Optional[dict] = None
    documentation_complete: bool
    family_summary: Optional[str] = None
    care_notes: Optional[str] = None
    photo_urls: Optional[List[str]] = None
    rating_by_consumer: Optional[int] = None
    rating_comment: Optional[str] = None
    escalation_triggered: bool
    created_at: datetime


class RatingSubmit(BaseModel):
    rating: int = Field(ge=1, le=5)
    comment: Optional[str] = None


# ----- ESCALATION -----
class EscalationCreateRequest(BaseModel):
    level: EscalationLevel = EscalationLevel.watch
    trigger_type: str = "manual"
    notes: str
    trigger_details: Optional[Dict[str, Any]] = None


class EscalationResolveRequest(BaseModel):
    resolution_notes: str

class EscalationAssignRequest(BaseModel):
    assigned_to: UUID

class EscalationNoteRequest(BaseModel):
    note: str = Field(min_length=1)

class EscalationSummaryOut(BaseModel):
    total: int
    open: int
    acknowledged: int
    investigating: int
    resolved: int
    emergency: int
    contact_doctor: int
    sla_breached: int
    unassigned: int
class EscalationOut(ORMModel):
    id: UUID
    booking_id: UUID
    visit_record_id: Optional[UUID] = None
    worker_id: UUID
    patient_id: UUID
    level: EscalationLevel
    status: EscalationStatus
    trigger_type: str
    trigger_details: Optional[dict] = None
    notes: Optional[str] = None
    notified_parties: Optional[List[str]] = None
    sla_minutes: Optional[int] = None
    sla_breach_at: Optional[datetime] = None
    auto_call_112: bool
    acknowledged_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    resolution_notes: Optional[str] = None
    # Patch 6 — Support dashboard
    assigned_to: Optional[UUID] = None
    assigned_at: Optional[datetime] = None
    internal_notes: Optional[str] = None
    priority: str = "normal"
    created_at: datetime

# ----- CONSENT -----
class ConsentCreate(BaseModel):
    patient_id: UUID
    booking_id: Optional[UUID] = None
    consent_type: ConsentType
    capture_method: ConsentCaptureMethod = ConsentCaptureMethod.digital_checkbox
    consented_by_name: Optional[str] = None
    relationship_to_patient: Optional[str] = None
    consent_text_version: Optional[str] = None
    consent_text_hash: Optional[str] = None
    expires_at: Optional[datetime] = None
    is_offline_captured: bool = False


# ----- PAYMENTS -----
class PaymentOrderRequest(BaseModel):
    booking_id: UUID


class PaymentOrderResponse(BaseModel):
    razorpay_order_id: str
    razorpay_key_id: str
    amount: int  # in paise
    currency: str = "INR"
    booking_id: UUID


class PaymentVerifyRequest(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    booking_id: UUID


# ----- TRACKING -----
class LocationUpdate(BaseModel):
    latitude: Decimal
    longitude: Decimal
    accuracy_metres: Optional[int] = None
    booking_id: Optional[UUID] = None
    is_offline: bool = False


# Patch 3 — Worker current-location ping (used for proximity dispatch).
class WorkerLocationUpdateRequest(BaseModel):
    latitude: Decimal
    longitude: Decimal
    accuracy: Optional[int] = None
    captured_at: Optional[datetime] = None


# ----- OFFLINE SYNC -----
class OfflineSyncItem(BaseModel):
    device_id: str
    booking_id: Optional[UUID] = None
    record_type: OfflineRecordType
    local_id: str
    payload: Dict[str, Any]
    locally_recorded_at: datetime


class OfflineSyncBatch(BaseModel):
    items: List[OfflineSyncItem]


class OfflineSyncResult(BaseModel):
    local_id: str
    sync_status: str
    server_record_id: Optional[UUID] = None
    error: Optional[str] = None


# ----- ABHA & CARE NOTES -----
class CareNoteCreate(BaseModel):
    patient_id: UUID
    booking_id: Optional[UUID] = None
    title: Optional[str] = None
    content: str
    note_type: str = "general"
    visible_to_family: bool = True
    visible_to_worker: bool = True


class CareNoteOut(ORMModel):
    id: UUID
    patient_id: UUID
    booking_id: Optional[UUID] = None
    visit_record_id: Optional[UUID] = None
    author_id: UUID
    author_role: str
    title: Optional[str] = None
    content: str
    note_type: str
    visible_to_family: bool
    visible_to_worker: bool
    created_at: datetime


class AbhaRecordCreate(BaseModel):
    patient_id: UUID
    record_type: str
    title: str
    cloudinary_url: Optional[str] = None
    cloudinary_public_id: Optional[str] = None
    abha_health_id: Optional[str] = None
    issued_on: Optional[date] = None
    issued_by: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None


class AbhaRecordOut(ORMModel):
    id: UUID
    patient_id: UUID
    record_type: str
    title: str
    cloudinary_url: Optional[str] = None
    abha_health_id: Optional[str] = None
    issued_on: Optional[date] = None
    issued_by: Optional[str] = None
    metadata_json: Optional[Dict[str, Any]] = None
    is_synced_with_abha: bool
    created_at: datetime


# ----- NOTIFICATIONS -----
class NotificationOut(ORMModel):
    id: UUID
    recipient_id: UUID
    channel: NotificationChannel
    template_code: Optional[str] = None
    title: Optional[str] = None
    body: Optional[str] = None
    payload: Optional[dict] = None
    status: str
    read_at: Optional[datetime] = None
    created_at: datetime


# ----- GENERIC -----
class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int


class Paginated(BaseModel):
    items: List[Any]
    meta: PageMeta