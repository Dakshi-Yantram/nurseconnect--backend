# NurseConnect Backend — Operations & Deployment

Production-grade FastAPI backend for the NurseConnect healthcare marketplace.

## Stack
- **Runtime:** Python 3.11, FastAPI 0.110, Uvicorn
- **Storage:** PostgreSQL 16 (async SQLAlchemy 2.0 / asyncpg), Redis 7
- **Background:** Celery + Celery Beat (4 scheduled jobs)
- **Auth:** JWT (access + rotating refresh), bcrypt, RBAC across 6 roles
- **Realtime:** native FastAPI WebSocket
- **External providers (abstracted):** Razorpay, Cloudinary, MSG91, Interakt, Firebase Push, ABHA

## Local dev (Docker Compose)

```bash
cd /app && docker compose up --build
# API: http://localhost:8001/api/health
```

## Local dev (Emergent preview)

Already wired: Postgres + Redis run inside the container; backend auto-starts both and seeds the DB on boot.

```bash
sudo supervisorctl restart backend
curl http://localhost:8001/api/health        # liveness + DB + Redis probe
/root/.venv/bin/python /app/backend/scripts/smoke_test.py    # full E2E
```

## Production deployment — AWS App Runner + RDS + ElastiCache

1. **Provision** RDS PostgreSQL 16 + ElastiCache Redis 7 in same VPC.
2. **Build & push** the image:
   ```bash
   docker build -t <ECR_REPO>:latest backend
   docker push <ECR_REPO>:latest
   ```
3. **Configure App Runner** service from ECR image. Mount env vars from `.env.production.example` template.
4. **Run migrations** (first boot only; tables auto-created via `Base.metadata.create_all` in seed). For zero-downtime migrations later, add Alembic revisions.
5. **Spin up Celery worker + beat** as separate App Runner services (or ECS tasks) using same image with overridden `CMD`:
   ```
   celery -A app.workers.celery_app worker --loglevel=info
   celery -A app.workers.celery_app beat   --loglevel=info
   ```
6. **Health check path:** `GET /api/health` — returns 200 only when DB + Redis are both reachable.

## Scheduled background jobs (Celery Beat)
| Task | Schedule | Purpose |
|------|----------|---------|
| `escalation_sla_check` | every 5 min | Flag escalations past SLA, re-notify |
| `detect_missed_visits` | every minute | Mark assigned bookings past grace as `missed` |
| `process_payout_batch` | nightly 02:00 UTC | Move pending payouts into processing |
| `retention_cleanup` | nightly 03:00 UTC | Honour data_retention_schedules |

## RBAC summary
| Role | Capabilities |
|------|--------------|
| `consumer` | Manage profile/patients/family, create/cancel bookings, pay, view tracking, rate, view care notes/abha, raise escalations indirectly |
| `worker` | Profile, kit, documents, availability, accept booking, visit lifecycle, vitals/meds/checklist, manual escalate, training, earnings |
| `admin_ops` | Worker approval, rematch, escalation triage, dashboard |
| `admin_clinical` | Clinical config (rules/templates), escalation triage |
| `admin_finance` | Ledger, refunds, payouts |
| `admin_super` | Everything (incl. worker suspension) |

## Security checklist for production
- [ ] Rotate `JWT_SECRET_KEY` to a fresh 64-byte hex
- [ ] Set `OTP_DEV_MODE=false`
- [ ] Set `MOCK_EXTERNAL_PROVIDERS=false`
- [ ] Lock `CORS_ORIGINS` to your exact app domains
- [ ] TLS everywhere (App Runner does this automatically)
- [ ] Configure WAF in front of App Runner for OWASP top-10 protection
- [ ] Enable RDS encryption at rest + automated backups
- [ ] Set up CloudWatch alarms on `/api/health` 503s and Celery DLQ
- [ ] Periodic JWT secret rotation (rolling — keep two keys for 24h)

## API documentation
Interactive Swagger UI: `GET /docs`
OpenAPI JSON: `GET /openapi.json`
