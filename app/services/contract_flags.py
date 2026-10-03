"""Switch for the Stage 2 (Master Agreement / Digio e-Sign) step.

CONTRACT_STAGE2_ENABLED=false  ->  the app shows only Stage 1 (in-app OTP
clickwrap). Stage 2 is reported as "not_applicable" (the app already greys out
that tab), and the e-Sign start endpoint refuses. Nothing is deleted: nurses who
already signed Stage 2 keep their "accepted" record, and setting the flag back to
true restores the previous behaviour exactly.

NOTE (business effect): the ₹ onboarding enablement fee is deducted from payouts
only while a Stage 2 agreement exists, so with Stage 2 off it is not collected.
"""
from __future__ import annotations

from typing import Optional

STAGE2_OFF_REASON = "The Master Agreement is not required at this time."
STAGE2_LOCKED_REASON = "Complete your first booking to unlock the Master Agreement."


def stage2_available(completed_visits_count: Optional[int], enabled: bool) -> bool:
    """True when a nurse may start Stage 2."""
    return bool(enabled) and (completed_visits_count or 0) >= 1


def stage2_reason(completed_visits_count: Optional[int], enabled: bool) -> Optional[str]:
    """Why Stage 2 is not actionable, or None when it is."""
    if not enabled:
        return STAGE2_OFF_REASON
    if (completed_visits_count or 0) < 1:
        return STAGE2_LOCKED_REASON
    return None
