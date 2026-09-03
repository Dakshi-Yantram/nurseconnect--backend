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


def _login(phone: str, role: str) -> dict:
    s = requests.Session()
    r = s.post(f"{API}/auth/otp/send", json={"phone_e164": phone, "role": role}, timeout=10)
    assert r.status_code == 200, f"send-otp failed: {r.status_code} {r.text}"
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
    assert r.status_code == 200, f"verify-otp failed: {r.status_code} {r.text}"
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


# NOTE: app/models/enums.py:UserRole only defines a single "admin" role —
# there is no admin_ops/admin_super/admin_finance/admin_clinical value in
# the enum (those names only appear in docstrings/comments as an aspirational
# permission model that was never implemented). Sending role="admin_ops" etc.
# to /auth/otp/send fails Pydantic validation with 422, which fails these
# session-scoped fixtures at setup and makes every test that depends on them
# error out. Until real admin sub-roles are added to the enum + RBAC layer,
# all four fixtures log in as the single "admin" role, on distinct phone
# numbers so each still gets an independent test user/token.
@pytest.fixture(scope="session")
def admin_ops_auth():
    return _login(ADMIN_OPS_PHONE, "admin")


@pytest.fixture(scope="session")
def admin_super_auth():
    return _login(ADMIN_SUPER_PHONE, "admin")


@pytest.fixture(scope="session")
def admin_finance_auth():
    return _login(ADMIN_FINANCE_PHONE, "admin")


@pytest.fixture(scope="session")
def admin_clinical_auth():
    return _login(ADMIN_CLINICAL_PHONE, "admin")


def auth_headers(auth: dict) -> dict:
    return {"Authorization": f"Bearer {auth['tokens']['access_token']}"}