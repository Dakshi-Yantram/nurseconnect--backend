"""Dynamic, role-driven form schemas.

The problem this replaces: one shared onboarding/profile form containing
every field any provider type might need, with the irrelevant ones either
shown to everyone or hidden by a growing pile of `if worker_type == ...`
conditionals spread across the apps. Adding a provider type meant editing
every client.

Here the server describes the form and the clients render whatever they are
given. Adding a field, or a provider type, is a change in this one file.

The schema is assembled from composable *sections* rather than written out
per role, so shared fields (identity, contact, bank) are defined once and
reused. That is what keeps Nurse, Tele-Doctor and Physical Doctor
independent without duplicating their common 80%.

Document requirements are NOT duplicated here — they are read from
app/core/provider_types.py, which is already the single source of truth and
is what the approval workflow enforces. A form that disagreed with the
approval rules would be worse than no form at all.
"""
from __future__ import annotations

from typing import Any, Optional

from app.core.provider_types import (
    DOCTOR_PROVIDER_TYPES,
    LICENSED_PROVIDER_TYPES,
    PROVIDER_TYPE_LABELS,
    is_physical_capable,
    is_tele_capable,
    optional_docs,
    required_docs,
)
from app.models.enums import WorkerType

# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def _field(
    key: str,
    label: str,
    ftype: str = "text",
    *,
    required: bool = False,
    placeholder: Optional[str] = None,
    help_text: Optional[str] = None,
    options: Optional[list] = None,
    max_length: Optional[int] = None,
    pattern: Optional[str] = None,
) -> dict:
    f: dict[str, Any] = {"key": key, "label": label, "type": ftype, "required": required}
    if placeholder:
        f["placeholder"] = placeholder
    if help_text:
        f["help_text"] = help_text
    if options is not None:
        f["options"] = options
    if max_length is not None:
        f["max_length"] = max_length
    if pattern:
        f["pattern"] = pattern
    return f


def _section(key: str, title: str, fields: list, description: Optional[str] = None) -> dict:
    s: dict[str, Any] = {"key": key, "title": title, "fields": fields}
    if description:
        s["description"] = description
    return s


# ---------------------------------------------------------------------------
# Reusable sections — defined once, composed per provider type
# ---------------------------------------------------------------------------
def _identity_section() -> dict:
    return _section(
        "identity",
        "Personal details",
        [
            _field("full_name", "Full name", required=True, max_length=255),
            _field("date_of_birth", "Date of birth", "date", required=True),
            _field(
                "gender",
                "Gender",
                "select",
                required=True,
                options=[
                    {"value": "male", "label": "Male"},
                    {"value": "female", "label": "Female"},
                    {"value": "other", "label": "Other"},
                ],
                help_text="Some patients may request a provider of a specific gender.",
            ),
            _field("languages", "Languages spoken", "multiselect", required=True,
                   options=[{"value": v, "label": l} for v, l in (
                       ("hindi", "Hindi"), ("english", "English"), ("bengali", "Bengali"),
                       ("tamil", "Tamil"), ("telugu", "Telugu"), ("marathi", "Marathi"),
                       ("kannada", "Kannada"), ("malayalam", "Malayalam"), ("gujarati", "Gujarati"),
                       ("punjabi", "Punjabi"), ("urdu", "Urdu"),
                   )]),
        ],
    )


def _contact_section(*, needs_address: bool) -> dict:
    fields = [
        _field("phone_e164", "Mobile number", "phone", required=True,
               pattern=r"^\+?[1-9]\d{7,14}$"),
        _field("email", "Email address", "email", required=True),
    ]
    if needs_address:
        fields += [
            _field("address_line1", "Address", required=True),
            _field("city", "City", required=True),
            _field("state", "State", required=True),
            _field("pincode", "PIN code", required=True, pattern=r"^\d{6}$"),
            _field("service_radius_km", "How far will you travel? (km)", "number",
                   required=True,
                   help_text="Used to match you with nearby visits."),
        ]
    return _section(
        "contact",
        "Contact details" if not needs_address else "Contact & service area",
        fields,
    )


def _license_section(worker_type: WorkerType) -> dict:
    from app.core.contracts import REGISTRATION_LABEL_BY_TYPE

    label = REGISTRATION_LABEL_BY_TYPE.get(worker_type, "Registration number")
    return _section(
        "license",
        "Professional registration",
        [
            _field("registration_number", label, required=True),
            _field("registration_council", "Issuing council", required=True),
            _field("registration_valid_till", "Valid until", "date", required=True),
            _field("qualification_name", "Highest qualification", required=True,
                   placeholder="e.g. MBBS, MD, GNM, B.Sc Nursing"),
            _field("years_experience", "Years of experience", "number", required=True),
        ],
        description="We verify this against the council register before approval.",
    )


def _bank_section() -> dict:
    """Payout details. Required for anyone we pay."""
    return _section(
        "bank",
        "Bank details for payouts",
        [
            _field("account_holder_name", "Account holder name", required=True,
                   help_text="Must match your bank records exactly, or payouts will fail."),
            _field("account_number", "Account number", required=True, pattern=r"^\d{6,20}$"),
            _field("ifsc_code", "IFSC code", required=True, pattern=r"^[A-Z]{4}0[A-Z0-9]{6}$"),
            _field("bank_name", "Bank name", required=True),
            _field("pan_number", "PAN", required=True, pattern=r"^[A-Z]{5}\d{4}[A-Z]$",
                   help_text="Required for TDS on your earnings."),
            _field("upi_id", "UPI ID (optional)", placeholder="name@bank"),
        ],
        description="Paid out weekly. We never share these details with patients.",
    )


def _tele_section() -> dict:
    """Only for providers who actually run tele-consultations."""
    return _section(
        "tele_practice",
        "Tele-consultation setup",
        [
            _field("consultation_fee", "Your consultation fee (₹)", "number", required=True),
            _field("avg_consultation_minutes", "Typical consultation length (minutes)",
                   "number", required=True),
            _field("tele_availability_hours", "Hours you take consultations", "text",
                   required=True, placeholder="e.g. 9am–1pm, 5pm–9pm"),
            _field("has_quiet_space", "I have a private, quiet space for video calls",
                   "checkbox", required=True),
            _field("digital_signature_ready",
                   "I can upload a digital signature for e-prescriptions",
                   "checkbox", required=True),
        ],
        description="These apply to video consultations only.",
    )


def _physical_visit_section(worker_type: WorkerType) -> dict:
    """Only for providers who attend in person."""
    fields = [
        _field("visit_fee", "Your per-visit fee (₹)", "number", required=True),
        _field("has_own_transport", "I have my own transport", "checkbox"),
        _field("available_days", "Days available", "multiselect", required=True,
               options=[{"value": d.lower(), "label": d} for d in
                        ("Monday", "Tuesday", "Wednesday", "Thursday",
                         "Friday", "Saturday", "Sunday")]),
        _field("shift_preference", "Preferred shift", "select", required=True,
               options=[
                   {"value": "morning", "label": "Morning"},
                   {"value": "afternoon", "label": "Afternoon"},
                   {"value": "evening", "label": "Evening"},
                   {"value": "night", "label": "Night"},
                   {"value": "any", "label": "Any"},
               ]),
    ]
    if worker_type in DOCTOR_PROVIDER_TYPES:
        fields.append(
            _field("procedures_offered", "Procedures you perform on visit", "multiselect",
                   options=[{"value": v, "label": l} for v, l in (
                       ("wound_dressing", "Wound dressing"),
                       ("suturing", "Suturing"),
                       ("nail_procedure", "Nail procedures (ingrown / avulsion)"),
                       ("abscess_drainage", "Abscess drainage"),
                       ("catheterisation", "Catheterisation"),
                       ("injection", "Injections / IV"),
                   )],
                   help_text="Determines which physical packages you are offered.")
        )
    return _section(
        "physical_visit",
        "In-person visits",
        fields,
        description="These apply to visits you attend in person.",
    )


def _documents_section(worker_type: WorkerType) -> dict:
    """Built from provider_types.py so the form and the approval rules
    can never disagree about what is required."""
    req = sorted(required_docs(worker_type))
    opt = sorted(optional_docs(worker_type))
    fields = [
        _field(f"doc_{code}", code.replace("_", " ").title(), "file", required=True)
        for code in req
    ] + [
        _field(f"doc_{code}", code.replace("_", " ").title() + " (optional)", "file")
        for code in opt
    ]
    return _section(
        "documents",
        "Documents",
        fields,
        description="Required documents must be verified before your account is approved.",
    )


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------
def onboarding_form_schema(worker_type: WorkerType) -> dict:
    """The onboarding form for one provider type.

    Composed from the sections above based on what the type actually does,
    which is why a Physical Doctor is never shown tele-consultation fields
    and a Tele-Doctor is never asked how far they will travel.
    """
    tele = is_tele_capable(worker_type)
    physical = is_physical_capable(worker_type)

    sections = [
        _identity_section(),
        # A purely tele provider never travels to a patient, so the service
        # area block is pointless for them.
        _contact_section(needs_address=physical),
    ]
    if worker_type in LICENSED_PROVIDER_TYPES:
        sections.append(_license_section(worker_type))
    if tele:
        sections.append(_tele_section())
    if physical:
        sections.append(_physical_visit_section(worker_type))
    sections.append(_bank_section())
    sections.append(_documents_section(worker_type))

    return {
        "form_key": f"onboarding.{worker_type.value}",
        "provider_type": worker_type.value,
        "provider_label": PROVIDER_TYPE_LABELS.get(worker_type, worker_type.value),
        "title": f"{PROVIDER_TYPE_LABELS.get(worker_type, worker_type.value)} registration",
        "capabilities": {"tele": tele, "physical": physical},
        "sections": sections,
    }


def profile_form_schema(worker_type: WorkerType) -> dict:
    """The editable profile form — onboarding minus the one-time sections.

    Identity and documents are verified at approval and are not freely
    editable afterwards, so they are excluded rather than shown read-only
    and silently ignored on save.
    """
    full = onboarding_form_schema(worker_type)
    editable = {"contact", "tele_practice", "physical_visit", "bank"}
    return {
        **full,
        "form_key": f"profile.{worker_type.value}",
        "title": f"{full['provider_label']} profile",
        "sections": [s for s in full["sections"] if s["key"] in editable],
    }
