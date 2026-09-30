"""Single source of truth for how a booking's wall-clock slot maps to an instant.

ROOT CAUSE (timezone bug): ``Booking.scheduled_date`` + ``scheduled_start_time``
are the wall-clock slot the customer picked on screen, i.e. IST
(``_validate_schedule`` in bookings.py has always compared them in IST).  But
``dispatch._window``, ``bookings._scheduled_start_utc`` and the Celery tasks
``detect_missed_visits`` / ``send_visit_reminders`` all attached
``tzinfo=timezone.utc`` to that same wall-clock value.  Every one of those was
therefore wrong by 5h30m: schedule-conflict checks compared shifted windows,
"missed visit" fired 5.5 hours late, reminders fired 5.5 hours late and the
cancellation cut-off was evaluated against the wrong instant.

Everything now goes through ``slot_start_utc`` / ``slot_end_utc`` so there is
exactly one convention.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

# India has no DST, so a fixed offset is exact (and avoids a tzdata dependency
# in minimal containers).
IST = timezone(timedelta(hours=5, minutes=30))

# Small grace so a customer who taps "Pay" a few seconds after the slot minute
# ticks over is not rejected, mirroring _validate_schedule's booking-time grace.
SLOT_GRACE = timedelta(minutes=5)


def slot_start_utc(scheduled_date: date, scheduled_start_time: Optional[time]) -> datetime:
    """The instant a booking slot starts, as an aware UTC datetime."""
    start_time = scheduled_start_time if scheduled_start_time is not None else time(0, 0)
    local = datetime.combine(scheduled_date, start_time).replace(tzinfo=IST)
    return local.astimezone(timezone.utc)


def slot_end_utc(
    scheduled_date: date,
    scheduled_start_time: Optional[time],
    duration_minutes: Optional[int],
) -> datetime:
    return slot_start_utc(scheduled_date, scheduled_start_time) + timedelta(
        minutes=int(duration_minutes or 60)
    )


def booking_start_utc(booking) -> datetime:
    return slot_start_utc(booking.scheduled_date, booking.scheduled_start_time)


def booking_end_utc(booking) -> datetime:
    return slot_end_utc(
        booking.scheduled_date,
        booking.scheduled_start_time,
        getattr(booking, "scheduled_duration_minutes", None),
    )


def is_slot_expired(
    scheduled_date: date,
    scheduled_start_time: Optional[time],
    *,
    now: Optional[datetime] = None,
    grace: timedelta = SLOT_GRACE,
) -> bool:
    """True when the slot start (+grace) is already in the past.

    A booking with no start time is treated as expiring at the end of its IST
    calendar day, so a same-day "any time" booking is still payable today.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if scheduled_start_time is None:
        day_end = datetime.combine(scheduled_date, time(23, 59, 59)).replace(tzinfo=IST)
        return now > day_end.astimezone(timezone.utc)
    return now > slot_start_utc(scheduled_date, scheduled_start_time) + grace


def is_booking_expired(booking, *, now: Optional[datetime] = None) -> bool:
    return is_slot_expired(
        booking.scheduled_date, booking.scheduled_start_time, now=now
    )


def is_booking_upcoming(booking, *, now: Optional[datetime] = None) -> bool:
    """A slot is 'upcoming' only while it has not yet ended (start + duration)."""
    now = now or datetime.now(timezone.utc)
    return booking_end_utc(booking) >= now


_TERMINAL_STATUSES = {"completed", "cancelled", "missed", "disputed"}
_ACTIVE_STATUSES = {"worker_en_route", "worker_arrived", "in_progress"}


def time_bucket(booking, now: Optional[datetime] = None) -> str:
    """'upcoming' | 'active' | 'past' — the server's truth for the app tabs.

    ROOT CAUSE: the apps bucketed on status alone, so an unpaid/unassigned
    booking whose slot had already passed (status pending_payment/confirmed)
    still showed as Upcoming.
    """
    now = now or datetime.now(timezone.utc)
    status = getattr(booking.status, "value", booking.status)
    if status in _TERMINAL_STATUSES:
        return "past"
    if status in _ACTIVE_STATUSES:
        return "active"
    if is_booking_expired(booking, now=now):
        # An assigned visit whose slot is still running is active, not past.
        if status == "assigned" and getattr(booking, "worker_id", None) is not None \
                and booking_end_utc(booking) >= now:
            return "active"
        return "past"
    return "upcoming"
