"""
ONE-SHOT FIX — finds the "hunny" user, creates a WorkerProfile for them if
missing, approves onboarding, sets tier3, and qualifies + opts them into
every service. Also does the same for any OTHER worker-role user missing
a profile, so this issue doesn't recur for other test accounts either.

USAGE (from backend/ folder):

    python fix_hunny_worker.py
"""
import asyncio
from datetime import datetime, timezone

from app.core.database import AsyncSessionLocal, engine
from app.models.models import (
    User, WorkerProfile, ServiceCatalogue,
    WorkerServiceQualification, WorkerServicePreference,
)
from app.models.enums import (
    UserRole, WorkerOnboardingStatus, WorkerTier,
    WorkerQualificationStatus, WorkerQualificationSource, WorkerPreferenceStatus,
)
from sqlalchemy import select, func


async def qualify_worker(session, worker, services):
    worker.onboarding_status = WorkerOnboardingStatus.approved
    worker.tier = WorkerTier.tier3
    for service in services:
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
        qual.valid_from = datetime.now(timezone.utc)

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


async def main():
    async with AsyncSessionLocal() as session:
        sres = await session.execute(select(ServiceCatalogue))
        services = sres.scalars().all()
        if not services:
            print("No services found — run run_seed.py first.")
            return

        # 1. Find every worker-role user missing a profile (includes "hunny" if worker role)
        ures = await session.execute(select(User).where(User.role == UserRole.worker))
        worker_users = ures.scalars().all()

        fixed_any = False
        for u in worker_users:
            wres = await session.execute(select(WorkerProfile).where(WorkerProfile.user_id == u.id))
            profile = wres.scalar_one_or_none()
            if profile:
                continue
            print(f"Creating missing WorkerProfile for {u.full_name or u.email or u.phone_e164} ({u.id})")
            profile = WorkerProfile(user_id=u.id)
            session.add(profile)
            await session.flush()  # get profile.id
            await qualify_worker(session, profile, services)
            fixed_any = True

        # 2. Also check: is "hunny" perhaps NOT role=worker at all? Report it either way.
        hres = await session.execute(
            select(User).where(func.lower(User.full_name).like("%hunny%"))
        )
        hunny_users = hres.scalars().all()
        if not hunny_users:
            print("\nNote: no user with full_name containing 'hunny' was found at all.")
        else:
            for u in hunny_users:
                print(f"\n'hunny' user found: id={u.id}, role={u.role.value}, email={u.email}, phone={u.phone_e164}")
                if u.role != UserRole.worker:
                    print("  -> This account's role is NOT 'worker' in the backend.")
                    print("     That's why /worker/new-requests 404s no matter what we fix on the worker side.")
                else:
                    wres = await session.execute(select(WorkerProfile).where(WorkerProfile.user_id == u.id))
                    profile = wres.scalar_one_or_none()
                    print(f"  -> worker_profile: {'now created/fixed above' if not profile else 'already existed'}")

        # 3. Also qualify the 2 workers that already had profiles (idempotent, harmless)
        allw = await session.execute(select(WorkerProfile))
        for wp in allw.scalars().all():
            await qualify_worker(session, wp, services)

        await session.commit()

    print("\nDone. Every worker-role user now has a WorkerProfile, approved, tier3,")
    print("and qualified/opted-in for every service. Refresh /partner/assignments.")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())