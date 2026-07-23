"""
One-off script: force-qualify a test worker for a specific service, bypassing
the full training/assessment/certificate system in app/services/qualification.py.

This is for TEST/DEV environments only. It directly sets the three things
can_worker_receive_service() checks for:
  1. WorkerProfile.onboarding_status -> approved
  2. WorkerServiceQualification row -> qualification_status APPROVED
  3. WorkerServicePreference row -> OPTED_IN + willing_to_accept=True

USAGE (from backend/ folder, same place as run_seed.py / seed_admin.py):

    python seed_worker_qualification.py

Edit WORKER_EMAIL and SERVICE_CODE below before running if your test
worker/service differ.
"""
import asyncio
import sys
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.models.models import (
    User,
    WorkerProfile,
    ServiceCatalogue,
    WorkerServiceQualification,
    WorkerServicePreference,
)
from app.models.enums import (
    WorkerOnboardingStatus,
    WorkerQualificationStatus,
    WorkerQualificationSource,
    WorkerPreferenceStatus,
    WorkerTier,
)
from sqlalchemy import select


WORKER_EMAIL = "testworker@yantram.com"
SERVICE_CODE = "WOUND_DRESSING"   # matches the seeded service in app/seed.py


async def main():
    print("NurseConnect worker qualification seed runner")
    print("=" * 50)

    async with AsyncSessionLocal() as session:
        # 1. Find the worker's User row, then their WorkerProfile
        ures = await session.execute(select(User).where(User.email == WORKER_EMAIL))
        user = ures.scalar_one_or_none()
        if not user:
            print(f"FAILED: no user found with email {WORKER_EMAIL}")
            return

        wres = await session.execute(
            select(WorkerProfile).where(WorkerProfile.user_id == user.id)
        )
        worker = wres.scalar_one_or_none()
        if not worker:
            print(f"FAILED: no WorkerProfile found for user_id={user.id}")
            return

        print(f"Found worker profile id={worker.id} (user email={user.email})")

        # 2. Force onboarding_status -> approved, and bump tier so tier checks pass
        worker.onboarding_status = WorkerOnboardingStatus.approved
        worker.tier = WorkerTier.tier3  # generous tier so most seeded services pass tier_ok
        print(f"  + set onboarding_status=approved, tier=tier3")

        # 3. Find the service
        sres = await session.execute(
            select(ServiceCatalogue).where(ServiceCatalogue.service_code == SERVICE_CODE)
        )
        service = sres.scalar_one_or_none()
        if not service:
            print(f"FAILED: no service found with service_code={SERVICE_CODE}")
            return
        print(f"Found service id={service.id} ({service.name})")

        # 4. Upsert WorkerServiceQualification -> APPROVED
        qres = await session.execute(
            select(WorkerServiceQualification).where(
                WorkerServiceQualification.worker_id == worker.id,
                WorkerServiceQualification.service_id == service.id,
            )
        )
        qual = qres.scalar_one_or_none()
        if not qual:
            qual = WorkerServiceQualification(
                worker_id=worker.id,
                service_id=service.id,
            )
            session.add(qual)
            print("  + created new WorkerServiceQualification row")
        else:
            print("  · found existing WorkerServiceQualification row, updating")

        qual.qualification_status = WorkerQualificationStatus.APPROVED
        qual.qualification_source = WorkerQualificationSource.TRAINING
        qual.admin_approved_by = user.id  # self-reference, fine for test data
        qual.admin_approved_at = datetime.now(timezone.utc)
        qual.valid_from = datetime.now(timezone.utc)
        print("  + set qualification_status=APPROVED")

        # 5. Upsert WorkerServicePreference -> OPTED_IN + willing_to_accept
        pres = await session.execute(
            select(WorkerServicePreference).where(
                WorkerServicePreference.worker_id == worker.id,
                WorkerServicePreference.service_id == service.id,
            )
        )
        pref = pres.scalar_one_or_none()
        if not pref:
            pref = WorkerServicePreference(
                worker_id=worker.id,
                service_id=service.id,
            )
            session.add(pref)
            print("  + created new WorkerServicePreference row")
        else:
            print("  · found existing WorkerServicePreference row, updating")

        pref.preference_status = WorkerPreferenceStatus.OPTED_IN
        pref.willing_to_accept = True
        print("  + set preference_status=OPTED_IN, willing_to_accept=True")

        await session.commit()

    print("\n" + "=" * 50)
    print(f"Done. Worker {WORKER_EMAIL} is now qualified, approved, and opted-in for {SERVICE_CODE}.")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nQualification seed run FAILED: {e}", file=sys.stderr)
        sys.exit(1)