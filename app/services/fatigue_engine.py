"""Pre-visit reaction-time / fatigue safety check — decision logic.

This is deliberately kept separate from the API layer (app/api/v1/workers.py,
app/api/v1/bookings.py) and from the DB model so the thresholds are easy to
tune from one place without touching request handling or persistence code.

Rules encoded here (from the product spec / mockups):
  Trial:        random 1-3s wait, tap the target as soon as it turns green.
  Round count:  5 trials per attempt.
  Lapse:        any single trial reaction time > LAPSE_THRESHOLD_MS.
  False start:  a tap registered before the cue changed colour.

Decision table:
  avg < PASS_THRESHOLD_MS and lapses == 0            -> PASS
  PASS_THRESHOLD_MS <= avg <= WARNING_THRESHOLD_MS    -> WARNING (one retry)
    (also WARNING if there's exactly one lapse but avg is still under the
    warning ceiling — a single slow tap shouldn't hard-fail someone outright)
  avg > WARNING_THRESHOLD_MS or lapses > 1            -> FAIL / FATIGUED
  excessive false starts (>= FALSE_START_FAIL_THRESHOLD) also FAIL, since it
  means the nurse isn't reliably tracking the cue at all.
"""
from dataclasses import dataclass
from typing import List, Optional

from app.models.enums import AlertnessTier

# ---------------------------------------------------------------------------
# Tunable thresholds. Change these constants (or wire them up to
# app.core.config.Settings / an admin-editable table later) to retune the
# gate without touching any endpoint code.
# ---------------------------------------------------------------------------
ROUNDS_REQUIRED = 5
LAPSE_THRESHOLD_MS = 500          # single-trial delay counted as a "lapse"
PASS_THRESHOLD_MS = 380           # avg below this, zero lapses -> PASS
WARNING_THRESHOLD_MS = 450        # avg up to this -> WARNING (retry offered)
MAX_LAPSES_FOR_WARNING = 1        # more than this -> automatic FAIL
FALSE_START_FAIL_THRESHOLD = 3    # this many false starts -> automatic FAIL
RETRY_ALLOWED_ON_WARNING = True


@dataclass
class FatigueResult:
    tier: AlertnessTier
    average_reaction_time_ms: Optional[int]
    lapses_count: int
    false_starts: int
    retry_allowed: bool
    message: str


def evaluate(reaction_times_ms: List[int], false_starts: int = 0) -> FatigueResult:
    """Score one 5-round attempt against the decision table above."""
    times = [t for t in reaction_times_ms if t is not None and t >= 0]
    lapses = sum(1 for t in times if t > LAPSE_THRESHOLD_MS)
    average = int(round(sum(times) / len(times))) if times else None

    if not times:
        return FatigueResult(
            tier=AlertnessTier.fail,
            average_reaction_time_ms=None,
            lapses_count=0,
            false_starts=false_starts,
            retry_allowed=True,
            message="No valid taps recorded — please try the check again.",
        )

    if false_starts >= FALSE_START_FAIL_THRESHOLD:
        return FatigueResult(
            tier=AlertnessTier.fail,
            average_reaction_time_ms=average,
            lapses_count=lapses,
            false_starts=false_starts,
            retry_allowed=False,
            message="Too many early taps — you seem unable to focus on the cue right now. Please rest before this booking.",
        )

    if average < PASS_THRESHOLD_MS and lapses == 0:
        return FatigueResult(
            tier=AlertnessTier.ok,
            average_reaction_time_ms=average,
            lapses_count=lapses,
            false_starts=false_starts,
            retry_allowed=False,
            message="Alertness check passed.",
        )

    if average <= WARNING_THRESHOLD_MS and lapses <= MAX_LAPSES_FOR_WARNING:
        return FatigueResult(
            tier=AlertnessTier.warning,
            average_reaction_time_ms=average,
            lapses_count=lapses,
            false_starts=false_starts,
            retry_allowed=RETRY_ALLOWED_ON_WARNING,
            message="Your reaction time is a little slow. Take a 5-second breather and try once more.",
        )

    return FatigueResult(
        tier=AlertnessTier.fail,
        average_reaction_time_ms=average,
        lapses_count=lapses,
        false_starts=false_starts,
        retry_allowed=False,
        message="You seem very fatigued right now — this booking will be reassigned so you can rest.",
    )
