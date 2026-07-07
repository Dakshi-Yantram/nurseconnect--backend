"""Clinical safety engine — vital threshold evaluation & escalation routing.

All rules are DB-driven via clinical_rule_sets.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from app.models.enums import EscalationLevel
from app.models.models import ClinicalRuleSet

# Priority of escalation levels — higher index = more severe
_LEVEL_ORDER = ["none", "watch", "inform_family", "contact_doctor", "emergency"]


def _max_level(a: str, b: str) -> str:
    return a if _LEVEL_ORDER.index(a) >= _LEVEL_ORDER.index(b) else b


def evaluate_vitals(rule_set: ClinicalRuleSet, vitals: Dict[str, Any]) -> Tuple[List[str], str]:
    """Returns (abnormal_flags, escalation_level)."""
    thresholds = rule_set.vital_thresholds or {}
    flags: List[str] = []
    level = "none"

    def _check_high_low(metric: str, value: Optional[float], cfg: Dict[str, Any]) -> None:
        nonlocal level
        if value is None or not cfg:
            return
        v = float(value)
        if "critical_high" in cfg and v >= float(cfg["critical_high"]):
            flags.append(f"{metric}_critical_high")
            level = _max_level(level, "emergency")
        elif "warning_high" in cfg and v >= float(cfg["warning_high"]):
            flags.append(f"{metric}_warning_high")
            level = _max_level(level, "contact_doctor")
        if "critical_low" in cfg and v <= float(cfg["critical_low"]):
            flags.append(f"{metric}_critical_low")
            level = _max_level(level, "emergency")
        elif "warning_low" in cfg and v <= float(cfg["warning_low"]):
            flags.append(f"{metric}_warning_low")
            level = _max_level(level, "contact_doctor")

    _check_high_low("bp_systolic", vitals.get("bp_systolic"), thresholds.get("bp_systolic", {}))
    _check_high_low("bp_diastolic", vitals.get("bp_diastolic"), thresholds.get("bp_diastolic", {}))
    _check_high_low("pulse", vitals.get("pulse"), thresholds.get("pulse", {}))
    _check_high_low("spo2", vitals.get("spo2"), thresholds.get("spo2", {}))
    _check_high_low("temperature_f", vitals.get("temperature_f"), thresholds.get("temperature_f", {}))
    _check_high_low("respiratory_rate", vitals.get("respiratory_rate"), thresholds.get("respiratory_rate", {}))
    # Pain >=8 -> watch
    if vitals.get("pain_score") is not None and int(vitals["pain_score"]) >= 8:
        flags.append("pain_score_high")
        level = _max_level(level, "inform_family")
    # GCS <=12 -> emergency
    if vitals.get("gcs_score") is not None and int(vitals["gcs_score"]) <= 12:
        flags.append("gcs_score_low")
        level = _max_level(level, "emergency")

    return flags, level


def evaluate_red_flag_symptoms(rule_set: ClinicalRuleSet, observed_symptoms: List[str]) -> Tuple[List[str], str, List[str]]:
    """Returns (matched_flags, level, parties_to_notify)."""
    cfg = rule_set.red_flag_symptoms or []
    matched: List[str] = []
    level = "none"
    parties: List[str] = []
    observed_lower = {s.lower() for s in observed_symptoms}
    for entry in cfg:
        symptom = (entry.get("symptom") or "").lower()
        if symptom in observed_lower:
            matched.append(symptom)
            level = _max_level(level, entry.get("escalation_level", "watch"))
            for p in entry.get("notify", []):
                if p not in parties:
                    parties.append(p)
    return matched, level, parties


def get_escalation_metadata(rule_set: ClinicalRuleSet, level: str) -> Dict[str, Any]:
    if level == "none":
        return {"notify": [], "sla_minutes": None, "auto_call_112": False}
    levels = rule_set.escalation_levels or {}
    meta = levels.get(level, {})
    return {
        "notify": meta.get("notify", []),
        "sla_minutes": meta.get("sla_minutes"),
        "auto_call_112": bool(meta.get("auto_call_112", False)),
    }


def compute_sla_breach(level_minutes: Optional[int]) -> Optional[datetime]:
    if not level_minutes:
        return None
    return datetime.now(timezone.utc) + timedelta(minutes=level_minutes)


def evaluate_checklist_payload(
    rule_set: Optional[ClinicalRuleSet],
    payload_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Evaluate a batch of submitted checklist answers against a rule set.

    Recognised answer payloads:
      * ``{"value": <symptom-string-or-list>}`` — checked against
        ``red_flag_symptoms`` keywords.
      * ``{"value": {"bp_systolic": ..., "spo2": ...}}`` for ``vitals_entry``
        questions — checked against ``vital_thresholds``.

    Returns ``{abnormal_flags, escalation_triggered, escalation_level,
    notified_parties, rule_set_version}``.
    """
    flags: List[str] = []
    level = "none"
    parties: List[str] = []

    if rule_set is None:
        return {
            "abnormal_flags": [],
            "escalation_triggered": False,
            "escalation_level": "none",
            "notified_parties": [],
            "rule_set_version": None,
        }

    observed_symptoms: List[str] = []
    for item in payload_items or []:
        # Walk into the validated payload shape used by the engine.
        ans = item.get("answer_json") if isinstance(item, dict) else None
        raw_value = None
        if isinstance(ans, dict):
            raw_value = ans.get("value")

        # vitals_entry inside checklist
        if isinstance(raw_value, dict) and any(
            k in raw_value for k in ("bp_systolic", "bp_diastolic", "pulse", "spo2", "temperature_f")
        ):
            v_flags, v_level = evaluate_vitals(rule_set, raw_value)
            for f in v_flags:
                if f not in flags:
                    flags.append(f)
            level = _max_level(level, v_level)
            continue

        # text symptoms
        if isinstance(raw_value, str):
            observed_symptoms.append(raw_value)
        elif isinstance(raw_value, list):
            for v in raw_value:
                if isinstance(v, str):
                    observed_symptoms.append(v)
        elif isinstance(raw_value, dict):
            # consent_confirmation etc — pull any "symptom" key
            sym = raw_value.get("symptom")
            if isinstance(sym, str):
                observed_symptoms.append(sym)

    if observed_symptoms:
        matched, m_level, m_parties = evaluate_red_flag_symptoms(rule_set, observed_symptoms)
        for m in matched:
            tag = f"red_flag_{m.replace(' ', '_')}"
            if tag not in flags:
                flags.append(tag)
        level = _max_level(level, m_level)
        for p in m_parties:
            if p not in parties:
                parties.append(p)

    if level != "none":
        meta = get_escalation_metadata(rule_set, level)
        for p in meta.get("notify", []) or []:
            if p not in parties:
                parties.append(p)

    return {
        "abnormal_flags": flags,
        "escalation_triggered": level != "none",
        "escalation_level": level,
        "notified_parties": parties,
        "rule_set_version": rule_set.version,
    }


def evaluate_insurance_coverage(rule_set: Optional[ClinicalRuleSet], assessment: Dict[str, bool]) -> Dict[str, Any]:
    """Lightweight evaluation; returns coverage_status and exclusion reasons."""
    reasons: List[str] = []
    required_pass = ["checklist_complete", "consent_obtained", "tier_appropriate", "gps_verified", "registration_valid"]
    for key in required_pass:
        if assessment.get(key) is False:
            reasons.append(f"{key}_failed")
    if assessment.get("escalation_timely") is False:
        reasons.append("escalation_delayed")
    if assessment.get("prescription_valid") is False:
        reasons.append("prescription_invalid")

    if not reasons:
        status = "covered"
        coverage_percent = Decimal("100")
    elif len(reasons) <= 1:
        status = "conditional"
        coverage_percent = Decimal("50")
    else:
        status = "not_covered"
        coverage_percent = Decimal("0")

    return {
        "coverage_status": status,
        "coverage_percent": coverage_percent,
        "exclusion_reasons": reasons,
        "flagged_for_review": status == "conditional",
    }
