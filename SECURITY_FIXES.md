# NurseConnect security & quality fixes

This file is included at the root of both the backend and frontend projects.
The mobile screen-capture hook is at `mobile/useScreenCaptureProtection.ts` inside the backend zip
(the mobile repo was not provided; adapt its imports to your app).

Verification done: all Python files compile; all changed TS/TSX files parse; `src/lib/api.ts`
type-checks standalone. NOT run: the pytest suite (needs Postgres + Redis) and a full frontend build.
Run both before deploying:
    backend:  pytest tests/ -v
    frontend: npm ci && npm run build && npm run lint

## 0. Before deploying (manual — cannot be done in code)
1. Rotate the RDS password and JWT_SECRET_KEY (both were committed). Rotating the JWT secret logs everyone out once.
2. These updated projects no longer contain `.env.prod` or `test-env.ps1`. Also purge them from your git history:  `git filter-repo --path .env.prod --path test-env.ps1 --invert-paths`
3. On the server `.env` (see new `.env.example`) set at least:
       APP_ENV=production
       MOCK_EXTERNAL_PROVIDERS=false
       RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / RAZORPAY_WEBHOOK_SECRET
       CORS_ORIGINS=<your exact frontend origin(s)>
       CORS_ORIGIN_REGEX=^https://nurseconnect-frontend\.<your-subdomain>\.workers\.dev$   (if served from workers.dev)
       TRUSTED_PROXY_HOPS=1        (CloudFront -> app; 2 if there is also an ALB/nginx that appends XFF)
       RUN_SEED_ON_STARTUP=true    ONLY if you still rely on app.seed creating tables on boot
       INTERAKT_WEBHOOK_SECRET
   The app now REFUSES TO START in production if mock mode is on, Razorpay secrets are missing,
   the JWT secret is < 32 chars, or CORS is a wildcard.
4. Restrict the EC2 security group so port 8000 only accepts traffic from CloudFront.

## Breaking changes to coordinate
- **Mobile app login**: `POST /api/auth/phone-login` now requires `code` (OTP from `POST /api/auth/otp/send`).
  Ship the mobile update together with, or before, the backend.
- **Uploaded photos**: `/api/uploads/documentation/<file>` now needs `Authorization: Bearer <token>`.
  React Native: `<Image source={{ uri, headers: { Authorization: `Bearer ${token}` } }} />`.
- **Sessions**: logout / password reset / refresh rotation now revoke the access token immediately.
- **Bookings**: past dates/times (IST) and dates > 365 days ahead are rejected with 422.
- **Phone numbers**: invalid numbers now return 400 `INVALID_PHONE`.

## What changed
### Data security (backend)
| Issue | File(s) |
|---|---|
| Phone login without OTP | `app/api/v1/auth.py` (`_consume_phone_otp`, `phone_login`), `app/schemas/schemas.py` |
| IDOR: prescriptions, invoice, safety checklist | `app/security/access_control.py` (`assert_can_view_booking_records`), `eprescriptions.py`, `composite_care.py` |
| Public clinical-photo URLs, no type/size limit, unsanitised filename | `app/main.py` (mount removed), `app/api/v1/care_workflow.py` (validated upload + authenticated download) |
| Mock payments/e-sign could be on in prod; empty webhook secret accepted | `app/integrations/providers.py`, `app/core/config.py` (`fatal_config_errors`), `app/main.py` |
| X-Forwarded-For spoofing bypassed rate limits | `app/core/rate_limit.py`, `app/core/config.py` |
| CORS: any *.workers.dev with credentials; `*` default | `app/main.py`, `app/core/config.py` |
| Access token valid after logout/reset | `app/api/v1/auth.py` (`sid` claim), `app/core/deps.py`, `app/api/v1/auth_password_reset.py` |
| WebSockets accepted refresh/download tokens, ignored suspension | `app/api/v1/tracking.py` |
| WhatsApp webhook fail-open | `app/api/v1/whatsapp_webhooks.py` |
| Re-registration hijack of unverified accounts; unthrottled resend | `app/api/v1/auth.py` |
| Destructive test scripts runnable against prod | `set_all_prices_to_1.py`, `set_test_price_1.py`, `delete_patient_bookings.py` (refuse when APP_ENV=production unless `--i-know-this-is-production`) |
| Hard-coded passwords in scripts | `seed_admin.py`, `seed_reviewer_account.py`, `reset_*_password.py`, `approve_pending_worker.py`, `retry_assign_tickets.py` |
| /docs public, seed on every boot, JWT error text leaked, security headers | `app/main.py`, `app/core/security.py` |
| Committed secrets | `.env.prod` deleted, `.gitignore`, new `.env.example` |

### Edge cases
- Malformed token `sub` → 401 (was 500): `app/core/deps.py`
- Past / far-future bookings: `app/api/v1/bookings.py`
- Phone E.164 validation: `app/api/v1/auth.py` (+ client-side in `auth.login.tsx`)
- Lat/lng ranges, address/pincode, special_instructions, message length: `app/schemas/schemas.py`, `messaging.py`
- Upload: empty, >10 MB, wrong type, bad field id: `care_workflow.py` (+ client-side pre-check in `_app.partner.visits.$visitId.tsx`)
- Refresh when user was deleted → 401 (was 500): `auth.py`
- Password policy on reset now matches sign-up: `auth_password_reset.py`

### Screenshot protection
- Web: `ProtectedContent` now also wraps vitals on `_app.bookings.$bookingId.tsx`, vitals on
  `_app.clinical-escalation.$caseId.tsx`, patient assessment on `_app.teledoctor-queue.tsx`.
  Staff attempts are now audited too (`visits.py` client-events uses the staff-aware check).
  When the access token expires, the protected block first tries a silent token refresh instead of blanking.
  Browsers still cannot block OS screenshots — this remains watermark + blur + print-blanking + audit.
- Mobile: `mobile/useScreenCaptureProtection.ts` — real FLAG_SECURE on Android, recording block +
  screenshot audit on iOS. Backend accepts the new `screenshot_taken` event (`report_access.py`).

### Error messages (frontend)
- `src/lib/api.ts`: automatic token refresh (single-flight) + retry; FormData uploads (`apiUpload`);
  413/415/422 messages; support reference on 5xx; Retry-After header honoured.
- `src/lib/auth-context.tsx`: sign-out revokes the server session and clears tokens; auto sign-out on refresh failure.
- `_app.support-dashboard.tsx`, `lib/domain/index.tsx`, `_app.care-packages.tsx`, `_app.services-catalogue.tsx`,
  `_app.consumer.bookings.tsx`: local fetch helpers now go through the shared client (no more
  "[object Object]" / raw JSON toasts, and they get token refresh).
- `VisitStartOtp.tsx`: used a relative URL with no auth header (calls never reached the API); fixed.
- `_app.partner.visits.$visitId.tsx`: uploads via `apiUpload`; errors via `apiErrorMessage`.

## Known limitations (not changed, by design)
- Web tokens remain in `localStorage` (moving to httpOnly cookies is an architecture change across
  web + mobile). Mitigations: token refresh with rotation, immediate revocation on logout/reset.
- Web pages cannot block OS screenshots; only the mobile app can (see the mobile hook).
- JWT_ACCESS_TOKEN_EXPIRE_MINUTES default is still 1440 so older mobile builds without refresh keep
  working; set 60 in `.env` once the mobile app refreshes tokens.
