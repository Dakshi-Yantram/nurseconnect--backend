"""Single source of truth for provider onboarding contracts.

Two-stage flow (per Yantram Medtech's legal write-up):
  Stage 1 — In-App Clickwrap Agreement. Shown at signup, accepted via
    checkbox + OTP. Free, no stamp paper. Gates initial app access.
  Stage 2 — Master Independent Contractor Agreement on state e-Stamp paper.
    Shown after the FIRST booking is completed, executed via Aadhaar eSign.
    Gates booking #2 onwards. Carries an onboarding fee deducted from the
    first payout.

Both stages are dynamically rendered per provider type:
  - The registration-number line/label changes: "State Nursing Council Reg
    No." for nurses, "Medical Council Registration No." for doctors, "State
    Dental Council Reg No." for dentists, "State Physiotherapy Council Reg
    No." for physiotherapists.
  - Caregivers / Mother & Baby Caregivers have NO degree/license, so the
    registration line is dropped entirely (they hold no registration
    number by definition — see app/core/provider_types.py).
  - "Nurse"/"Contractor" role noun switches to the provider's actual type
    label (Doctor, Dentist, Physiotherapist, Caregiver, ...).

Extending wording: edit CONTRACT_TEMPLATES below. Nothing else needs to
change — app/api/v1/contracts.py always renders from here, and every
acceptance freezes the exact rendered text onto WorkerAgreement.rendered_text
for legal audit (so future edits here never retroactively alter what a
worker already agreed to).
"""
from string import Template

from app.core.provider_types import LICENSED_PROVIDER_TYPES, PROVIDER_TYPE_LABELS
from app.models.enums import WorkerType

TEMPLATE_VERSION = "v1"

# Registration-authority label per provider type. Only present for
# LICENSED_PROVIDER_TYPES — caregivers/mother_baby_caregiver never appear
# here, matching the "no license, ever" rule in provider_types.py.
REGISTRATION_LABEL_BY_TYPE = {
    WorkerType.nurse: "State Nursing Council Reg No.",
    WorkerType.doctor: "Medical Council Registration No.",
    WorkerType.dentist: "State Dental Council Reg No.",
    WorkerType.physiotherapist: "State Physiotherapy Council Reg No.",
}

_STAGE1_TEMPLATE = Template(
    """TERMS OF PLATFORM ENGAGEMENT & ONBOARDING AGREEMENT
This Platform Engagement Agreement ("Agreement") is an electronic contract executed under the Information Technology Act, 2000, between Yantram Medtech Private Limited ("Company/Platform") and the applicant healthcare provider $full_name ("Partner").

1. CONDITIONAL APP ACCESS
1.1 By clicking "I Accept" and validating via One-Time Password (OTP), the $role_label agrees to the preliminary onboarding terms to access the Platform app.
1.2 The $role_label acknowledges that initial registration does not constitute a permanent partnership or guaranteed gig allocation.

2. OFF-PLATFORM BYPASSING & CONTRACT VOIDABILITY
2.1 Zero Off-Platform Tolerance: The $role_label is strictly prohibited from soliciting, rendering healthcare services to, or accepting direct cash/online payments from any patient introduced via the Platform outside of the official app interface.
2.2 Immediate Voidability: Any engagement, service delivery, or agreement conducted outside the Platform interface shall render this engagement NULL AND VOID immediately, resulting in permanent app ban, loss of platform payout credits, and potential legal action for breach of trust.

3. VOIDANCE OF INSURANCE & COVERAGE BENEFITS
3.1 Coverage Conditionality: The Company may arrange third-party professional liability insurance, personal accidental insurance, or safety coverage for $role_label_plural.
3.2 Absolute Exclusion: All insurance benefits, third-party liability protections, legal defense assistance, and personal accidental covers provided directly or indirectly by the Company are STRICTLY VALID ONLY FOR VISITS ACCEPTED, TRACKED, AND PAID THROUGH THE PLATFORM APP.
3.3 If the $role_label services a patient off-platform or takes a booking offline, ALL INSURANCE AND INDEMNITY COVERAGE SHALL BE AUTOMATICALLY VOIDED for that visit and any subsequent incidents. The $role_label shall bear 100% personal, financial, and legal liability for medical negligence or accidents.

4. MANDATORY STAGE-2 EXECUTION
4.1 The $role_label agrees that upon accepting and successfully completing their FIRST patient booking, they shall be required to execute the formal Master Healthcare Contractor Agreement via Aadhaar eSign on a $state e-Stamp paper.
4.2 Access to accept subsequent bookings (Booking #2 onwards) will remain locked until Stage 2 execution is completed.
4.3 The $role_label authorizes the Platform to deduct an Onboarding Enablement Fee of ₹200 directly from the payout generated from their first completed booking to cover state stamp duties and processing costs.
"""
)

_STAGE2_TEMPLATE = Template(
    """MASTER INDEPENDENT CONTRACTOR AGREEMENT (State of $state)
THIS AGREEMENT is executed electronically on this $execution_day day of $execution_month, $execution_year ("Execution Date"), by and between:

1. PLATFORM: Yantram Medtech Pvt Ltd, having its office at HITEC City, Hyderabad, Telangana (hereinafter referred to as "Company");
AND
2. CONTRACTOR: $full_name, residing at $address$registration_line (hereinafter referred to as "Contractor").

1. SCOPE & PLATFORM INTEGRITY
1.1 The Contractor is engaged as an independent healthcare provider on a principal-to-principal basis to perform $service_description requested by users through the Company's mobile app.
1.2 Exclusivity of Platform Bookings: Every booking initiated via the Platform must be completed within the Platform ecosystem. Taking Platform-introduced patients off-line is a material breach.

2. ABSOLUTE VOIDANCE FOR OFF-PLATFORM OPERATIONS
2.1 If the Contractor is found to be operating outside the Platform with patients acquired via the app:
  - This Agreement shall stand INSTANTLY VOID at the option of the Company.
  - The Contractor's profile shall be permanently deactivated.
  - All pending balance payouts shall be forfeited towards liquidated damages.

3. INSURANCE & ACCIDENTAL COVERAGE EXCLUSIONS
3.1 The Company may facilitate Third-Party Professional Liability Insurance and Personal Accidental Coverage for the Contractor.
3.2 Operational Limitation: EVERY INSURANCE BENEFIT, PERSONAL ACCIDENTAL COVER, AND MEDICAL NEGLIGENCE INDEMNITY IS VALID ONLY AND EXCLUSIVELY WHILE THE CONTRACTOR IS ON AN ACTIVE, PLATFORM-TRACKED BOOKING (FROM APP CHECK-IN TO APP CHECK-OUT).
3.3 Any medical incident, injury, patient casualty, or third-party claim occurring during an offline/off-platform visit is 100% UNINSURED. The Contractor explicitly releases the Company from all liabilities and agrees to personal indemnification.

4. DEDUCTION OF ONBOARDING ENABLEMENT FEES
4.1 The Contractor explicitly agrees and authorizes the Company to deduct a one-time Onboarding & Verification Fee of ₹200 from the payout generated from their first completed booking to offset state stamp paper fees, background checks, and verification expenses.

5. GOVERNING LAW & JURISDICTION
5.1 This Agreement is governed by the laws of India and subject to the exclusive jurisdiction of the Courts at Hyderabad, Telangana.
"""
)

_SERVICE_DESCRIPTION_BY_TYPE = {
    WorkerType.nurse: "home care nursing visits",
    WorkerType.doctor: "home/tele consultations",
    WorkerType.dentist: "home dental consultations",
    WorkerType.physiotherapist: "home physiotherapy sessions",
    WorkerType.caregiver: "home caregiving visits",
    WorkerType.mother_baby_caregiver: "mother & baby home care visits",
}


def _role_label(worker_type: WorkerType) -> str:
    return PROVIDER_TYPE_LABELS.get(worker_type, "Provider")


def _registration_line(worker_type: WorkerType, registration_no: str | None, registration_authority: str | None) -> str:
    """Dynamic registration-number clause. Empty string for provider types
    that hold no registration/license (caregivers) — matches the frontend
    rule that the "Registration Number" field must not even be shown to
    non-degree care providers."""
    if worker_type not in LICENSED_PROVIDER_TYPES:
        return ""
    label = REGISTRATION_LABEL_BY_TYPE.get(worker_type, "Registration No.")
    authority = registration_authority or ""
    number = registration_no or "[TO BE VERIFIED]"
    authority_part = f" ({authority})" if authority else ""
    return f", holding {label}{authority_part} {number}"


def render_stage1(*, full_name: str, worker_type: WorkerType, state: str = "Telangana") -> str:
    role_label = _role_label(worker_type)
    return _STAGE1_TEMPLATE.substitute(
        full_name=full_name or "[Name pending]",
        role_label=role_label,
        role_label_plural=f"{role_label}s",
        state=state,
    )


def render_stage2(
    *,
    full_name: str,
    address: str,
    worker_type: WorkerType,
    registration_no: str | None,
    registration_authority: str | None,
    execution_date,
    state: str = "Telangana",
) -> str:
    return _STAGE2_TEMPLATE.substitute(
        state=state,
        execution_day=execution_date.day,
        execution_month=execution_date.strftime("%B"),
        execution_year=execution_date.year,
        full_name=full_name or "[Name pending]",
        address=address or "[Address pending]",
        registration_line=_registration_line(worker_type, registration_no, registration_authority),
        service_description=_SERVICE_DESCRIPTION_BY_TYPE.get(worker_type, "home care visits"),
    )
