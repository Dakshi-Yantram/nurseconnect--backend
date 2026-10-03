#!/usr/bin/env python3
"""Safety check. Refuses to pass unless this process points ONLY at UAT resources.

Run it by hand, and systemd runs it before every UAT service start (ExecStartPre).
It reads the same .env the app reads (environment variables win over the file,
exactly like the app's settings) and imports nothing from the app.
"""
import os
import sys
from urllib.parse import urlsplit

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FILL = "__FILL_ME__"


def read_env(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path, encoding="utf-8"):
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


FILE = read_env(os.path.join(ROOT, ".env"))


def get(key, default=""):
    return os.environ.get(key, FILE.get(key, default))


def path_of(url):
    return urlsplit(url).path.lstrip("/")


problems = []

if not FILE:
    problems.append(f"{os.path.join(ROOT, '.env')} not found or empty")

if get("APP_ENV").strip().lower() != "staging":
    problems.append(f"APP_ENV is '{get('APP_ENV')}', must be 'staging'")

for label in ("DATABASE_URL", "DATABASE_URL_SYNC"):
    url = get(label)
    if path_of(url) != "nurseconnect_uat":
        problems.append(f"{label} points at database '{path_of(url)}', must be 'nurseconnect_uat'")
    if "neon.tech" in url:
        problems.append(f"{label} points at Neon (production)")

for label, want in (("REDIS_URL", "10"), ("CELERY_BROKER_URL", "11"), ("CELERY_RESULT_BACKEND", "12")):
    url = get(label)
    if path_of(url) != want:
        problems.append(f"{label} uses Redis db '{path_of(url)}', must be '{want}'")
    if "upstash.io" in url:
        problems.append(f"{label} points at Upstash (production)")

if not get("RAZORPAY_KEY_ID").startswith("rzp_test_"):
    problems.append("RAZORPAY_KEY_ID must be a TEST key (rzp_test_...)")
for key in ("RAZORPAYX_KEY_ID", "RAZORPAYX_KEY_SECRET", "RAZORPAYX_ACCOUNT_NUMBER"):
    if get(key):
        problems.append(f"{key} is set: payouts must be disabled in UAT")
if get("MOCK_EXTERNAL_PROVIDERS").strip().lower() in ("1", "true", "yes", "on"):
    problems.append("MOCK_EXTERNAL_PROVIDERS must be false for a staging app")
if len(get("JWT_SECRET_KEY")) < 32:
    problems.append("JWT_SECRET_KEY is missing or shorter than 32 characters")

for key in ("RAZORPAY_KEY_SECRET", "MSG91_AUTH_KEY", "MSG91_TEMPLATE_ID", "RESEND_API_KEY", "EMAIL_FROM_ADDRESS"):
    v = get(key)
    if not v or FILL in v:
        problems.append(f"{key} is not filled in")

if get("CONTRACT_STAGE2_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off"):
    print("note: CONTRACT_STAGE2_ENABLED is on (Stage 2 / Digio will fail in UAT)")

if problems:
    print("PREFLIGHT FAILED:")
    for p in problems:
        print("  -", p)
    sys.exit(1)
print("preflight OK: UAT database, UAT Redis dbs, Razorpay test key, payouts off")
