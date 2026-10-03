"""What a nurse sees for a service/package she has never chosen.

Before: a missing preference row was reported as OPTED_IN for every item, even
ones the nurse is NOT qualified for. The screen then said "You are offering
this", the nurse could tap "Opt out" (always allowed), and from then on the item
was OPTED_OUT and locked: opting back in needs qualification she never had.
That is the "can opt out but cannot opt back in" trap.

Now an unchosen item is shown as opted in only when she could actually be
offered it (qualified). Otherwise it shows as not offered, i.e. locked with its
reason, and there is nothing to opt out of.
"""
from __future__ import annotations

from typing import Tuple

OPTED_IN = "OPTED_IN"
OPTED_OUT = "OPTED_OUT"


def default_preference(qualified: bool) -> Tuple[str, bool]:
    """(preference_status, willing_to_accept) for an item with no saved choice."""
    return (OPTED_IN, True) if qualified else (OPTED_OUT, False)
