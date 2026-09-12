"""Single source of truth for Provider-Type-driven dynamic onboarding.

Previously this lived as three separate, hand-maintained copies:
  - app/api/v1/workers.py::REQUIRED_DOCUMENTS_BY_TYPE (powers /me/onboarding
    doc catalogue — the one the mobile/website onboarding screens actually render)
  - app/services/worker_approval.py::REQUIRED_DOCUMENTS_BY_WORKER_TYPE (enforced
    at admin approval time)
  - app/api/v1/admin.py::REQUIRED_DOCUMENTS_BY_WORKER_TYPE (declared, unused)

Three copies of the same table drift silently — the admin.py copy already had
drifted (unused/dead). This module is now the only place document
requirements per Provider Type are defined; the three files above import
from here.

Extending this dict is the correct way to add/change document requirements
for a provider type — nothing else needs to change for that.
"""
from app.models.enums import WorkerType

PROVIDER_TYPE_LABELS = {
    WorkerType.nurse: "Nurse",
    WorkerType.caregiver: "Caregiver",
    WorkerType.doctor: "Doctor",
    WorkerType.tele_doctor: "Tele-Doctor",
    WorkerType.physical_doctor: "Physical Doctor",
    WorkerType.dentist: "Dentist",
    WorkerType.physiotherapist: "Physiotherapist",
    WorkerType.mother_baby_caregiver: "Mother & Baby Caregiver",
}

# Provider types that require a formal degree/registration-based onboarding
# path (Qualification -> Registration -> Registration Number -> ...).
# Caregiver and Mother & Baby Caregiver are deliberately absent — no degree
# is ever required of them, per spec.
LICENSED_PROVIDER_TYPES = {
    WorkerType.nurse,
    WorkerType.doctor,
    WorkerType.tele_doctor,
    WorkerType.physical_doctor,
    WorkerType.dentist,
    WorkerType.physiotherapist,
}

# ---------------------------------------------------------------------------
# Tele vs Physical capability.
#
# The single predicate the rest of the system branches on when deciding
# whether a provider gets tele-consultation features (waiting queue, call
# button, video room) or physical-visit features (visible patient, on-site
# reports, procedures).
#
# `doctor` — the pre-existing generic type — appears in BOTH sets. Every
# doctor onboarded before Tele/Physical existed keeps the behaviour they
# have today; nothing regresses. New doctors pick a specific type and get
# only the features that apply to them, which is what stops a Physical
# Doctor being shown tele-consultation UI.
# ---------------------------------------------------------------------------
TELE_CAPABLE_PROVIDER_TYPES = {
    WorkerType.doctor,
    WorkerType.tele_doctor,
}

PHYSICAL_CAPABLE_PROVIDER_TYPES = {
    WorkerType.doctor,
    WorkerType.physical_doctor,
    WorkerType.nurse,
    WorkerType.caregiver,
    WorkerType.dentist,
    WorkerType.physiotherapist,
    WorkerType.mother_baby_caregiver,
}

# Every provider type that practises medicine as a doctor, in either mode.
DOCTOR_PROVIDER_TYPES = {
    WorkerType.doctor,
    WorkerType.tele_doctor,
    WorkerType.physical_doctor,
}


def is_tele_capable(worker_type: WorkerType) -> bool:
    """True if this provider type may run tele-consultations."""
    return worker_type in TELE_CAPABLE_PROVIDER_TYPES


def is_physical_capable(worker_type: WorkerType) -> bool:
    """True if this provider type attends in-person visits."""
    return worker_type in PHYSICAL_CAPABLE_PROVIDER_TYPES


def is_doctor(worker_type: WorkerType) -> bool:
    return worker_type in DOCTOR_PROVIDER_TYPES

# Documents that block onboarding submission until uploaded + verified.
REQUIRED_DOCUMENTS_BY_PROVIDER_TYPE = {
    WorkerType.nurse: {"aadhaar", "nursing_license", "degree_certificate", "police_verification"},
    WorkerType.doctor: {"aadhaar", "medical_registration", "degree_certificate", "police_verification"},
    # Both doctor modes carry identical credentialling requirements — the
    # mode changes how they deliver care, not what licenses them to.
    WorkerType.tele_doctor: {"aadhaar", "medical_registration", "degree_certificate", "police_verification"},
    WorkerType.physical_doctor: {"aadhaar", "medical_registration", "degree_certificate", "police_verification"},
    WorkerType.dentist: {"aadhaar", "dental_registration", "degree_certificate", "police_verification"},
    WorkerType.physiotherapist: {"aadhaar", "degree_certificate", "police_verification"},
    WorkerType.caregiver: {"aadhaar", "police_verification"},
    WorkerType.mother_baby_caregiver: {"aadhaar", "police_verification"},
}

# Optional / supporting documents — strengthen the profile or unlock more
# packages, never block submission for review.
OPTIONAL_DOCUMENTS_BY_PROVIDER_TYPE = {
    WorkerType.nurse: {"experience_certificate", "specialization_certificate"},
    WorkerType.doctor: {"experience_certificate", "specialization_certificate"},
    WorkerType.tele_doctor: {"experience_certificate", "specialization_certificate"},
    WorkerType.physical_doctor: {"experience_certificate", "specialization_certificate"},
    WorkerType.dentist: {"experience_certificate", "specialization_certificate"},
    WorkerType.physiotherapist: {"physio_registration", "experience_certificate", "specialization_certificate"},
    WorkerType.caregiver: {"caregiver_training_certificate", "first_aid_certificate", "degree_certificate", "experience_certificate"},
    WorkerType.mother_baby_caregiver: {"caregiver_training_certificate", "first_aid_certificate", "mother_baby_training_certificate", "experience_certificate"},
}

DOCUMENT_LABELS = {
    "aadhaar": "Aadhaar Card",
    "nursing_license": "Nursing Registration / License",
    "medical_registration": "Medical Council Registration",
    "dental_registration": "Dental Council Registration (BDS/MDS)",
    "physio_registration": "Physiotherapy Council Registration",
    "degree_certificate": "Degree / Education Certificate",
    "police_verification": "Police / Background Verification",
    "experience_certificate": "Experience Certificate",
    "specialization_certificate": "Specialization Certificate",
    "caregiver_training_certificate": "Caregiver Training Certificate",
    "first_aid_certificate": "First Aid / Emergency Training Certificate",
    "mother_baby_training_certificate": "Mother & Baby Care Training Certificate",
}


def required_docs(worker_type: WorkerType) -> set:
    return REQUIRED_DOCUMENTS_BY_PROVIDER_TYPE.get(worker_type, REQUIRED_DOCUMENTS_BY_PROVIDER_TYPE[WorkerType.nurse])


def optional_docs(worker_type: WorkerType) -> set:
    return OPTIONAL_DOCUMENTS_BY_PROVIDER_TYPE.get(worker_type, set())


# ---------------------------------------------------------------------------
# Caregiver / Mother & Baby Caregiver specializations.
#
# This is a DISPLAY/REFERENCE catalogue only — the onboarding UI and admin
# screens use it to render grouped checkboxes. It does NOT grant
# qualification by itself (selecting a specialization != qualified, per
# spec). Actual Interested -> Trained -> Assessed -> Qualified -> Opted-in ->
# Eligible tracking rides entirely on the EXISTING WorkerServiceQualification
# / WorkerServicePreference engine (app/services/qualification.py) by
# creating one ServiceCatalogue row per specialization (service_code below)
# tagged allowed_provider_types=["caregiver","mother_baby_caregiver"]. That
# reuses the three-gate qualification model instead of building a second,
# parallel qualification system for specializations.
# ---------------------------------------------------------------------------
CAREGIVER_SPECIALIZATION_GROUPS = {
    "elder_care": {
        "label": "Elder Care",
        "items": {
            "elder_companionship": "Elder Companionship",
            "elder_assistance": "Elder Assistance",
            "meal_water_reminders": "Meal / Water Reminders",
            "medicine_reminders": "Medicine Reminders",
            "activity_conversation": "Activity / Conversation",
            "overnight_presence": "Overnight Presence",
        },
    },
    "personal_care": {
        "label": "Personal Care",
        "items": {
            "bathing": "Bathing",
            "grooming": "Grooming",
            "toileting": "Toileting",
            "diaper_change": "Diaper Change",
            "feeding": "Feeding",
            "mobility_assistance": "Mobility Assistance",
        },
    },
    "accompaniment": {
        "label": "Accompaniment",
        "items": {
            "doctor_visit": "Doctor Visit",
            "hospital_visit": "Hospital Visit",
            "diagnostic_centre": "Diagnostic Centre",
            "dialysis": "Dialysis",
            "social_outing": "Social Outing",
            "religious_outing": "Religious Outing",
        },
    },
    "mother_and_baby": {
        "label": "Mother & Baby",
        "items": {
            "baby_massage": "Baby Massage",
            "baby_bath": "Baby Bath",
            "mother_massage": "Mother Massage",
            "newborn_assistance": "Newborn Assistance",
            "jaapa_support": "Jaapa Support",
        },
    },
    "other": {
        "label": "Other",
        "items": {
            "bedbound_assistance": "Bedbound Assistance",
            "hospital_companion": "Hospital Companion",
            "post_hospital_support": "Post-Hospital Support",
        },
    },
}


def specialization_service_code(specialization_key: str) -> str:
    """The convention used to map a specialization to its ServiceCatalogue
    row: prefix + key, e.g. 'baby_massage' -> 'spec_baby_massage'. Admin
    creates one ServiceCatalogue row per specialization with this code,
    category left as whatever fits the existing ServiceCategory enum
    (typically micro_visit), and allowed_provider_types set to the
    provider types that may qualify for it."""
    return f"spec_{specialization_key}"