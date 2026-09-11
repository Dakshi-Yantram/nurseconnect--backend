"""Find a Patient record by (partial) name, and trace it back to the
consumer account that added them — so you can see which phone number to
log in with to see this patient.

Run from the backend project root:
    python find_patient.py "Suman"
"""
import asyncio
import sys
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Patient, ConsumerProfile, User
from sqlalchemy import select


async def main():
    name_query = sys.argv[1] if len(sys.argv) > 1 else "Suman"

    async with AsyncSessionLocal() as session:
        res = await session.execute(
            select(Patient).where(Patient.full_name.ilike(f"%{name_query}%"))
        )
        patients = res.scalars().all()

        if not patients:
            print(f"No patient found matching '{name_query}'.")
            return

        print(f"Found {len(patients)} matching patient(s):\n")
        for p in patients:
            cres = await session.execute(
                select(ConsumerProfile).where(ConsumerProfile.id == p.consumer_id)
            )
            consumer = cres.scalar_one_or_none()
            user = None
            if consumer:
                ures = await session.execute(
                    select(User).where(User.id == consumer.user_id)
                )
                user = ures.scalar_one_or_none()

            print(f"Patient: {p.full_name} (id={p.id})")
            print(f"  relationship_to_consumer: {p.relationship_to_consumer}")
            print(f"  date_of_birth: {p.date_of_birth}")
            if user:
                print(f"  → added under consumer account: phone={user.phone_e164} | name={user.full_name} | last_login={user.last_login_at}")
            else:
                print("  → could not resolve owning consumer account")
            print()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())