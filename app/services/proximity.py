"""Patch 3 — Lightweight proximity helpers.

Pure-Python Haversine + wave-radius logic. No external geo services, no paid
Google Maps backend APIs. Designed to be called inline from the existing
booking-request filtering path; we deliberately keep this module zero-cost so
qualification + opt-in filtering (Patch 2) remains the dominant cost.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Tuple

# ----- Wave configuration -----------------------------------------------------
# Per Patch 3 spec. Tuple of (radius_km) per wave for normal vs urgent bookings.
NORMAL_WAVE_RADII_KM: Tuple[int, int, int] = (5, 8, 12)
URGENT_WAVE_RADII_KM: Tuple[int, int, int] = (3, 5, 8)

# Wave progression thresholds (minutes since booking creation).
# Normal: wave 1 first 5 min, wave 2 next 5 min, wave 3 next 10 min, escalated after 20 min.
# Urgent : tighter (2 / 4 / 7 min then escalated).
NORMAL_WAVE_TIME_MIN: Tuple[int, int, int] = (5, 10, 20)
URGENT_WAVE_TIME_MIN: Tuple[int, int, int] = (2, 4, 7)

# Worker current-location freshness window — anything older than this falls
# back to the worker's home location.
CURRENT_LOCATION_FRESHNESS_MINUTES = 15


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two WGS-84 points.

    Returns ``inf`` if any coordinate is ``None``/missing — callers can treat
    that as "out of range" without special-casing.
    """
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return float("inf")
    # Coerce Decimal → float; SQLAlchemy Numeric columns hand us Decimal.
    if isinstance(lat1, Decimal):
        lat1 = float(lat1)
    if isinstance(lon1, Decimal):
        lon1 = float(lon1)
    if isinstance(lat2, Decimal):
        lat2 = float(lat2)
    if isinstance(lon2, Decimal):
        lon2 = float(lon2)
    r = 6371.0088  # Earth radius (km), mean per IUGG
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r * c


def effective_origin_for_worker(worker) -> Optional[Tuple[float, float]]:
    """Return (lat, lng) to use for distance calculations.

    Preference order:
      1. Current location if recorded within ``CURRENT_LOCATION_FRESHNESS_MINUTES``.
      2. Home location (``home_latitude/home_longitude``).
      3. None — caller must decide fallback (city/zone) and exclusion rules.
    """
    cur_lat = getattr(worker, "current_latitude", None)
    cur_lng = getattr(worker, "current_longitude", None)
    cur_ts = getattr(worker, "current_location_updated_at", None)
    if cur_lat is not None and cur_lng is not None and cur_ts is not None:
        try:
            now = datetime.now(timezone.utc)
            ts = cur_ts if cur_ts.tzinfo else cur_ts.replace(tzinfo=timezone.utc)
            age_min = (now - ts).total_seconds() / 60.0
            if age_min <= CURRENT_LOCATION_FRESHNESS_MINUTES:
                return (float(cur_lat), float(cur_lng))
        except Exception:
            # Bad timestamp — fall through to home.
            pass
    home_lat = getattr(worker, "home_latitude", None)
    home_lng = getattr(worker, "home_longitude", None)
    if home_lat is not None and home_lng is not None:
        return (float(home_lat), float(home_lng))
    return None


def radius_for_wave(wave: int, is_urgent: bool) -> Optional[int]:
    """Radius (km) for a given wave.

    Past the last wave the booking is flagged as escalated for admin
    attention (assignment_escalated_at), but it STAYS visible to workers at
    the widest radius — returning ``None`` here used to hide it from every
    worker forever, so any booking not claimed within ~20 minutes could
    only be rescued by manual admin assignment.
    """
    table = URGENT_WAVE_RADII_KM if is_urgent else NORMAL_WAVE_RADII_KM
    if 1 <= wave <= len(table):
        return table[wave - 1]
    if wave > len(table):
        return table[-1]  # escalated: keep serving at max radius
    return None  # wave < 1 — defensive, never produced by compute_current_wave


def compute_current_wave(booking, now: Optional[datetime] = None) -> int:
    """Compute which wave this booking is in given elapsed minutes since it
    became dispatchable. Returns 1..3 or 4 (escalated/past last wave).

    The clock starts at ``dispatch_started_at`` (set when payment is
    captured and the booking flips to confirmed) — falling back to
    ``created_at`` only for legacy rows that predate the column. Using
    created_at unconditionally meant the wave window was already burning
    while the consumer was still paying.

    Spec: progress opportunistically when worker new-requests endpoint is
    called — so this is a pure function of (now - clock start, is_urgent).
    """
    if booking is None:
        return 1
    started = getattr(booking, "dispatch_started_at", None) or booking.created_at
    if started is None:
        return 1
    if now is None:
        now = datetime.now(timezone.utc)
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    elapsed_min = (now - started).total_seconds() / 60.0
    thresholds = URGENT_WAVE_TIME_MIN if booking.is_urgent else NORMAL_WAVE_TIME_MIN
    if elapsed_min < thresholds[0]:
        return 1
    if elapsed_min < thresholds[1]:
        return 2
    if elapsed_min < thresholds[2]:
        return 3
    return 4  # past last wave → ESCALATED
