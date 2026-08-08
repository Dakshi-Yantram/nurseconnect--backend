"""
Generic dev/test script — qualifies EVERY worker for EVERY service, so no
matter which worker ID logs in, they can see and claim any booking.

Unlike seed_worker_qualification.py (which is hardcoded to one worker +
one service), this loops over ALL WorkerProfile rows and ALL
ServiceCatalogue rows and force-approves each combination. Safe to re-run
(upserts, no duplicates).

For each worker, this sets:
  1. WorkerProfile.onboarding_status -> approved
  2. WorkerProfile.tier              -> tier3 (highest, passes every tier check)
  3. WorkerServiceQualification      -> APPROVED  (for every service)
  4. WorkerServicePreference         -> OPTED_IN + willing_to_accept=True (for every service)

⚠️  DEV/TEST ONLY. Do not run against production — this bypasses the real
    training/certification/approval workflow entirely.

USAGE (from backend/ folder, same place as run_seed.py):

    python qualify_all_workers.py
"""
import asyncio
import sys
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.models.models import (
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


async def main():
    print("NurseConnect — qualify ALL workers for ALL services")
    print("=" * 55)

    async with AsyncSessionLocal() as session:
        wres = await session.execute(select(WorkerProfile))
        workers = wres.scalars().all()
        if not workers:
            print("No workers found. Nothing to do.")
            return

        sres = await session.execute(select(ServiceCatalogue))
        services = sres.scalars().all()
        if not services:
            print("No services found. Run run_seed.py first to seed the catalogue.")
            return

        print(f"Found {len(workers)} worker(s) and {len(services)} service(s).")
        print(f"That's {len(workers) * len(services)} qualification rows to upsert.\n")

        now = datetime.now(timezone.utc)

        for worker in workers:
            worker.onboarding_status = WorkerOnboardingStatus.approved
            worker.tier = WorkerTier.tier3
            print(f"Worker {worker.id}: onboarding=approved, tier=tier3")

            for service in services:
                # -- qualification --
                qres = await session.execute(
                    select(WorkerServiceQualification).where(
                        WorkerServiceQualification.worker_id == worker.id,
                        WorkerServiceQualification.service_id == service.id,
                    )
                )
                qual = qres.scalar_one_or_none()
                if not qual:
                    qual = WorkerServiceQualification(worker_id=worker.id, service_id=service.id)
                    session.add(qual)
                qual.qualification_status = WorkerQualificationStatus.APPROVED
                qual.qualification_source = WorkerQualificationSource.TRAINING
                qual.admin_approved_by = None
                qual.admin_approved_at = now
                qual.valid_from = now

                # -- preference (opt-in) --
                pres = await session.execute(
                    select(WorkerServicePreference).where(
                        WorkerServicePreference.worker_id == worker.id,
                        WorkerServicePreference.service_id == service.id,
                    )
                )
                pref = pres.scalar_one_or_none()
                if not pref:
                    pref = WorkerServicePreference(worker_id=worker.id, service_id=service.id)
                    session.add(pref)
                pref.preference_status = WorkerPreferenceStatus.OPTED_IN
                pref.willing_to_accept = True

            print(f"  + qualified + opted-in for all {len(services)} services")

        await session.commit()

    print("\n" + "=" * 55)
    print("Done. Every worker is now approved, tier3, and qualified/opted-in")
    print("for every service. Refresh /partner/assignments — all open")
    print("bookings should now be claimable regardless of which worker ID you use.")

    await engine.dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)