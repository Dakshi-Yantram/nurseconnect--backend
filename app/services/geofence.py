"""Server-side arrival geofence for starting a visit.

The nurse app sends its GPS fix with the OTP.  Previously the backend stored
that fix as ``check_in_latitude/longitude`` and never compared it to the
customer's address, so OTP alone started a visit from anywhere.  These helpers
make the backend the authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.services.proximity import haversine_km

# Nurse must be within this distance of the customer's saved location.
DEFAULT_START_RADIUS_M = 150
# A GPS fix older than this is not evidence of where the nurse is *now*.
MAX_FIX_AGE_SECONDS = 120
# Reject implausibly imprecise fixes (metres) when the client reports accuracy.
MAX_ACCEPTED_ACCURACY_M = 200


@dataclass(frozen=True)
class GeofenceResult:
    ok: bool
    code: Optional[str] = None
    message: Optional[str] = None
    distance_m: Optional[int] = None


def _valid_coord(lat, lng) -> bool:
    try:
        lat_f, lng_f = float(lat), float(lng)
    except (TypeError, ValueError):
        return False
    if lat_f != lat_f or lng_f != lng_f:  # NaN
        return False
    if not (-90.0 <= lat_f <= 90.0 and -180.0 <= lng_f <= 180.0):
        return False
    # (0, 0) is the classic "no GPS yet" default from mobile clients.
    return not (abs(lat_f) < 1e-6 and abs(lng_f) < 1e-6)


def distance_metres(lat1, lng1, lat2, lng2) -> int:
    return int(round(haversine_km(float(lat1), float(lng1), float(lat2), float(lng2)) * 1000))


def check_arrival(
    *,
    nurse_lat,
    nurse_lng,
    customer_lat,
    customer_lng,
    radius_m: int = DEFAULT_START_RADIUS_M,
    accuracy_m: Optional[float] = None,
    fix_captured_at: Optional[datetime] = None,
    now: Optional[datetime] = None,
) -> GeofenceResult:
    """Decide whether a nurse is close enough to start the visit."""
    if not _valid_coord(customer_lat, customer_lng):
        # We cannot prove arrival without a customer location, so fail closed.
        return GeofenceResult(
            False,
            "CUSTOMER_LOCATION_UNAVAILABLE",
            "This booking has no verified customer location. Contact support to start the visit.",
        )
    if not _valid_coord(nurse_lat, nurse_lng):
        return GeofenceResult(
            False, "NURSE_LOCATION_REQUIRED",
            "Turn on location and try again — we need your current position to start the visit.",
        )

    now = now or datetime.now(timezone.utc)
    if fix_captured_at is not None:
        ts = fix_captured_at if fix_captured_at.tzinfo else fix_captured_at.replace(tzinfo=timezone.utc)
        age = (now - ts).total_seconds()
        if age > MAX_FIX_AGE_SECONDS or age < -60:
            return GeofenceResult(
                False, "NURSE_LOCATION_STALE",
                "Your location is out of date. Refresh your location and try again.",
            )
    if accuracy_m is not None and accuracy_m > MAX_ACCEPTED_ACCURACY_M:
        return GeofenceResult(
            False, "NURSE_LOCATION_INACCURATE",
            "Your GPS signal is too weak to confirm arrival. Move to an open area and retry.",
        )

    dist = distance_metres(nurse_lat, nurse_lng, customer_lat, customer_lng)
    # Allow the reported accuracy radius as tolerance, capped so a sloppy fix
    # cannot widen the geofence beyond 2x the configured radius.
    tolerance = min(float(accuracy_m or 0), float(radius_m))
    if dist > radius_m + tolerance:
        return GeofenceResult(
            False, "NOT_AT_CUSTOMER_LOCATION",
            f"You are about {dist} m from the customer's address. Reach the location to start the visit.",
            distance_m=dist,
        )
    return GeofenceResult(True, distance_m=dist)
