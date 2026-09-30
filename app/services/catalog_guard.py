"""Production guard for test-only / incomplete catalogue entries.

The catalogue contains rows that must never reach a real customer, e.g.
"High-Risk Service (template missing — test only)".  Those rows were visible in
``GET /catalog/services`` and could be booked and paid, because only
``is_active`` was checked.  Checkout would later fail with
CLINICAL_TEMPLATE_MISSING *after* the customer had already paid.

A catalogue item is **bookable** only when all of these hold:
  * active (and not soft-deleted, for packages)
  * not marked test-only (name / code convention) — enforced when the
    environment blocks test catalogue items (production/staging by default)
  * clinically complete: a MEDIUM/HIGH/CRITICAL item must resolve at least one
    checklist or documentation template (mirrors the checkout engine, so we
    refuse at booking time what checkout would refuse at completion time)
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from app.core.config import settings
from app.models.enums import ServiceRiskLevel

_TEST_MARKERS = re.compile(
    r"(test[\s_-]*only|template[\s_-]*missing|\bqa[\s_-]*only\b|\[\s*test\s*\]|\(\s*test\s*\)|\bdummy\b|\bsandbox\b)",
    re.IGNORECASE,
)
_TEST_CODE_PREFIXES = ("TEST_", "TEST-", "TMP_", "TMP-", "QA_", "QA-", "ZZ_", "ZZ-")
_NEEDS_TEMPLATE = {ServiceRiskLevel.MEDIUM, ServiceRiskLevel.HIGH, ServiceRiskLevel.CRITICAL}


def block_test_catalog() -> bool:
    """Test-only rows are hidden everywhere except local development, unless
    explicitly allowed (ALLOW_TEST_CATALOG_ITEMS=true, used by the test-suite)."""
    if getattr(settings, "ALLOW_TEST_CATALOG_ITEMS", False):
        return False
    return bool(settings.is_production)


def is_test_only(item) -> bool:
    """Name/code convention for rows that exist only for testing."""
    if item is None:
        return False
    name = getattr(item, "name", "") or ""
    code = (getattr(item, "service_code", None) or getattr(item, "package_code", None) or "")
    if _TEST_MARKERS.search(name) or _TEST_MARKERS.search(code):
        return True
    return code.upper().startswith(_TEST_CODE_PREFIXES)


def has_clinical_template(item, *, fallback_items: Iterable = ()) -> bool:
    for it in (item, *fallback_items):
        if it is not None and (
            getattr(it, "checklist_template_id", None)
            or getattr(it, "documentation_template_id", None)
        ):
            return True
    return False


def is_incomplete(item, *, fallback_items: Iterable = ()) -> bool:
    risk = getattr(item, "risk_level", None)
    if risk in _NEEDS_TEMPLATE:
        return not has_clinical_template(item, fallback_items=fallback_items)
    return False


def unbookable_reason(item, *, fallback_items: Iterable = ()) -> Optional[str]:
    """Return a stable machine code when ``item`` must not be sold, else None."""
    if item is None:
        return "NOT_FOUND"
    if getattr(item, "is_deleted", False):
        return "ITEM_DELETED"
    if not getattr(item, "is_active", True):
        return "ITEM_INACTIVE"
    if block_test_catalog() and is_test_only(item):
        return "ITEM_TEST_ONLY"
    if block_test_catalog() and is_incomplete(item, fallback_items=fallback_items):
        return "ITEM_TEMPLATE_MISSING"
    return None


def is_publicly_visible(item, *, fallback_items: Iterable = ()) -> bool:
    return unbookable_reason(item, fallback_items=fallback_items) is None
