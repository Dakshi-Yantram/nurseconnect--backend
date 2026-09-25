"""Shared fixtures for NurseConnect backend tests."""
import os
import pytest
import requests

# Backend public URL
BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

# Direct DB access, for test-isolation cleanup only (see
# _release_shared_worker_schedules below). PG_TEST_DSN is the existing env
# var already set by CI and read by test_patch3_proximity.py,
# test_phase3_nurse_flow.py, test_patch5a_e2e.py and test_calls.py.
TEST_DB_DSN = os.environ.get(
    "PG_TEST_DSN", "postgresql://nurseconnect:nurseconnect@127.0.0.1:5432/nurseconnect"
)

CONSUMER_PHONE = "+919999000001"
WORKER_PHONE = "+919999000002"
ADMIN_OPS_PHONE = "+919999000003"
ADMIN_SUPER_PHONE = "+919999000004"
ADMIN_FINANCE_PHONE = "+919999000005"
ADMIN_CLINICAL_PHONE = "+919999000006"
# All four staff phones are seeded as `admin` (see tests/backend_test.py
# lines 61-64), and /auth/otp/verify authenticates an existing phone under
# its STORED role regardless of what's requested here -- so the value below
# only has to be a valid enum member to get past /auth/otp/send's 422.
#
# The upshot: these four fixtures all return an `admin` session, so any test
# asserting role SEPARATION between them (e.g.
# test_admin_ops_cannot_access_reviewer_endpoints in test_patch5a.py) is not
# actually testing anything and will fail. Fixing that properly means seeding
# distinct roles in backend_test.py AND updating the existing rows in the CI
# database -- tracked separately.
ROLE_OPS = "admin"
ROLE_SUPER = "admin"
ROLE_CLINICAL = "admin"
ROLE_FINANCE = "admin"

def _login(phone: str, role: str) -> dict:
    s = requests.Session()
    r = s.post(f"{API}/auth/otp/send", json={"phone_e164": phone, "role": role}, timeout=10)
    assert r.status_code == 200, f"otp/send failed: {r.status_code} {r.text}"
    r = s.post(
        f"{API}/auth/otp/verify",
        json={
            "phone_e164": phone,
            "code": "123456",
            "role": role,
            "device_id": "pytest-cli",
            "device_platform": "cli",
        },
        timeout=10,
    )
    assert r.status_code == 200, f"otp/verify failed: {r.status_code} {r.text}"
    return r.json()


@pytest.fixture(scope="session")
def api():
    return API


@pytest.fixture(scope="session")
def consumer_auth():
    return _login(CONSUMER_PHONE, "consumer")


@pytest.fixture(scope="session")
def worker_auth():
    return _login(WORKER_PHONE, "worker")


@pytest.fixture(scope="session")
def admin_ops_auth():
    return _login(ADMIN_OPS_PHONE, ROLE_OPS)


@pytest.fixture(scope="session")
def admin_super_auth():
    return _login(ADMIN_SUPER_PHONE, ROLE_SUPER)


@pytest.fixture(scope="session")
def admin_finance_auth():
    """Finance-permission fixture.

    `admin_finance` no longer exists as a role, so this signs in as `admin`.
    That is a placeholder, not a considered decision: if finance duties were
    split out to `operations` (or dropped entirely), the tests depending on
    this fixture are asserting against permissions that may no longer be
    modelled the way they assume.
    """
    return _login(ADMIN_FINANCE_PHONE, ROLE_FINANCE)


@pytest.fixture(scope="session")
def admin_clinical_auth():
    return _login(ADMIN_CLINICAL_PHONE, ROLE_CLINICAL)


def auth_headers(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['tokens']['access_token']}"}


# ===========================================================================
# Shared-worker schedule cleanup
#
# Many test files (test_phase6_hardening.py, test_phase4_realtime_payment.py,
# test_phase3_nurse_flow.py, etc.) all book the SAME worker (WORKER_PHONE)
# for the SAME fixed slot (date.today()+2 days, "11:00:00"), because the
# worker account is expensive to set up (onboarding/approval) and is shared
# on purpose.
#
# The problem: app/services/dispatch.py::worker_has_schedule_conflict()
# correctly refuses to let a worker accept two overlapping bookings. A test
# that intentionally never reaches checkout — e.g.
# test_checkout_400_when_checklist_missing, which expects a 400 — leaves its
# booking sitting in `in_progress` forever. Any later test anywhere in the
# suite that tries to accept a NEW booking for the same worker at the same
# slot then gets a real, correct 409 WORKER_SCHEDULE_CONFLICT — which looks
# like a cascade of unrelated failures, but is actually one test's leftover
# state poisoning every test after it.
#
# This is a test-isolation gap, not an application bug: worker_has_schedule_
# conflict is working exactly as designed. The fix belongs here, not in the
# dispatch guard.
# ===========================================================================
_OCCUPYING_BOOKING_STATUSES = ("assigned", "worker_en_route", "worker_arrived", "in_progress")

# Every phone a test file uses as "the worker" who accepts/checks in/out of
# a booking. Add to this list if a new test file introduces another one.
_SHARED_TEST_WORKER_PHONES = (WORKER_PHONE,)


def _release_worker_schedule(phone: str) -> None:
    """Force any of this worker's occupying bookings to `cancelled`.

    Talks to the DB directly because there is no (and should be no) real
    user-facing "un-assign my booking" action — this only exists to undo
    what a previous test left behind. Best-effort: a test's own setup must
    never fail because this cleanup step couldn't reach the DB in some
    environment, so any error here is swallowed.
    """
    try:
        import psycopg

        with psycopg.connect(TEST_DB_DSN, connect_timeout=5) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE bookings b
                    SET status = 'cancelled'
                    FROM worker_profiles wp, users u
                    WHERE b.worker_id = wp.id
                      AND wp.user_id = u.id
                      AND u.phone_e164 = %s
                      AND b.status::text = ANY(%s)
                    """,
                    (phone, list(_OCCUPYING_BOOKING_STATUSES)),
                )
            conn.commit()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _release_shared_worker_schedules():
    """Runs before every test in the suite (autouse, function-scoped).

    Guarantees every test starts with the shared test worker(s) free to
    accept a new booking, regardless of what any earlier test — in this
    file or any other — left dangling. See the block comment above for why
    this exists.
    """
    for phone in _SHARED_TEST_WORKER_PHONES:
        _release_worker_schedule(phone)
    yield