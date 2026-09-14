"""
Fixes the user whose `role` column got set to the invalid value
'admin_super' (which doesn't exist in the UserRole enum), causing
every query that touches the `users` table (e.g. /admin/patients,
/admin/consumers) to crash with:

    LookupError: 'admin_super' is not among the defined enum values.

Run this ON THE SAME MACHINE/ENV that has the correct DATABASE_URL
(same as where you ran check_schema.py):

    python fix_bad_role.py

It uses raw SQL so it bypasses the Python enum validation that is
currently blocking this row from even being read.
"""
import asyncio
from app.core.database import engine
from sqlalchemy import text

USER_ID = "d7e3698e-e162-4f36-af3b-4dbe8cb4cd54"  # Dakshi Gupta

async def main():
    async with engine.begin() as conn:
        # See current (invalid) value first
        res = await conn.execute(
            text("SELECT id, full_name, role::text FROM users WHERE id = :uid"),
            {"uid": USER_ID},
        )
        row = res.first()
        if not row:
            print("User not found.")
            return
        print(f"Found user: {row}")

        # Set it back to a valid role. Change 'admin' below if you
        # intended a different role for this user.
        await conn.execute(
            text("UPDATE users SET role = 'admin' WHERE id = :uid"),
            {"uid": USER_ID},
        )
        print("Updated role to 'admin'.")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())