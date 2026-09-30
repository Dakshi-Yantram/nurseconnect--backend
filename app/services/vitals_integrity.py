"""Vitals integrity: never store or print a measurement nobody took.

ROOT CAUSES
* ``POST /visits/{id}/vitals`` accepted a payload with every field null and
  stored it as a "reading", so ``latest_vitals`` (and the PDF/summary) treated
  an empty row as "vitals were recorded".
* No plausibility limits: 0/0, SpO2 400, pulse -5 were all stored.
* A ``vitals_entry`` checklist/documentation item counted as complete with any
  non-empty dict (even ``{"notes": ""}``-style stubs).
* ``render_family_summary`` substituted "—" / "TBD" placeholders and the
  fallback template asserted "Care delivered as planned" regardless of what
  was recorded.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import HTTPException

# field -> (min, max) inclusive, physiologically possible bounds. These reject
# typos and placeholder zeros; they are NOT clinical alert thresholds (those
# live in the ClinicalRuleSet).
LIMITS: Dict[str, Tuple[float, float]] = {
    "bp_systolic": (40, 300),
    "bp_diastolic": (20, 200),
    "pulse": (20, 300),
    "spo2": (30, 100),
    "temperature_f": (85, 115),
    "respiratory_rate": (4, 80),
    "blood_sugar_fasting": (10, 1000),
    "blood_sugar_random": (10, 1000),
    "weight_kg": (0.3, 500),
    "pain_score": (0, 10),
    "gcs_score": (3, 15),
}
MEASUREMENT_FIELDS = tuple(LIMITS.keys())


def _num(v: Any) -> Optional[float]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        return float(v)
    return None


def recorded_fields(values: Mapping[str, Any]) -> List[str]:
    """Fields that carry a real (non-null) measurement."""
    return [k for k in MEASUREMENT_FIELDS if _num(values.get(k)) is not None]


def validate_vitals(values: Mapping[str, Any]) -> List[Dict[str, str]]:
    """Return a list of {field, message} problems (empty = valid)."""
    problems: List[Dict[str, str]] = []
    present = recorded_fields(values)
    if not present:
        return [{"field": "*", "message": "Record at least one measurement."}]
    for k in present:
        v = _num(values.get(k))
        lo, hi = LIMITS[k]
        if v is None or v < lo or v > hi:
            problems.append({"field": k, "message": f"{k} must be between {lo:g} and {hi:g}."})
    # A single BP number IS accepted. Rejecting a reading because only one half
    # of the blood pressure was captured would also reject whatever else came
    # with it (e.g. a critical SpO2) and stop the emergency escalation that is
    # driven by saved vitals. Reports already print a half BP as "(incomplete)".
    # Only the ordering is checked, and only when both numbers are present.
    s, d = _num(values.get("bp_systolic")), _num(values.get("bp_diastolic"))
    if s is not None and d is not None and s <= d:
        problems.append({"field": "bp_systolic", "message": "Systolic must be higher than diastolic."})
    return problems


def assert_valid_vitals(values: Mapping[str, Any]) -> None:
    problems = validate_vitals(values)
    if problems:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "VITALS_INVALID",
                "message": "Vitals were not saved: " + problems[0]["message"],
                "problems": problems,
            },
        )


def vitals_dict_is_meaningful(answer: Any) -> bool:
    """For ``vitals_entry`` checklist/documentation answers."""
    if not isinstance(answer, dict):
        return False
    return bool(recorded_fields(answer)) and not validate_vitals(answer)
