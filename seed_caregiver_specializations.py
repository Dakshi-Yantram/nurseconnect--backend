"""Seeds one ServiceCatalogue row per caregiver specialization
(CAREGIVER_SPECIALIZATION_GROUPS in app/core/provider_types.py), so the
existing three-gate qualification engine (WorkerServiceQualification /
WorkerServicePreference / can_worker_receive_service in
app/services/qualification.py) can be reused for specializations instead of
building a second, parallel qualification system.

This is intentionally a standalone script, not folded into app/seed.py's
SERVICES list, because:
  - it's generated programmatically from provider_types.py (single source
    of truth for the specialization catalogue), not hand-listed
  - it's meant to be run once per environment as a deliberate step, same
    as the add_provider_type_schema*.py migration scripts

Each row:
  - service_code = spec_<key>  (e.g. 'spec_baby_massage'), via
    specialization_service_code() — the exact convention documented in
    provider_types.py
  - category = micro_visit (specializations are opt-in add-ons, not
    standalone shift/live-in bookings)
  - allowed_provider_types = ["caregiver", "mother_baby_caregiver"] for
    every group except "mother_and_baby", which is
    ["mother_baby_caregiver"] only — the other groups (elder_care,
    personal_care, accompaniment, other) are general caregiver work.
  - gate = credential_only — no separate training/assessment content
    exists per specialization yet; change a row's gate to
    theory_verified/practical_verified once such content exists for that
    specific specialization.
  - price/duration/commission are placeholder defaults (see PLACEHOLDER_*
    below) since the spec doesn't define per-specialization pricing yet —
    an admin can edit these via PUT /admin/services/{id} (or the website's
    Services admin screen once built) without re-running this script.

Safe to re-run: skips any service_code that already exists (same
select-before-insert pattern as app/seed.py).
"""
import asyncio

from app.core.database import AsyncSessionLocal
from app.core.provider_types import CAREGIVER_SPECIALIZATION_GROUPS, specialization_service_code
from app.models.enums import BillingTrigger, QualificationGate, ServiceCategory, WorkerTier
from app.models.models import ServiceCatalogue
from sqlalchemy import select

# Placeholder defaults — every specialization starts identical here; adjust
# per-row afterwards via the admin Services screen / PUT /admin/services/{id}.
PLACEHOLDER_DURATION_MINUTES = 60
PLACEHOLDER_BASE_PRICE = "499.00"
PLACEHOLDER_COMMISSION_PCT = "20.00"

# Groups whose specializations are Mother & Baby Caregiver only, vs. general
# caregiver work available to both caregiver types.
MOTHER_BABY_ONLY_GROUPS = {"mother_and_baby"}


async def seed_specializations(session) -> int:
    created = 0
    for group_key, group in CAREGIVER_SPECIALIZATION_GROUPS.items():
        allowed_types = (
            ["mother_baby_caregiver"]
            if group_key in MOTHER_BABY_ONLY_GROUPS
            else ["caregiver", "mother_baby_caregiver"]
        )
        for item_key, item_label in group["items"].items():
            code = specialization_service_code(item_key)
            exists = await session.execute(
                select(ServiceCatalogue).where(ServiceCatalogue.service_code == code)
            )
            if exists.scalar_one_or_none():
                print(f"  · {code} already exists, skipping")
                continue
            session.add(ServiceCatalogue(
                service_code=code,
                name=item_label,
                description=f"Caregiver specialization: {item_label} ({group['label']})",
                category=ServiceCategory.micro_visit,
                min_tier=WorkerTier.tier1,
                duration_minutes=PLACEHOLDER_DURATION_MINUTES,
                base_price=PLACEHOLDER_BASE_PRICE,
                commission_pct=PLACEHOLDER_COMMISSION_PCT,
                billing_trigger=BillingTrigger.on_completion,
                insurance_covered=False,
                gate=QualificationGate.credential_only,
                allowed_provider_types=allowed_types,
                is_active=True,
                version=1,
            ))
            created += 1
            print(f"  + created {code} ({item_label}) -> allowed_provider_types={allowed_types}")
    return created


async def main():
    async with AsyncSessionLocal() as session:
        created = await seed_specializations(session)
        await session.commit()
    print(f"\nDone. {created} new specialization service(s) created.")
    print("Existing PUT /workers/me/service-preferences and /me/service-eligibility "
          "endpoints (workers.py, untouched by this script) should now work for "
          "these service_codes without further code changes — verify by opting a "
          "test caregiver worker into one and confirming can_worker_receive_service() "
          "in app/services/qualification.py returns eligible.")


if __name__ == "__main__":
    asyncio.run(main())
