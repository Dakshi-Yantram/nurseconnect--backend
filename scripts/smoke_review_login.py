#!/usr/bin/env python3
"""Post-deploy smoke test: existing login -> Partner Agreement -> dashboard.

Run it against the deployed API BEFORE and AFTER a release with the existing
Google-Play review credentials. It only signs in ONCE (the login endpoint is
rate-limited / lockout-protected) and afterwards issues read-only GETs, so it
cannot change credentials, data or sessions.

    export NC_API=https://<your-api-host>/api
    export NC_EMAIL='<review login email>'
    export NC_PASSWORD='<review login password>'
    python scripts/smoke_review_login.py

Credentials are read from the environment only; they are never written to
disk or printed. Exit code 0 = every check passed; 1 = something failed.

Failure = login rejected, or any endpoint answering 5xx / not JSON where JSON
is expected. 403/404 on role-specific endpoints are reported as INFO (a Staff
account legitimately has no worker bookings, for example).
"""
from __future__ import annotations

import os
import sys

import requests

API = os.environ.get("NC_API", "").rstrip("/")
EMAIL = os.environ.get("NC_EMAIL", "")
PASSWORD = os.environ.get("NC_PASSWORD", "")
TIMEOUT = 30

failures: list[str] = []


def report(ok: bool, label: str, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
    if not ok:
        failures.append(label)


def info(label: str, detail: str) -> None:
    print(f"[INFO] {label} - {detail}")


def main() -> int:
    if not (API and EMAIL and PASSWORD):
        print("Set NC_API, NC_EMAIL and NC_PASSWORD (see the docstring).", file=sys.stderr)
        return 2

    # 1. Existing login endpoint, existing payload shape (unchanged).
    r = requests.post(f"{API}/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=TIMEOUT)
    if r.status_code != 200:
        report(False, "POST /auth/login", f"HTTP {r.status_code}")
        return 1
    body = r.json()
    token = (body.get("tokens") or {}).get("access_token")
    role = (body.get("user") or {}).get("role")
    report(bool(token), "POST /auth/login", f"role={role}, token issued")
    if not token:
        return 1
    h = {"Authorization": f"Bearer {token}"}

    def get(path: str, expect_json: bool = True, role_specific: bool = False):
        try:
            resp = requests.get(f"{API}{path}", headers=h, timeout=TIMEOUT)
        except requests.RequestException as e:
            report(False, f"GET {path}", f"network error: {e.__class__.__name__}")
            return None
        if resp.status_code >= 500:
            report(False, f"GET {path}", f"HTTP {resp.status_code} (server error)")
            return None
        if resp.status_code in (401,):
            report(False, f"GET {path}", "HTTP 401 - session/token not accepted")
            return None
        if resp.status_code in (403, 404) and role_specific:
            info(f"GET {path}", f"HTTP {resp.status_code} (not applicable to this role)")
            return None
        ok = resp.status_code == 200
        if ok and expect_json:
            try:
                data = resp.json()
            except ValueError:
                report(False, f"GET {path}", "200 but body is not JSON")
                return None
            report(True, f"GET {path}", "HTTP 200")
            return data
        report(ok, f"GET {path}", f"HTTP {resp.status_code}")
        return None

    # 2. Session / token handling.
    me = get("/auth/me")
    if me is not None:
        report(me.get("role") == role, "session role matches login role", f"role={me.get('role')}")

    # 3. Partner Agreement (Stage 1 / Stage 2 status).
    stages = get("/contracts/me", role_specific=True)
    if isinstance(stages, list):
        for s in stages:
            info("agreement stage", f"stage={s.get('stage')} status={s.get('status')} unlocked={s.get('unlocked')}")

    # 4. Dashboard data the app loads after the agreement step.
    get("/workers/me", role_specific=True)
    get("/workers/me/onboarding", role_specific=True)
    get("/bookings/worker", role_specific=True)
    get("/bookings/worker/new-requests", role_specific=True)   # rewritten in this release
    get("/training/modules", role_specific=True)
    get("/notifications", role_specific=True)

    print()
    if failures:
        print(f"SMOKE FAILED ({len(failures)}): " + "; ".join(failures))
        return 1
    print("SMOKE OK: login, session, agreement and dashboard endpoints all healthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
