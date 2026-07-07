"""
PATCH 5 — Standalone seed runner.

Seeds ServiceCatalogue and CarePackage rows so the consumer app has
something bookable. Safe to re-run: every insert is guarded by a
"does this code already exist?" check, so running it twice will not
create duplicates or raise unique-constraint errors.

USAGE (on the server, inside the backend virtualenv):

    cd /var/app/current        # or wherever backend/ lives
    python run_seed.py

This file must sit in the same directory as the `app` package
(i.e. backend/run_seed.py, next to backend/app/, backend/main.py).
"""
import asyncio
import sys
from decimal import Decimal

from app.core.database import AsyncSessionLocal, engine, Base
from app.models.models import ServiceCatalogue, CarePackage
from app.models.enums import (
    ServiceCategory,
    WorkerTier,
    BillingTrigger,
    GenderRestriction,
    VisitFrequency,
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


async def main():
    print("NurseConnect seed runner")
    print("=" * 50)
    async with AsyncSessionLocal() as session:
        print("\nSeeding services...")
        services_created = await seed_services(session)

        print("\nSeeding care packages...")
        packages_created = await seed_packages(session)

        await session.commit()

    print("\n" + "=" * 50)
    print(f"Done. {services_created} services created, {packages_created} packages created.")
    if services_created == 0 and packages_created == 0:
        print("(Everything already existed — database was already seeded.)")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nSeed run FAILED: {e}", file=sys.stderr)
        sys.exit(1)