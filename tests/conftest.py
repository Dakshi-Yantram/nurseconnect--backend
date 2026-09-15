"""Shared fixtures for NurseConnect backend tests."""
import os
import pytest
import requests

# Backend public URL
BASE_URL = os.environ.get("EXPO_PUBLIC_BACKEND_URL", "http://localhost:8001").rstrip("/")
API = f"{BASE_URL}/api"

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