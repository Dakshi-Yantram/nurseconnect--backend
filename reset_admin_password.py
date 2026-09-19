import asyncio
from app.core.database import AsyncSessionLocal, engine
from app.core.security import hash_password
from app.models.models import User
from sqlalchemy import select

def _prompt_password(env_var: str) -> str:
    """Read the password from the environment or prompt for it. Never
    hard-code credentials in the repo, and never print them."""
    import getpass
    import os
    import re as _re
    pw = os.environ.get(env_var) or getpass.getpass(f"New password ({env_var}): ")
    if (len(pw) < 12 or not _re.search(r"[A-Z]", pw) or not _re.search(r"[a-z]", pw)
            or not _re.search(r"\d", pw)):
        raise SystemExit("Password must be 12+ chars with upper, lower and a digit.")
    return pw


ADMIN_EMAIL = "ops@example.com"
NEW_PASSWORD = None  # set at runtime from $ADMIN_NEW_PASSWORD or an interactive prompt
async def main():
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.email == ADMIN_EMAIL))
        user = result.scalar_one_or_none()

        if not user:
            print(f"No user found with email {ADMIN_EMAIL}")
            return

        user.password_hash = hash_password(_prompt_password("ADMIN_NEW_PASSWORD"))
        await session.commit()
        print(f"Password updated for {ADMIN_EMAIL}")

    await engine.dispose()

if __name__ == "__main__":
    asyncio.run(main())