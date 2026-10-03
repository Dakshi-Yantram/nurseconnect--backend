"""A service address must have real coordinates.

Nurse matching, the arrival geofence and every distance check use the booking's
latitude/longitude. The app only gets coordinates from the phone's GPS, and it
sends 0/0 when an address has none (``latitude ?? 0``). Before this guard:

* the service-booking path answered with a developer message
  ("Provide address_id or address + latitude/longitude"), and
* the package-booking path accepted 0/0 silently, creating a booking at
  "null island" that no nurse can ever be matched to, with no error at all.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional, Union

Number = Union[int, float, Decimal]

MISSING_LOCATION_MESSAGE = (
    "We couldn't find the location of this address. Please open Addresses, tap "
    "'Use current location' (or pick the place on the map), save it and try again."
)


def coordinates_missing(lat: Optional[Number], lng: Optional[Number]) -> bool:
    """True when the pair cannot be a real place: absent, out of range, or 0/0."""
    if lat is None or lng is None:
        return True
    try:
        la, lo = float(lat), float(lng)
    except (TypeError, ValueError):
        return True
    if not (-90.0 <= la <= 90.0 and -180.0 <= lo <= 180.0):
        return True
    return abs(la) < 1e-6 and abs(lo) < 1e-6
