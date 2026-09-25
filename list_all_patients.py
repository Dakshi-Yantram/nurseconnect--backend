"""Debug helper — lists every patient in the database with its EXACT stored
name (in quotes, so trailing/leading spaces or odd characters are visible),
plus which consumer account owns it. Use this if find_patient.py isn't
matching a name you know exists in the app.

Run from the backend project root:
    python list_all_patients.py
"""
import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.models.models import Patient, ConsumerProfile, User
from sqlalchemy import select


async def main():
    async with AsyncSessionLocal() as session:
        res = await session.execute(select(Patient).order_by(Patient.created_at.desc()))
        patients = res.scalars().all()

        print(f"Found {len(patients)} patient record(s) total:\n")
        for p in patients:
            cres = await session.execute(
                select(ConsumerProfile).where(ConsumerProfile.id == p.consumer_id)
            )
            consumer = cres.scalar_one_or_none()
            user = None
            if consumer:
                ures = await session.execute(select(User).where(User.id == consumer.user_id))
                user = ures.scalar_one_or_none()

            print(f'name="{p.full_name}" | id={p.id} | created_at={p.created_at}')
            if user:
                print(f'  owner phone={user.phone_e164} | owner name={user.full_name}')
            print()

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())