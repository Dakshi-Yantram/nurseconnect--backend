"""
Seed runner — seeds services, care packages, and training modules.

Seeds are idempotent: re-running never creates duplicates.
"""
import asyncio
import sys
from decimal import Decimal

from app.core.database import AsyncSessionLocal, engine, Base
from app.models.models import (
    ServiceCatalogue, CarePackage, TrainingModule, AssessmentModule, Faq,
    ChecklistTemplate,
)
from app.models.enums import (
    ServiceCategory,
    WorkerTier,
    BillingTrigger,
    ContentStatus,
    GenderRestriction,
    VisitFrequency,
    ChecklistPhase,
)
from sqlalchemy import select


SERVICES = [
    dict(
        service_code="WOUND_DRESSING",
        name="Wound Dressing",
        description="Sterile wound cleaning and dressing change by a qualified nurse.",
        category=ServiceCategory.micro_visit,
        min_tier=WorkerTier.tier1,
        duration_minutes=30,
        base_price=Decimal("499"),
        commission_pct=Decimal("20"),
        billing_trigger=BillingTrigger.on_completion,
        insurance_covered=True,
        icon="bandage",
    ),
    dict(
        service_code="INJECTION_IM_IV",
        name="Injection (IM/IV)",
        description="Administration of prescribed intramuscular or intravenous injections.",
        category=ServiceCategory.micro_visit,
        min_tier=WorkerTier.tier2,
        duration_minutes=20,
        base_price=Decimal("349"),
        commission_pct=Decimal("20"),
        requires_prescription=True,
        billing_trigger=BillingTrigger.on_completion,
        insurance_covered=True,
        icon="syringe",
    ),
    dict(
        service_code="VITALS_CHECK",
        name="Vitals Monitoring",
        description="Blood pressure, pulse, SpO2, temperature and blood sugar check.",
        category=ServiceCategory.micro_visit,
        min_tier=WorkerTier.tier1,
        duration_minutes=20,
        base_price=Decimal("249"),
        commission_pct=Decimal("20"),
        billing_trigger=BillingTrigger.on_completion,
        insurance_covered=True,
        icon="activity",
    ),
    dict(
        service_code="POST_OP_CARE",
        name="Post-Operative Care Visit",
        description="Post-surgical monitoring, dressing checks, and recovery support.",
        category=ServiceCategory.micro_visit,
        min_tier=WorkerTier.tier3,
        duration_minutes=45,
        base_price=Decimal("799"),
        commission_pct=Decimal("22"),
        billing_trigger=BillingTrigger.on_completion,
        insurance_covered=True,
        icon="heart-pulse",
    ),
    dict(
        service_code="ELDERLY_DAY_SHIFT",
        name="Elderly Care — Day Shift",
        description="12-hour day shift nursing and companionship care for elderly patients.",
        category=ServiceCategory.shift,
        min_tier=WorkerTier.tier2,
        duration_minutes=720,
        base_price=Decimal("1899"),
        commission_pct=Decimal("18"),
        billing_trigger=BillingTrigger.on_checkin,
        insurance_covered=True,
        icon="sun",
    ),
    dict(
        service_code="ELDERLY_NIGHT_SHIFT",
        name="Elderly Care — Night Shift",
        description="12-hour overnight nursing and monitoring care for elderly patients.",
        category=ServiceCategory.shift,
        min_tier=WorkerTier.tier2,
        duration_minutes=720,
        base_price=Decimal("2099"),
        commission_pct=Decimal("18"),
        billing_trigger=BillingTrigger.on_checkin,
        insurance_covered=True,
        icon="moon",
    ),
    dict(
        service_code="LIVE_IN_CARE",
        name="Live-in Full-time Care",
        description="Round-the-clock live-in nursing care for high-dependency patients.",
        category=ServiceCategory.live_in,
        min_tier=WorkerTier.tier3,
        duration_minutes=1440,
        base_price=Decimal("3499"),
        commission_pct=Decimal("15"),
        billing_trigger=BillingTrigger.on_checkin,
        insurance_covered=True,
        icon="home",
    ),
    dict(
        service_code="PHYSIO_HOME_VISIT",
        name="Home Physiotherapy Visit",
        description="In-home physiotherapy session for mobility and rehabilitation.",
        category=ServiceCategory.micro_visit,
        min_tier=WorkerTier.tier3,
        duration_minutes=45,
        base_price=Decimal("899"),
        commission_pct=Decimal("22"),
        billing_trigger=BillingTrigger.on_completion,
        insurance_covered=False,
        icon="dumbbell",
    ),
]

PACKAGES = [
    dict(
        package_code="POST_OP_7D",
        name="Post Surgery Recovery — 7 Day",
        tagline="Daily wound care and vitals monitoring for the first week home",
        description="A 7-day daily-visit package covering wound dressing, vitals monitoring, "
                     "and recovery progress tracking after surgery.",
        target_condition="Post knee/hip replacement, post-operative recovery",
        min_tier=WorkerTier.tier3,
        gender_restriction=GenderRestriction.any,
        visit_frequency=VisitFrequency.daily,
        visits_per_cycle=7,
        cycle_duration_days=7,
        package_price=Decimal("8999"),
        per_visit_price=Decimal("1285"),
        subsidy_eligible=False,
        commission_pct=Decimal("20"),
        requires_prescription=True,
        insurance_covered=True,
    ),
    dict(
        package_code="ELDERLY_MONTHLY",
        name="Elderly Care — Monthly Plan",
        tagline="Daily wellness visits for elderly parents living alone",
        description="Daily 1-hour wellness check-ins including vitals, medication reminders, "
                     "and a family update report after every visit.",
        target_condition="General elderly wellness and monitoring",
        min_tier=WorkerTier.tier2,
        gender_restriction=GenderRestriction.any,
        visit_frequency=VisitFrequency.daily,
        visits_per_cycle=30,
        cycle_duration_days=30,
        package_price=Decimal("17999"),
        per_visit_price=Decimal("600"),
        subsidy_eligible=True,
        commission_pct=Decimal("18"),
        requires_prescription=False,
        insurance_covered=True,
    ),
    dict(
        package_code="DIABETES_CARE_14D",
        name="Diabetes Management — 14 Day",
        tagline="Alternate-day blood sugar monitoring and medication support",
        description="A 14-day package with alternate-day visits for blood sugar checks, "
                     "insulin administration support, and dietary guidance.",
        target_condition="Type 1 / Type 2 diabetes management",
        min_tier=WorkerTier.tier2,
        gender_restriction=GenderRestriction.any,
        visit_frequency=VisitFrequency.alternate_days,
        visits_per_cycle=7,
        cycle_duration_days=14,
        package_price=Decimal("4999"),
        per_visit_price=Decimal("714"),
        subsidy_eligible=True,
        commission_pct=Decimal("20"),
        requires_prescription=True,
        insurance_covered=True,
    ),
    dict(
        package_code="MATERNITY_POSTNATAL_30D",
        name="Postnatal Mother & Baby Care — 30 Day",
        tagline="Daily postnatal care for mother and newborn",
        description="30 days of daily visits supporting postnatal recovery for the mother "
                     "and newborn care guidance.",
        target_condition="Postnatal recovery, newborn care",
        min_tier=WorkerTier.tier3,
        gender_restriction=GenderRestriction.female_only,
        visit_frequency=VisitFrequency.daily,
        visits_per_cycle=30,
        cycle_duration_days=30,
        package_price=Decimal("21999"),
        per_visit_price=Decimal("733"),
        subsidy_eligible=False,
        commission_pct=Decimal("20"),
        requires_prescription=False,
        insurance_covered=True,
    ),
]


# ---------------------------------------------------------------------------
# Package -> Service linkage.
#
# Root cause of the "care package booking shows the whole service catalogue"
# bug: PACKAGES above never set primary_service_id / included_service_ids,
# so CarePackage.primary_service_id was NULL for every seeded package and
# the frontend had nothing to filter the Service dropdown by.
#
# Each entry maps a package_code to the service_code(s) it actually covers.
# The first code in the list becomes primary_service_id; the full list
# becomes included_service_ids. Kept as data (not hardcoded IDs) — codes are
# resolved to real UUIDs at seed time, and this is the single place a future
# package/service change needs to be reflected.
# ---------------------------------------------------------------------------
PACKAGE_SERVICE_LINKS: dict[str, list[str]] = {
    "POST_OP_7D": ["POST_OP_CARE", "WOUND_DRESSING", "VITALS_CHECK"],
    "ELDERLY_MONTHLY": ["ELDERLY_DAY_SHIFT", "ELDERLY_NIGHT_SHIFT"],
    "DIABETES_CARE_14D": ["VITALS_CHECK", "INJECTION_IM_IV"],
    "MATERNITY_POSTNATAL_30D": ["VITALS_CHECK", "WOUND_DRESSING"],
}


async def link_package_services(session) -> int:
    """Populate primary_service_id / included_service_ids on every package
    using PACKAGE_SERVICE_LINKS. Runs after seed_services/seed_packages, and
    is safe to re-run (idempotent): only backfills packages whose
    primary_service_id is still NULL, so it also fixes rows created by an
    earlier, pre-fix version of this seed script without touching anything
    an operator may have set manually since.
    """
    await session.flush()  # ensure any newly-added rows have ids

    code_to_id: dict[str, "uuid.UUID"] = {}
    sres = await session.execute(select(ServiceCatalogue.id, ServiceCatalogue.service_code))
    for sid, code in sres.all():
        code_to_id[code] = sid

    linked = 0
    for package_code, service_codes in PACKAGE_SERVICE_LINKS.items():
        pres = await session.execute(select(CarePackage).where(CarePackage.package_code == package_code))
        package = pres.scalar_one_or_none()
        if not package:
            print(f"  ! package {package_code} not found, skipping service link")
            continue
        if package.primary_service_id and package.included_service_ids:
            continue  # already linked — don't clobber

        resolved_ids = [code_to_id[c] for c in service_codes if c in code_to_id]
        missing = [c for c in service_codes if c not in code_to_id]
        if missing:
            print(f"  ! package {package_code} references unknown service code(s): {missing}")
        if not resolved_ids:
            continue

        package.primary_service_id = resolved_ids[0]
        package.included_service_ids = resolved_ids
        linked += 1
        print(f"  + linked package {package_code} -> {service_codes}")
    return linked


# ---------------------------------------------------------------------------
# In-visit questionnaires (ChecklistTemplate) — previously missing entirely,
# which left the nurse's in-visit questionnaire screen blank for every
# service/package. Seeded here + linked onto the matching ServiceCatalogue
# rows via link_service_checklists() below, which (unlike seed_services)
# updates already-existing rows so this works on a DB that was seeded
# before this fix shipped.
# ---------------------------------------------------------------------------
CHECKLIST_TEMPLATES = [
    dict(
        code="CHK-WOUND-DRESSING-V1",
        name="Wound Dressing — Visit Questionnaire",
        service_codes=["WOUND_DRESSING"],
        phase=ChecklistPhase.during_visit,
        questions=[
            {
                "id": "wound_photo_captured",
                "type": "photo",
                "text": "Photo of the wound before dressing",
                "required": True,
                "phase": "during_visit",
            },
            {
                "id": "wound_condition",
                "type": "single_select",
                "text": "Current wound condition",
                "options": ["Healing well", "No change", "Signs of infection", "Worsening"],
                "required": True,
                "phase": "during_visit",
            },
            {
                "id": "pain_level",
                "type": "number",
                "text": "Patient-reported pain level (0–10)",
                "required": True,
                "phase": "during_visit",
            },
            {
                "id": "dressing_type_used",
                "type": "text",
                "text": "Dressing material used",
                "required": True,
                "phase": "during_visit",
            },
            {
                "id": "signs_of_infection_notes",
                "type": "textarea",
                "text": "Notes on any signs of infection (redness, discharge, odour, swelling)",
                "required": False,
                "phase": "during_visit",
            },
            {
                "id": "photo_after_dressing",
                "type": "photo",
                "text": "Photo of the wound after fresh dressing applied",
                "required": True,
                "phase": "post_visit",
            },
            {
                "id": "patient_consent",
                "type": "consent_confirmation",
                "text": "Patient/family consented to the procedure and photos",
                "required": True,
                "phase": "pre_visit",
            },
        ],
    ),
    dict(
        code="CHK-VITALS-CHECK-V1",
        name="Vitals Monitoring — Visit Questionnaire",
        service_codes=["VITALS_CHECK"],
        phase=ChecklistPhase.during_visit,
        questions=[
            {
                "id": "vitals_reading",
                "type": "vitals_entry",
                "text": "Record vitals (BP, pulse, SpO2, temperature, blood sugar)",
                "required": True,
                "phase": "during_visit",
            },
            {
                "id": "vitals_notes",
                "type": "textarea",
                "text": "Any observations to flag for the care team",
                "required": False,
                "phase": "during_visit",
            },
            {
                "id": "patient_consent",
                "type": "consent_confirmation",
                "text": "Patient/family consented to the check",
                "required": True,
                "phase": "pre_visit",
            },
        ],
    ),
]


async def seed_checklist_templates(session) -> int:
    """Create the ChecklistTemplate rows themselves (idempotent by code)."""
    created = 0
    for data in CHECKLIST_TEMPLATES:
        exists = await session.execute(
            select(ChecklistTemplate).where(ChecklistTemplate.code == data["code"])
        )
        if exists.scalar_one_or_none():
            print(f"  · checklist template {data['code']} already exists, skipping")
            continue
        session.add(ChecklistTemplate(
            code=data["code"],
            name=data["name"],
            service_codes=data["service_codes"],
            phase=data["phase"],
            version=1,
            is_active=True,
            status=ContentStatus.published,
            questions=data["questions"],
        ))
        created += 1
        print(f"  + created checklist template {data['code']}")
    return created


async def link_service_checklists(session) -> int:
    """Point ServiceCatalogue.checklist_template_id at the matching template.

    Unlike seed_services(), this DOES touch already-existing service rows —
    that's the whole point, since production already has WOUND_DRESSING /
    VITALS_CHECK seeded from before this fix existed. Never overwrites a
    checklist_template_id an admin already set some other way.
    """
    linked = 0
    for data in CHECKLIST_TEMPLATES:
        tres = await session.execute(
            select(ChecklistTemplate).where(ChecklistTemplate.code == data["code"])
        )
        template = tres.scalar_one_or_none()
        if not template:
            continue
        for service_code in data["service_codes"]:
            sres = await session.execute(
                select(ServiceCatalogue).where(ServiceCatalogue.service_code == service_code)
            )
            service = sres.scalar_one_or_none()
            if not service:
                print(f"  ! service {service_code} not found — cannot link checklist {data['code']}")
                continue
            if service.checklist_template_id:
                print(f"  · service {service_code} already has a checklist template linked, skipping")
                continue
            service.checklist_template_id = template.id
            linked += 1
            print(f"  + linked service {service_code} -> checklist {data['code']}")
    return linked


async def seed_services(session) -> int:
    created = 0
    for data in SERVICES:
        exists = await session.execute(
            select(ServiceCatalogue).where(ServiceCatalogue.service_code == data["service_code"])
        )
        if exists.scalar_one_or_none():
            print(f"  · service {data['service_code']} already exists, skipping")
            continue
        session.add(ServiceCatalogue(**data))
        created += 1
        print(f"  + created service {data['service_code']}")
    return created


async def seed_packages(session) -> int:
    created = 0
    for data in PACKAGES:
        exists = await session.execute(
            select(CarePackage).where(CarePackage.package_code == data["package_code"])
        )
        if exists.scalar_one_or_none():
            print(f"  · package {data['package_code']} already exists, skipping")
            continue
        session.add(CarePackage(**data, is_active=True, version=1))
        created += 1
        print(f"  + created package {data['package_code']}")
    return created


# ---------------------------------------------------------------------------
# Training modules with adaptive MCQ questions
# Sources: ICMR guidelines, NHS UK (NICE/NPSA), AHA/CDC/JAMA best practices
# Each question has a "difficulty" field (1-5) for adaptive testing.
# ---------------------------------------------------------------------------
TRAINING_MODULES = [
    {
        "code": "TRN-INFECTION-CTRL",
        "title": "Infection Control Essentials",
        "description": "Standard precautions, hand hygiene, PPE, sharps safety, and waste management per WHO/ICMR protocols.",
        "category": "Infection Prevention",
        "duration_minutes": 45,
        "is_mandatory": True,
        "pass_percent": 70,
        "required_for_tiers": ["tier1", "tier2", "tier3"],
        "assessment": [
            {
                "id": "ic1", "difficulty": 1, "type": "single_select",
                "question": "According to WHO guidelines, how long should hand washing with soap and water take?",
                "options": ["5 seconds", "20–30 seconds", "60 seconds", "Only needed before procedures"],
                "correct_index": 1,
                "explanation": "WHO recommends 20–30 seconds of active hand scrubbing with soap and water to effectively reduce pathogens."
            },
            {
                "id": "ic2", "difficulty": 1, "type": "single_select",
                "question": "Which of the following is NOT one of the five moments of hand hygiene defined by WHO?",
                "options": ["Before touching a patient", "After touching patient surroundings", "Before eating a meal", "After body fluid exposure risk"],
                "correct_index": 2,
                "explanation": "The 5 WHO moments are: before patient contact, before aseptic procedure, after body fluid exposure risk, after patient contact, after touching patient surroundings."
            },
            {
                "id": "ic3", "difficulty": 2, "type": "single_select",
                "question": "A nurse is about to perform a urinary catheter insertion. Which category of PPE is minimally required?",
                "options": ["No PPE needed", "Gloves only", "Gloves and sterile drape", "Full gown, gloves, mask, goggles"],
                "correct_index": 2,
                "explanation": "Urinary catheterisation is an aseptic procedure requiring sterile gloves and sterile drape at minimum per ICMR/NHS guidelines."
            },
            {
                "id": "ic4", "difficulty": 2, "type": "single_select",
                "question": "Yellow waste bags in India (as per BMW Rules 2016) are used for:",
                "options": ["Recyclable plastic", "Human anatomical waste and soiled dressings", "Sharps only", "General kitchen waste"],
                "correct_index": 1,
                "explanation": "As per Bio-Medical Waste Management Rules 2016, yellow bags are for human anatomical waste, soiled/contaminated solid waste, and expired medicines."
            },
            {
                "id": "ic5", "difficulty": 3, "type": "single_select",
                "question": "A home-visit patient has active pulmonary tuberculosis on rifampicin therapy. The nurse must prioritise which PPE?",
                "options": ["Surgical mask and gloves", "N95 respirator, gloves, and gown", "Face shield only", "Apron only — open TB on treatment is not infectious"],
                "correct_index": 1,
                "explanation": "Active pulmonary TB requires N95 respirator (not just surgical mask), gloves and gown. Patients on therapy for ≥2 weeks with documented sputum conversion may be less infectious but precautions remain until confirmed."
            },
            {
                "id": "ic6", "difficulty": 3, "type": "single_select",
                "question": "Which technique is correct for doffing (removing) gloves to prevent self-contamination?",
                "options": ["Pull both gloves off simultaneously from the wrists", "Pinch outside of first glove, peel inward; insert finger under second glove cuff, peel inward", "Remove gloves with bare hands touching the outside surface", "Rinse gloves with water before removal"],
                "correct_index": 1,
                "explanation": "Proper doffing: pinch the outside of one glove at the wrist without touching skin, peel off inward. Slide a clean finger under the second glove cuff and peel off inward, trapping the first glove inside."
            },
            {
                "id": "ic7", "difficulty": 4, "type": "single_select",
                "question": "A community nurse accidentally suffers a needlestick from a known HIV-positive patient. The CORRECT immediate action is:",
                "options": ["Apply pressure and cover with a plaster, no further action needed", "Wash with soap and water, report immediately, and start PEP within 72 hours", "Apply alcohol and wait to see if symptoms develop", "Only report if skin is broken deeply"],
                "correct_index": 1,
                "explanation": "ICMR/NACO protocol: wash with soap and water (do NOT squeeze), report to OIC, and initiate HIV Post-Exposure Prophylaxis (PEP) within 72 hours (ideally 2 hours) per national guidelines."
            },
            {
                "id": "ic8", "difficulty": 4, "type": "single_select",
                "question": "Which of the following organisms is MOST commonly responsible for healthcare-associated urinary tract infections (HAUTIs)?",
                "options": ["Staphylococcus aureus", "Escherichia coli", "Candida albicans", "Pseudomonas aeruginosa"],
                "correct_index": 1,
                "explanation": "E. coli accounts for ~65–70% of HAUTIs. Most are associated with urinary catheters (CAUTIs). ICMR surveillance data confirms E. coli as the leading gram-negative uropathogen."
            },
            {
                "id": "ic9", "difficulty": 5, "type": "single_select",
                "question": "A nurse changes a PICC line dressing on a patient at home. The site appears clean with no redness. The patient's temperature is 38.4°C and has rigors. The MOST appropriate action is:",
                "options": ["Continue with dressing change and monitor temperature", "Remove the PICC line immediately at home", "Notify physician urgently — suspect CLABSI; do NOT remove line at home without medical order", "Apply antibiotic cream to the insertion site"],
                "correct_index": 2,
                "explanation": "Fever + rigors with a central line in situ suggests possible CLABSI (Central Line-Associated BSI). The nurse must notify the physician immediately. Line removal without blood cultures and medical order may destroy diagnostic evidence. Per CDC/NHS criteria, CLABSI requires blood cultures from both the line and a peripheral site before any intervention."
            },
            {
                "id": "ic10", "difficulty": 5, "type": "single_select",
                "question": "The most effective strategy to prevent catheter-associated urinary tract infections (CAUTI) per HICPAC/CDC guidelines is:",
                "options": ["Routine daily catheter cleaning with antiseptic", "Avoid unnecessary catheterisation and remove catheters as early as possible", "Change indwelling catheters every 7 days prophylactically", "Use antibiotic-impregnated catheters in all patients"],
                "correct_index": 1,
                "explanation": "The primary CAUTI prevention strategy per CDC/HICPAC is avoiding unnecessary catheter insertion and removing catheters promptly when no longer needed. Routine antiseptic cleaning, prophylactic changes, and antibiotic-impregnated catheters are not recommended as first-line strategies for all patients."
            },
        ]
    },
    {
        "code": "TRN-CLINICAL-ESCALATION",
        "title": "Clinical Escalation Protocols",
        "description": "Early warning scoring (NEWS2), SBAR communication, and escalation pathways per NHS/ICMR standards.",
        "category": "Clinical Skills",
        "duration_minutes": 30,
        "is_mandatory": True,
        "pass_percent": 75,
        "required_for_tiers": ["tier1", "tier2", "tier3"],
        "assessment": [
            {
                "id": "ce1", "difficulty": 1, "type": "single_select",
                "question": "What does SBAR stand for?",
                "options": ["Situation, Background, Assessment, Recommendation", "Status, Briefing, Alert, Response", "Symptom, Blood pressure, Assessment, Report", "Situation, Body, Action, Resolution"],
                "correct_index": 0,
                "explanation": "SBAR: Situation (what is happening), Background (why it is happening), Assessment (what you think), Recommendation (what you need). Standard NHS/JCAHO handoff tool."
            },
            {
                "id": "ce2", "difficulty": 1, "type": "single_select",
                "question": "A SpO2 reading of 94% on room air in a previously healthy adult should be classified as:",
                "options": ["Normal — no action", "Mildly concerning — monitor more frequently", "Concerning — initiate supplemental oxygen and escalate", "Critical — call emergency services immediately"],
                "correct_index": 2,
                "explanation": "SpO2 <95% on room air is below normal in a healthy adult. Per NEWS2/BTS guidelines, SpO2 94% warrants supplemental oxygen, increased monitoring frequency, and clinical escalation."
            },
            {
                "id": "ce3", "difficulty": 2, "type": "single_select",
                "question": "In the NEWS2 scoring system, what respiratory rate scores 3 (highest urgency) points?",
                "options": ["12–20/min", "21–24/min", "≤8/min or ≥25/min", "9–11/min"],
                "correct_index": 2,
                "explanation": "NEWS2 assigns 3 points (maximum) for respiratory rate ≤8/min or ≥25/min. This triggers urgent escalation to the clinical team per RCP NEWS2 protocol."
            },
            {
                "id": "ce4", "difficulty": 2, "type": "single_select",
                "question": "A home-care patient's blood pressure drops from 130/80 to 90/60 mmHg over 30 minutes with increased heart rate. This is MOST consistent with:",
                "options": ["White-coat hypertension resolving", "Septic or haemorrhagic shock — requires immediate escalation", "Normal postural hypotension", "Medication effect — observe for 2 hours"],
                "correct_index": 1,
                "explanation": "A drop in systolic BP to <90 mmHg with compensatory tachycardia meets criteria for shock (septic, haemorrhagic, or cardiogenic). Immediate escalation and emergency services activation are required per NICE sepsis pathway NG51."
            },
            {
                "id": "ce5", "difficulty": 3, "type": "single_select",
                "question": "A 68-year-old patient post-hip replacement has sudden onset confusion, temperature 38.8°C, HR 112, RR 24, and the wound appears erythematous. Using the UK Sepsis Trust criteria, the FIRST priority action is:",
                "options": ["Reassure family and re-measure vitals in 30 minutes", "Give paracetamol for fever and document observations", "Escalate immediately using SBAR — suspected sepsis. Call for emergency assessment", "Apply cool compress and encourage oral fluids"],
                "correct_index": 2,
                "explanation": "This patient has ≥2 SIRS criteria plus a suspected source (wound infection) in a post-surgical patient — high-risk for sepsis per UK Sepsis Trust / NICE NG51. Immediate escalation (999/emergency medical services) and SBAR communication to physician is mandatory. The 'Sepsis Six' bundle should be initiated within 1 hour."
            },
            {
                "id": "ce6", "difficulty": 3, "type": "single_select",
                "question": "When escalating a deteriorating patient by phone, which information is NOT part of the SBAR framework?",
                "options": ["Patient's current medications list", "Your assessment of the most likely problem", "What you recommend (e.g., come and review, change medications)", "The specific situation prompting the call"],
                "correct_index": 0,
                "explanation": "SBAR focuses on Situation, Background, Assessment, and Recommendation. While medication history is useful context, it is not a named SBAR component. The medication list may be mentioned under Background but is not a distinct element."
            },
            {
                "id": "ce7", "difficulty": 4, "type": "single_select",
                "question": "A paediatric patient aged 4 months has a respiratory rate of 66/min, SpO2 92%, and grunting. The home nurse should:",
                "options": ["Call the paediatrician and await call-back", "Immediately call emergency services (108/112) and begin supportive care", "Increase room temperature and re-assess in 20 minutes", "Give nebulised saline and monitor SpO2"],
                "correct_index": 1,
                "explanation": "Respiratory rate >60/min, SpO2 <95%, and grunting in an infant are signs of severe respiratory distress per IAP/WHO guidelines. This is a life-threatening emergency. Immediate emergency services (108) must be activated without delay."
            },
            {
                "id": "ce8", "difficulty": 4, "type": "single_select",
                "question": "A patient with known COPD has oxygen saturation targets that differ from general adults. The target SpO2 range for COPD patients receiving supplemental oxygen is:",
                "options": ["98–100%", "95–98%", "88–92%", "Any level above 85% is acceptable"],
                "correct_index": 2,
                "explanation": "Per BTS/NICE guidelines, most COPD patients have a target SpO2 of 88–92% because high-flow oxygen can suppress hypoxic drive and worsen hypercapnia (CO2 retention). Standard adult target of 94–98% does NOT apply to at-risk COPD patients."
            },
            {
                "id": "ce9", "difficulty": 5, "type": "single_select",
                "question": "A post-surgical patient at home becomes acutely confused with a NEWS2 score of 7. The nursing protocol requires escalation within 30 minutes. Which of the following responses is MOST appropriate?",
                "options": ["Document and reassess in 1 hour as confusion may be anaesthetic residual", "Call the on-call physician, use SBAR, implement continuous monitoring until help arrives, and be prepared to activate emergency services if NEWS2 continues to rise", "Reduce analgesics — confusion may be opioid-induced", "Encourage oral fluids and re-assess after rehydration"],
                "correct_index": 1,
                "explanation": "NEWS2 ≥7 is a medical emergency requiring continuous monitoring and urgent physician review within 30 minutes. The nurse must use SBAR to communicate, document all vitals, maintain IV access if present, and be ready to escalate to emergency services if the patient deteriorates further. Reducing analgesia without physician order is inappropriate."
            },
            {
                "id": "ce10", "difficulty": 5, "type": "single_select",
                "question": "A patient's ECG (taken with a portable monitor) shows ST elevation in leads II, III, aVF with reciprocal ST depression in I and aVL. The patient complains of crushing chest pain radiating to the jaw. The nurse's IMMEDIATE action is:",
                "options": ["Administer GTN sublingual and re-assess ECG in 5 minutes", "Call emergency services (108/112) immediately — this is an inferior STEMI. Do not delay for further assessment", "Give aspirin 325mg orally and notify the cardiologist", "Obtain IV access and give thrombolytics if available"],
                "correct_index": 1,
                "explanation": "ST elevation in II, III, aVF = inferior STEMI. This requires immediate emergency services activation (108). Time is myocardium — door-to-balloon target is 90 minutes from first medical contact. The nurse should call 108 first, then give aspirin 325mg (if no contraindication) and oxygen, and prepare to initiate CPR if cardiac arrest occurs. Thrombolytics are not administered at home by nursing staff."
            },
        ]
    },
    {
        "code": "TRN-GERIATRIC-FALLS",
        "title": "Geriatric Mobility & Falls Prevention",
        "description": "Falls risk assessment (Morse scale), safe transfers, fall prevention strategies, and family communication per NICE NG161/ICMR guidelines.",
        "category": "Geriatric Care",
        "duration_minutes": 60,
        "is_mandatory": True,
        "pass_percent": 70,
        "required_for_tiers": ["tier1", "tier2", "tier3"],
        "assessment": [
            {
                "id": "gf1", "difficulty": 1, "type": "single_select",
                "question": "Which of the following is the most common modifiable risk factor for falls in elderly patients?",
                "options": ["Age above 65", "Taking 4 or more medications (polypharmacy)", "Female gender", "Having a previous stroke"],
                "correct_index": 1,
                "explanation": "Polypharmacy (≥4 medications) is the most common MODIFIABLE falls risk factor. Sedatives, antihypertensives, diuretics, and antidepressants significantly increase falls risk. Medication review is a key NICE NG161 recommendation."
            },
            {
                "id": "gf2", "difficulty": 1, "type": "single_select",
                "question": "The Morse Fall Scale assesses which of the following factors?",
                "options": ["Age, gender, weight, height, recent surgery", "History of falling, secondary diagnosis, ambulatory aid, IV/heparin, gait, and mental status", "GCS score, oxygen saturation, pain score, and temperature", "BMI, blood pressure, pulse, and cognitive function"],
                "correct_index": 1,
                "explanation": "The Morse Fall Scale (MFS) has 6 items: history of falling, secondary diagnosis, ambulatory aid use, IV/heparin lock, gait/transfer status, and mental status. Score ≥45 = high risk."
            },
            {
                "id": "gf3", "difficulty": 2, "type": "single_select",
                "question": "When assisting an elderly patient from bed to chair (transfer), which action is SAFEST?",
                "options": ["Lift the patient by holding under the armpits", "Use a gait belt, have the patient bear weight on stronger leg, pivot to chair", "Transfer quickly to minimise discomfort", "Ask the patient to grab the nurse's neck for support"],
                "correct_index": 1,
                "explanation": "Safe transfer technique: apply a gait belt around the waist, position patient's stronger leg forward, have them lean forward to shift weight, then pivot to the chair. Avoid axillary lifts (shoulder injury risk) and never allow patients to grab the caregiver's neck."
            },
            {
                "id": "gf4", "difficulty": 2, "type": "single_select",
                "question": "A patient scores 55 on the Morse Fall Scale. Which environmental modification is MOST immediately effective?",
                "options": ["Move the patient to a hospital", "Keep the call bell within reach, ensure non-slip footwear, clear pathways, and lower the bed", "Apply bilateral wrist restraints", "Place the patient in a wheelchair at all times"],
                "correct_index": 1,
                "explanation": "MFS ≥45 = high risk. Immediate environmental interventions: call bell within reach, bed at lowest height, non-slip footwear, cleared pathways. Restraints are NOT recommended (increase fall injury risk) and are a human rights concern per NICE guidelines."
            },
            {
                "id": "gf5", "difficulty": 3, "type": "single_select",
                "question": "An 82-year-old woman with osteoporosis falls while walking to the bathroom. She is conscious but complains of severe hip pain and cannot bear weight. The nurse should:",
                "options": ["Help her stand and assist to bed — a brief rest usually resolves minor falls", "Do not move her. Call emergency services, keep her warm and still, reassure her, and document fall details", "Massage the hip and give oral analgesic before any assessment", "Ask family members to help lift her"],
                "correct_index": 1,
                "explanation": "Hip fracture must be suspected in an elderly osteoporotic patient after a fall with inability to bear weight. Moving the patient risks displacing a fracture. Emergency services must be called. Keep the patient still and warm, provide reassurance, and document circumstances per incident protocol. This is a NICE NG161 and RCN guidance recommendation."
            },
            {
                "id": "gf6", "difficulty": 3, "type": "single_select",
                "question": "Which medication class has the STRONGEST evidence for increasing falls risk in older adults?",
                "options": ["Antibiotics", "Benzodiazepines and Z-drugs (sedative-hypnotics)", "Statins", "Antihistamines (non-sedating)"],
                "correct_index": 1,
                "explanation": "Benzodiazepines and Z-drugs (zolpidem, zopiclone) are the medication class with strongest evidence for falls. They cause sedation, muscle relaxation, and impaired balance. De-prescribing or dose reduction is a NICE NG161 priority."
            },
            {
                "id": "gf7", "difficulty": 4, "type": "single_select",
                "question": "A nurse is completing the Timed Up and Go (TUG) test. The patient takes 16 seconds to complete the test. This indicates:",
                "options": ["Normal mobility — no intervention needed", "Moderate fall risk — consider physiotherapy referral and home hazard assessment", "Severe fall risk — patient should be bedridden", "The test is invalid for community settings"],
                "correct_index": 1,
                "explanation": "TUG >12 seconds indicates increased fall risk. 16 seconds = moderate risk per CDC/NICE guidance. Referral to physiotherapy for strength/balance training and a home hazard assessment are recommended. TUG is valid in community settings."
            },
            {
                "id": "gf8", "difficulty": 4, "type": "single_select",
                "question": "NICE NG161 recommends multifactorial falls risk assessment. Which combination of interventions has the strongest evidence for falls reduction in older adults?",
                "options": ["Vitamin D supplements alone", "Balance and strength training + medication review + home hazard modification", "Hip protectors and bed rails only", "Calcium supplementation and dietary advice"],
                "correct_index": 1,
                "explanation": "NICE NG161 Grade A evidence: multifactorial interventions combining exercise (strength/balance), medication review (particularly sedatives, antihypertensives), and home hazard modification provide the greatest reduction in falls rate and fall-related injuries."
            },
            {
                "id": "gf9", "difficulty": 5, "type": "single_select",
                "question": "A post-stroke patient has left hemiplegia and moderate cognitive impairment. During a home visit, the nurse observes the patient attempting to stand without calling for help despite a high falls risk. The BEST nursing response is:",
                "options": ["Restrain the patient in the chair to prevent falls", "Educate the patient each visit (even if forgotten), use visual cues and bed alarms, and develop a family safety plan with care team input", "Document and leave — cognitive impairment means education is futile", "Discharge the patient to a nursing home as home care is unsafe"],
                "correct_index": 1,
                "explanation": "Restraints are not recommended and worsen outcomes. For cognitively impaired patients, repeated simple education each visit (even if not retained), visual bed-exit alarms, call-bell placement, and family/carer safety plans are evidence-based strategies per NICE NG161. The care team should reassess the care plan with the family."
            },
            {
                "id": "gf10", "difficulty": 5, "type": "single_select",
                "question": "A 79-year-old diabetic man on insulin glargine, metoprolol, and amlodipine is at high falls risk. His fasting blood sugar this morning was 3.8 mmol/L (68 mg/dL). Which is the MOST important immediate clinical consideration?",
                "options": ["The blood sugar is acceptable — proceed with morning care", "Hypoglycaemia increases falls risk acutely — treat hypoglycaemia first, re-check blood sugar, and inform physician before giving insulin", "Administer insulin as prescribed — blood sugar will normalise after breakfast", "Increase metoprolol dose as beta-blockers mask hypoglycaemia symptoms"],
                "correct_index": 1,
                "explanation": "BSL 3.8 mmol/L (68 mg/dL) = hypoglycaemia (<4.0 mmol/L). Hypoglycaemia is a primary acute falls risk factor — it causes dizziness, confusion, and weakness. Treat immediately (15g fast-acting carbohydrate per the '15-15 rule'), recheck BSL, hold insulin until physician review, and inform physician. Metoprolol can mask tachycardia (a warning symptom of hypoglycaemia) making this situation more dangerous."
            },
        ]
    },
    {
        "code": "TRN-WOUND-CARE",
        "title": "Wound Care — Advanced",
        "description": "Wound assessment, dressing selection, exudate management, pressure ulcer prevention, and wound photography per NPUAP/EPUAP guidelines.",
        "category": "Clinical Skills",
        "duration_minutes": 90,
        "is_mandatory": False,
        "pass_percent": 70,
        "required_for_tiers": ["tier2", "tier3"],
        "assessment": [
            {
                "id": "wc1", "difficulty": 1, "type": "single_select",
                "question": "A Stage 2 pressure ulcer is best described as:",
                "options": ["Intact skin with non-blanchable redness", "Partial-thickness skin loss involving epidermis and/or dermis — shallow open wound", "Full-thickness skin loss with visible subcutaneous tissue", "Full-thickness tissue loss with exposed bone, tendon, or muscle"],
                "correct_index": 1,
                "explanation": "NPUAP/EPUAP classification: Stage 1 = non-blanchable erythema, Stage 2 = partial thickness loss (dermis/epidermis), Stage 3 = full thickness with subcutaneous tissue, Stage 4 = full thickness with bone/tendon/muscle visible."
            },
            {
                "id": "wc2", "difficulty": 1, "type": "single_select",
                "question": "For a wound with moderate exudate and no infection, which primary dressing provides the best moist wound healing environment?",
                "options": ["Dry gauze", "Hydrocolloid dressing", "Calcium alginate or foam dressing", "Iodine-soaked gauze"],
                "correct_index": 2,
                "explanation": "Calcium alginate and foam dressings absorb moderate-to-heavy exudate while maintaining a moist environment. NICE/NICE evidence review supports moist wound healing as standard of care. Dry gauze disrupts healing; hydrocolloid is better for low-exudate wounds."
            },
            {
                "id": "wc3", "difficulty": 2, "type": "single_select",
                "question": "When documenting wound dimensions, the CORRECT method for measuring wound length is:",
                "options": ["Always measure at the widest point regardless of body position", "Head-to-toe axis (superior to inferior) for length, perpendicular for width", "Estimate visually and document as 'approximately X cm'", "Measure only depth as surface dimensions are not clinically relevant"],
                "correct_index": 1,
                "explanation": "Standard wound documentation: length = head-to-toe axis, width = perpendicular, depth = deepest point measured with probe. This ensures consistent comparison between visits and between clinicians."
            },
            {
                "id": "wc4", "difficulty": 2, "type": "single_select",
                "question": "Signs of wound infection (as opposed to normal inflammation) include:",
                "options": ["Mild warmth and slight redness at wound edges at day 3 post-surgery", "Increasing pain, purulent discharge, malodour, pyrexia, and failure to progress", "Small amount of clear/straw-coloured exudate", "Pink/red granulation tissue in the wound base"],
                "correct_index": 1,
                "explanation": "Wound infection signs: NERDS/STONEES criteria include increasing pain, purulent (not serous) discharge, malodour, systemic signs (pyrexia/raised CRP), wound deterioration or failure to progress. Granulation tissue and mild early erythema are normal healing phases."
            },
            {
                "id": "wc5", "difficulty": 3, "type": "single_select",
                "question": "A diabetic foot ulcer is classified as Wagner Grade 3. This means:",
                "options": ["Superficial ulcer involving skin only", "Deep ulcer reaching tendon, capsule, or bone", "Ulcer with deep abscess, osteomyelitis, or joint sepsis", "Localised gangrene with background peripheral vascular disease"],
                "correct_index": 2,
                "explanation": "Wagner Diabetic Foot Classification: Grade 0=pre/post-ulcer, Grade 1=superficial ulcer, Grade 2=deep to tendon/capsule/bone, Grade 3=deep ulcer with abscess/osteomyelitis/joint sepsis, Grade 4=localised gangrene, Grade 5=extensive gangrene. Grade 3 requires urgent hospital referral."
            },
            {
                "id": "wc6", "difficulty": 3, "type": "single_select",
                "question": "A pressure ulcer over the sacrum has slough covering >75% of the wound bed. The MOST appropriate debridement method for a community nurse is:",
                "options": ["Sharp (surgical) debridement with a scalpel at the bedside", "Autolytic debridement using a hydrogel or hydrocolloid to soften slough", "High-pressure irrigation with normal saline using a syringe", "Maggot therapy (larval therapy) as first-line treatment"],
                "correct_index": 1,
                "explanation": "Autolytic debridement (hydrogel, hydrocolloid) is the appropriate first-line method for community nurses. It is safe, painless, and selective (removes only non-viable tissue). Sharp debridement requires specialist training and is not within most community nurse scope. Maggot therapy is a specialist intervention for resistant wounds."
            },
            {
                "id": "wc7", "difficulty": 4, "type": "single_select",
                "question": "A patient has a venous leg ulcer with heavy exudate, but also has a low ankle brachial pressure index (ABPI) of 0.65. The nurse should:",
                "options": ["Apply full four-layer compression bandaging as per standard VLU protocol", "Apply modified/reduced compression (e.g. 23–30 mmHg) after vascular team assessment, not full compression", "Avoid all compression — ABPI <0.8 is a contraindication to any form of compression", "Refer immediately to vascular surgery for amputation assessment"],
                "correct_index": 1,
                "explanation": "ABPI 0.5–0.8 = mixed arterial-venous disease. Full compression (40 mmHg) is contraindicated. Reduced/modified compression (23–30 mmHg) may be appropriate after vascular assessment. ABPI <0.5 = full compression contraindicated. Per SIGN/NICE guidelines, ABPI ≤0.8 requires vascular assessment before any compression."
            },
            {
                "id": "wc8", "difficulty": 4, "type": "single_select",
                "question": "A fistula wound is identified during dressing change. Profuse watery fluid is draining. The nurse suspects an enterocutaneous fistula. The priority action is:",
                "options": ["Apply a moisture barrier and standard foam dressing", "Protect surrounding skin with Cavilon/barrier cream, measure output, and notify the physician urgently for specialist wound management plan", "Apply silver-impregnated dressing and close the wound", "Irrigate the fistula with normal saline to clear blockage"],
                "correct_index": 1,
                "explanation": "Enterocutaneous fistulas require specialist stoma/wound care nurse input and physician management to address underlying pathology (often requires surgical intervention). Immediate priorities: protect surrounding skin from enzymatic fluid, accurately measure output to guide fluid/electrolyte replacement, and refer for specialist care. Attempting to close a fistula is dangerous."
            },
            {
                "id": "wc9", "difficulty": 5, "type": "single_select",
                "question": "During a wound dressing for a post-mastectomy patient on Herceptin (trastuzumab), you notice an area of wound dehiscence 4cm long with no signs of infection but delayed healing. The MOST likely explanation and action is:",
                "options": ["Normal surgical healing — document and continue standard dressings", "Chemotherapy and targeted therapy impair wound healing; notify the oncology team before changing the wound management plan", "Apply skin closure strips and discharge patient from wound care", "Irrigate vigorously and pack the wound tightly with ribbon gauze"],
                "correct_index": 1,
                "explanation": "Trastuzumab (Herceptin) and many chemotherapy agents impair angiogenesis and cellular proliferation, delaying wound healing. Wound dehiscence in an oncology patient requires oncology team notification to determine safe management (potential chemotherapy hold, specialist wound care). Closure strips on a dehisced wound in an immunocompromised patient risk trapping infection."
            },
            {
                "id": "wc10", "difficulty": 5, "type": "single_select",
                "question": "A patient with a grade 4 sacral pressure ulcer has a wound base of 50% black necrotic eschar and 50% yellow slough. The wound cavity is 3cm deep. The nurse assesses significant pain. Regarding treatment planning, which statement is CORRECT?",
                "options": ["Pack the wound tightly with normal saline gauze to manage the cavity", "A multidisciplinary wound care plan is essential — debridement method should consider patient's pain, vascular status, and prognosis; conservative moist wound management may be appropriate for palliative patients", "Immediately debride all necrotic tissue with sharp debridement to promote healing", "Apply hydrocolloid dressing — it will liquefy all necrotic tissue within 48 hours"],
                "correct_index": 1,
                "explanation": "Stage 4 pressure ulcers with necrotic/slough require multidisciplinary planning. For palliative patients, aggressive debridement may not align with goals of care and can be harmful. Patient pain, vascular status (ischaemic wounds should not be debrided without vascular assessment), and prognosis guide wound management goals (healing vs. palliation). Hydrocolloids are not appropriate for deep cavity wounds or heavily infected wounds."
            },
        ]
    },
    {
        "code": "TRN-DIABETES-MONITORING",
        "title": "Diabetes Monitoring & Management",
        "description": "Glucometry technique, insulin administration, hypoglycaemia/hyperglycaemia protocols, and dietary guidance per ICMR/ADA/NHS guidelines.",
        "category": "Chronic Disease Management",
        "duration_minutes": 40,
        "is_mandatory": False,
        "pass_percent": 70,
        "required_for_tiers": ["tier2", "tier3"],
        "assessment": [
            {
                "id": "dm1", "difficulty": 1, "type": "single_select",
                "question": "The target fasting plasma glucose for a Type 2 diabetes patient per ADA/ICMR guidelines is:",
                "options": ["3.0–4.0 mmol/L (54–72 mg/dL)", "4.4–7.2 mmol/L (80–130 mg/dL)", "8.0–10.0 mmol/L (144–180 mg/dL)", "Any value below 12 mmol/L (216 mg/dL) is acceptable"],
                "correct_index": 1,
                "explanation": "ADA 2023 and ICMR guidelines: target fasting blood glucose 80–130 mg/dL (4.4–7.2 mmol/L) for most non-pregnant adults with T2DM. Targets are individualized based on age, comorbidities, and hypoglycaemia risk."
            },
            {
                "id": "dm2", "difficulty": 1, "type": "single_select",
                "question": "Which symptom is a classic sign of hypoglycaemia?",
                "options": ["Polyuria (frequent urination)", "Polydipsia (excessive thirst)", "Diaphoresis (sweating) and tremor", "Kussmaul breathing (deep rapid breathing)"],
                "correct_index": 2,
                "explanation": "Hypoglycaemia symptoms: sweating, tremor, palpitations (adrenergic), and confusion, weakness, headache (neuroglycopenic). Polyuria, polydipsia, and Kussmaul breathing are signs of hyperglycaemia/DKA."
            },
            {
                "id": "dm3", "difficulty": 2, "type": "single_select",
                "question": "A patient with Type 1 diabetes has blood glucose of 3.2 mmol/L (58 mg/dL) and is conscious and cooperative. The '15-15 rule' requires:",
                "options": ["Give 15 units of rapid-acting insulin and recheck in 15 minutes", "Give 15g fast-acting carbohydrate (e.g. 120ml fruit juice), wait 15 minutes, recheck blood glucose", "Administer 50% dextrose IV and monitor for 15 minutes", "Give 15g of complex carbohydrate (e.g. whole grain bread) for slower absorption"],
                "correct_index": 1,
                "explanation": "The 15-15 rule (ADA/NHS): if BSL <4.0 mmol/L (72 mg/dL) in a conscious patient, give 15g fast-acting carbohydrate (glucose tablets, 150ml fruit juice, or 150ml regular Coke). Recheck BSL after 15 minutes. Repeat if still <4.0. Complex carbs are too slow; IV dextrose is for unconscious patients."
            },
            {
                "id": "dm4", "difficulty": 2, "type": "single_select",
                "question": "Which injection site for insulin has the fastest absorption rate?",
                "options": ["Outer thigh", "Buttock", "Abdomen (periumbilical area)", "Upper arm"],
                "correct_index": 2,
                "explanation": "Absorption rate by site: abdomen (fastest) > arm > thigh > buttock (slowest). Rapid-acting insulins (e.g. NovoRapid, Humalog) should ideally be injected in the abdomen for pre-meal use. Rotation within the same body region reduces lipohypertrophy."
            },
            {
                "id": "dm5", "difficulty": 3, "type": "single_select",
                "question": "A patient with T2DM has a random blood glucose of 19.8 mmol/L (356 mg/dL) and complains of nausea and vomiting. The nurse should:",
                "options": ["Give extra insulin on sliding scale and recheck in 2 hours", "Check for ketones (urinary or blood), assess vital signs, check hydration status, and escalate to physician urgently — possible DKA or HHS", "Encourage oral fluids and withhold insulin until the patient can eat", "Reassure the patient — high blood sugar with vomiting is expected after a heavy meal"],
                "correct_index": 1,
                "explanation": "Marked hyperglycaemia with vomiting in a diabetic patient requires urgent assessment for DKA (Type 1/Type 2) or HHS (Type 2). Check ketones: moderate/large ketonaemia + vomiting = possible DKA. Vital signs and physician escalation are mandatory. Withholding insulin in DKA is dangerous. HHS can occur without ketones but with severe dehydration."
            },
            {
                "id": "dm6", "difficulty": 3, "type": "single_select",
                "question": "A patient's HbA1c is 9.2% at a review visit. According to ICMR/ADA guidelines, this corresponds to an estimated average blood glucose (eAG) of approximately:",
                "options": ["140 mg/dL (7.8 mmol/L)", "212 mg/dL (11.8 mmol/L)", "180 mg/dL (10.0 mmol/L)", "250 mg/dL (13.9 mmol/L)"],
                "correct_index": 1,
                "explanation": "HbA1c to eAG formula: eAG (mg/dL) = 28.7 × HbA1c − 46.7. For HbA1c 9.2%: 28.7 × 9.2 = 264 − 46.7 = 217 mg/dL (~12.1 mmol/L). Closest option is 212 mg/dL (11.8 mmol/L). This indicates poor glycaemic control requiring medication review."
            },
            {
                "id": "dm7", "difficulty": 4, "type": "single_select",
                "question": "A T1DM patient on insulin pump therapy (CSII) develops DKA despite normal-appearing blood glucose on the pump display. The MOST likely explanation is:",
                "options": ["The patient has eaten more carbohydrates than programmed", "Pump cannula occlusion or dislodgement — no insulin is being delivered despite pump indicating delivery", "The blood glucose meter is faulty", "This cannot occur with pump therapy"],
                "correct_index": 1,
                "explanation": "Insulin pump DKA: the most common cause is cannula occlusion, kinking, or dislodgement — the pump continues to operate but insulin is not infused. Even brief interruptions (hours) can precipitate DKA in T1DM due to no basal insulin reserve. The nurse must check cannula insertion site, change infusion set, and administer correction dose via injection while escalating to the diabetes team."
            },
            {
                "id": "dm8", "difficulty": 4, "type": "single_select",
                "question": "A patient takes metformin and a SGLT2 inhibitor (empagliflozin). They are admitted for a CT scan with contrast dye. The nurse should advise:",
                "options": ["Continue all medications as normal", "Hold metformin 48 hours before/after contrast, and hold SGLT2 inhibitor before procedure to reduce SGLT2-inhibitor associated DKA risk", "Double the SGLT2 inhibitor dose to compensate for contrast-induced stress", "Continue SGLT2 inhibitor but hold insulin"],
                "correct_index": 1,
                "explanation": "Metformin should be held 48h before and after contrast due to lactic acidosis risk with contrast-induced nephropathy. SGLT2 inhibitors must be held before surgical/procedural stress — they can cause euglycaemic DKA (normal blood glucose but ketoacidosis) during stress, starvation, or dehydration. Per ADA/RCP guidance."
            },
            {
                "id": "dm9", "difficulty": 5, "type": "single_select",
                "question": "An elderly diabetic patient (78 years, CKD stage 3b, eGFR 38 mL/min) is on glibenclamide (a sulphonylurea) for T2DM. Why is this particularly dangerous in this patient?",
                "options": ["Sulphonylureas are ineffective in patients with CKD", "Glibenclamide's active metabolites accumulate in CKD, causing prolonged and potentially fatal hypoglycaemia", "The patient should be switched to insulin immediately as all oral agents are contraindicated in CKD", "Glibenclamide causes hyperkalaemia in CKD patients"],
                "correct_index": 1,
                "explanation": "Glibenclamide (glyburide) has active metabolites that are renally cleared. In CKD, these accumulate causing severe, prolonged hypoglycaemia — a leading cause of hypoglycaemia deaths in elderly diabetics. ICMR/NHS/ADA guidelines recommend avoiding glibenclamide in CKD — use glipizide (short-acting, inactive metabolites) or gliclazide MR with dose adjustment. eGFR <45 = glibenclamide contraindicated."
            },
            {
                "id": "dm10", "difficulty": 5, "type": "single_select",
                "question": "A nurse visits a T1DM patient who is confused and diaphoretic. The glucometer reads 'LO' (below measurable range). The patient cannot swallow safely. The CORRECT sequence of actions is:",
                "options": ["Give 3 glucose gel sachets by mouth and call 108", "Call emergency services (108) immediately, administer 1mg glucagon IM/SC if available, place in recovery position, and do NOT give anything by mouth", "Try to force orange juice into the patient's mouth", "Give insulin to reverse the confusion — it may be DKA"],
                "correct_index": 1,
                "explanation": "Severe hypoglycaemia with impaired consciousness: NEVER give oral fluids/glucose (aspiration risk). Immediate actions: call 108, administer glucagon 1mg IM/SC (if available), place in lateral recovery position. Paramedics will give IV dextrose 50%. Do not delay emergency services for any other intervention. Giving insulin would be catastrophic."
            },
        ]
    },
    {
        "code": "TRN-POSTSURGICAL-CARE",
        "title": "Post-Operative & Post-Surgical Care",
        "description": "Monitoring surgical wounds, pain management, DVT prevention, and recovery support per NICE/RCS/ICMR guidelines.",
        "category": "Surgical Care",
        "duration_minutes": 50,
        "is_mandatory": False,
        "pass_percent": 70,
        "required_for_tiers": ["tier2", "tier3"],
        "assessment": [
            {
                "id": "ps1", "difficulty": 1, "type": "single_select",
                "question": "In the 24–48 hours after surgery, which vital sign change is MOST concerning for surgical bleeding?",
                "options": ["Temperature 37.5°C", "Heart rate increasing from 78 to 115 bpm with BP falling", "Mild confusion on waking from anaesthesia", "RR 18/min"],
                "correct_index": 1,
                "explanation": "Tachycardia with hypotension is the classic presentation of haemorrhagic shock/post-operative bleeding. Compensatory tachycardia is an early sign before BP falls severely. This requires immediate escalation as per NICE/NCEPOD guidance on post-operative monitoring."
            },
            {
                "id": "ps2", "difficulty": 2, "type": "single_select",
                "question": "Deep vein thrombosis (DVT) prophylaxis in post-surgical patients includes which primary pharmacological measure?",
                "options": ["Aspirin 75mg daily", "Low-molecular-weight heparin (LMWH) e.g. enoxaparin", "Warfarin started same day as surgery", "No pharmacological prophylaxis is required if the patient mobilises"],
                "correct_index": 1,
                "explanation": "LMWH (e.g. enoxaparin, dalteparin) is first-line pharmacological DVT prophylaxis for surgical patients per NICE NG89 and SIGN 122. It starts typically 6–12h post-op. Warfarin is used for extended prophylaxis in some orthopaedic cases but not immediate post-op. Aspirin alone is insufficient for surgical DVT risk."
            },
            {
                "id": "ps3", "difficulty": 3, "type": "single_select",
                "question": "A patient is Day 3 post-laparoscopic cholecystectomy. The nurse notices the wound appears to have separated at one end with a small amount of brownish fluid. The patient has no fever. The BEST immediate action is:",
                "options": ["Apply adhesive closure strips and reassure the patient", "Photograph the wound, clean the area gently, cover with a sterile non-adherent dressing, and contact the surgical team — possible wound dehiscence with early infection", "Pack the wound tightly with gauze", "Tell the patient this is normal and review in one week"],
                "correct_index": 1,
                "explanation": "Wound dehiscence with brownish fluid (may indicate infected seroma or early wound breakdown) requires surgical team notification. Document, photograph, and cover. 'Suture spit' and minor dehiscence may be manageable, but infected/deeper dehiscence requires surgical evaluation. Never delay reporting and never close a potentially infected wound."
            },
            {
                "id": "ps4", "difficulty": 3, "type": "single_select",
                "question": "A patient 5 days post-total knee replacement reports unilateral calf pain, warmth, and mild swelling. The nurse should:",
                "options": ["Massage the calf to reduce swelling", "Elevate the leg, do NOT massage, and urgently notify the physician — DVT suspected", "Apply a cold compress and recommend ibuprofen", "Encourage the patient to walk more — it is likely stiffness from immobility"],
                "correct_index": 1,
                "explanation": "Unilateral calf pain, warmth, and oedema within 90 days of surgery are classic DVT warning signs (Wells criteria: recent surgery = +1, unilateral leg swelling = +1, calf tenderness = +1). NEVER massage a suspected DVT — risk of embolisation. Elevate, immobilise, notify physician urgently for Doppler ultrasound. This is a potential surgical emergency (NICE NG89)."
            },
            {
                "id": "ps5", "difficulty": 4, "type": "single_select",
                "question": "A patient is recovering at home after a Whipple procedure (pancreaticoduodenectomy). Day 6, they report upper abdominal pain and a 'gushing' sensation. The drain (if present) output suddenly increases to 500mL of blood-stained fluid. The nurse should:",
                "options": ["Clamp the drain to reduce output and contact the surgeon at next review", "Call emergency services immediately — this is a post-pancreatectomy haemorrhage (PPH), a life-threatening surgical emergency", "Reassure the patient — post-operative drain output increases normally at Day 6", "Replace the dressing and re-assess in 4 hours"],
                "correct_index": 1,
                "explanation": "Sudden high-volume blood-stained drain output at Day 6 post-Whipple = Sentinel bleed / Post-Pancreatectomy Haemorrhage (PPH) — ISGPS Grade B/C. This is a life-threatening emergency. Call 108 immediately. PPH is one of the most feared complications of pancreatic surgery with mortality up to 30%. Immediate emergency surgical intervention is required."
            },
        ]
    },
]


async def seed_training_modules(session) -> int:
    """Seed training modules with MCQ questions — idempotent."""
    created = 0
    for data in TRAINING_MODULES:
        exists = await session.execute(
            select(TrainingModule).where(TrainingModule.code == data["code"])
        )
        if exists.scalar_one_or_none():
            print(f"  · training module {data['code']} already exists, skipping")
            continue
        assessment = data.pop("assessment", [])
        module = TrainingModule(
            **data,
            assessment=assessment,
            is_active=True,
            version=1,
            status=ContentStatus.published,
            published_version=1,
        )
        session.add(module)
        created += 1
        print(f"  + created training module {data['code']} ({len(assessment)} questions)")
    return created


FAQS = [
    dict(audience="consumer", category="Bookings", question="How do I book a nurse?",
         answer="Go to Bookings → New Booking and fill in the details.", display_order=1),
    dict(audience="consumer", category="Bookings", question="Can I cancel a booking?",
         answer="Yes, you can cancel up to 2 hours before the scheduled time.", display_order=2),
    dict(audience="consumer", category="Patients", question="How do I add a patient?",
         answer="Go to Patients → Add Patient and fill in the form.", display_order=3),
    dict(audience="consumer", category="Trust & Safety", question="How are nurses verified?",
         answer="All nurses are background-checked and licensed before onboarding.", display_order=4),
    dict(audience="consumer", category="Bookings", question="How do I contact my nurse?",
         answer="Open your booking and use the in-app message button to reach your nurse once they've accepted the visit.", display_order=5),
    dict(audience="consumer", category="Billing", question="What payment methods are accepted?",
         answer="Cards, UPI, and net banking via Razorpay. Payment is collected after you confirm a booking.", display_order=6),
    dict(audience="worker", category="Assignments", question="How do I claim a booking?",
         answer="Open Assignments and tap Claim on any open booking. The first nurse to claim wins it — claims are first-come, first-served.", display_order=1),
    dict(audience="worker", category="Training", question="Why can't I claim higher-tier bookings?",
         answer="Higher-tier and specialised bookings require passing the relevant training assessment first. Check Training & Certifications for what's required.", display_order=2),
    dict(audience="worker", category="Payments", question="When do I get paid?",
         answer="Payouts are processed in batches after a visit is marked complete. Check Earnings for your payout history and status.", display_order=3),
    dict(audience="worker", category="Account", question="How do I go online/offline?",
         answer="Use the availability toggle on your home screen. You must be an approved nurse/caregiver to go online.", display_order=4),
    dict(audience="all", category="Account", question="How do I reset my password?",
         answer="Use 'Forgot password' on the login screen — you'll get a reset code by email.", display_order=1),
]


async def seed_faqs(session) -> int:
    """Seed default help-center FAQs — idempotent (matches on question text)."""
    created = 0
    for data in FAQS:
        exists = await session.execute(
            select(Faq).where(Faq.question == data["question"], Faq.audience == data["audience"])
        )
        if exists.scalar_one_or_none():
            continue
        session.add(Faq(**data, is_active=True))
        created += 1
    if created:
        print(f"  + created {created} FAQ entries")
    return created


async def _run_pending_column_migrations():
    """Small, safe, idempotent ALTER TABLE fixes that must run before the app
    serves traffic. Each statement uses IF NOT EXISTS / WHERE-guarded UPDATE,
    so re-running on every startup is harmless."""
    from sqlalchemy import text

    async with engine.begin() as conn:
        await conn.execute(text(
            "ALTER TABLE bookings ADD COLUMN IF NOT EXISTS dispatch_started_at TIMESTAMPTZ NULL"
        ))
        await conn.execute(text(
            "UPDATE bookings SET dispatch_started_at = created_at "
            "WHERE dispatch_started_at IS NULL AND status NOT IN ('draft', 'pending_payment')"
        ))
    print("Column migrations: bookings.dispatch_started_at ensured")


async def main():
    print("NurseConnect seed runner")
    print("=" * 50)

    print("\nRunning pending column migrations...")
    await _run_pending_column_migrations()

    async with AsyncSessionLocal() as session:
        print("\nSeeding services...")
        services_created = await seed_services(session)

        print("\nSeeding care packages...")
        packages_created = await seed_packages(session)

        print("\nSeeding training modules...")
        training_created = await seed_training_modules(session)

        print("\nSeeding FAQs...")
        faqs_created = await seed_faqs(session)

        print("\nSeeding in-visit questionnaires (checklist templates)...")
        checklists_created = await seed_checklist_templates(session)

        print("\nLinking checklist templates onto services...")
        checklists_linked = await link_service_checklists(session)

        await session.commit()

    print("\n" + "=" * 50)
    print(
        f"Done. {services_created} services, {packages_created} packages, "
        f"{training_created} training modules, {faqs_created} FAQs, "
        f"{checklists_created} checklist templates created, "
        f"{checklists_linked} service-checklist links created."
    )
    if (
        services_created == 0 and packages_created == 0 and training_created == 0
        and faqs_created == 0 and checklists_created == 0 and checklists_linked == 0
    ):
        print("(Everything already existed — database was already seeded.)")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nSeed run FAILED: {e}", file=sys.stderr)
        sys.exit(1)