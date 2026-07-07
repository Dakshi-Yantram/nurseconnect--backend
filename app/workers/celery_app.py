"""Celery app + beat schedule.

Tasks:
- escalation_sla_check: every 5 min, mark SLA breaches & re-notify
- process_worker_payouts: daily batch payout
- retention_cleanup: nightly purge per data_retention_schedules
- offline_sync_processor: every minute, reconcile pending queue
"""
from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

celery_app = Celery(
    "nurseconnect",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    timezone="UTC",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)

celery_app.conf.beat_schedule = {
    # escalation_sla_check: every 5 min, mark SLA breaches & re-notify
    "escalation-sla-check-every-5-min": {
        "task": "app.workers.tasks.escalation_sla_check",
        "schedule": 300.0,
    },
    # process_worker_payouts: daily batch payout
    "payout-batch-daily": {
        "task": "app.workers.tasks.process_payout_batch",
        "schedule": crontab(hour=2, minute=0),
    },
    # retention_cleanup: nightly purge per data_retention_schedules
    "retention-cleanup-nightly": {
        "task": "app.workers.tasks.retention_cleanup",
        "schedule": crontab(hour=3, minute=0),
    },
    # offline_sync_processor: every minute, reconcile pending queue
    "offline-sync-processor-every-minute": {
        "task": "app.workers.tasks.detect_missed_visits",
        "schedule": 60.0,
    },
    # visit_reminders: every 5 min
    "visit-reminders-every-5-min": {
        "task": "app.workers.tasks.send_visit_reminders",
        "schedule": 300.0,
    },

}
