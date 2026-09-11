"""Centralised company / statutory identity used on every invoice and payout
statement.

Everything that appears in an invoice *header* or as a statutory citation is
defined exactly once, here, and read from settings so it can be changed per
environment without touching a template. No PDF template hardcodes any of it.

Nothing in this module is booking-specific — amounts never live here.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

# NOTE: `settings` is imported lazily inside get_company() rather than at module
# scope. The statutory constants below are plain tax facts with no configuration
# behind them, and pricing_engine.py imports them — keeping this module free of
# pydantic/env-file dependencies is what lets the money maths be unit-tested
# without a database or a loaded environment.


@dataclass(frozen=True)
class CompanyIdentity:
    legal_name: str
    address_line: str
    gstin: str
    state_name: str
    state_code: str
    support_email: str

    @property
    def place_of_supply(self) -> str:
        """e.g. 'Telangana (36)' — printed on every invoice."""
        return f"{self.state_name} ({self.state_code})"

    @property
    def header_line(self) -> str:
        return f"{self.address_line} | GSTIN: {self.gstin}"


def get_company() -> CompanyIdentity:
    from app.core.config import settings

    return CompanyIdentity(
        legal_name=settings.COMPANY_LEGAL_NAME,
        address_line=settings.COMPANY_ADDRESS_LINE,
        gstin=settings.COMPANY_GSTIN,
        state_name=settings.COMPANY_STATE_NAME,
        state_code=settings.COMPANY_STATE_CODE,
        support_email=settings.COMPANY_SUPPORT_EMAIL,
    )


# ---------------------------------------------------------------------------
# Statutory constants. These are tax-law facts, not tunable business numbers,
# so they live in code rather than settings — but still in exactly one place.
# ---------------------------------------------------------------------------

#: GST rate applied to every taxable line (platform fees, consumables, kits,
#: non-medical caregiving). Exempt lines carry 0 and cite the notification below.
STANDARD_GST_RATE = Decimal("18")

#: GST rate on the B2B platform technology fee the platform bills the nurse.
PLATFORM_FEE_GST_RATE = Decimal("18")

#: Cited on every 0%-rated healthcare line.
GST_EXEMPTION_NOTE = (
    "Exempt per Notification No. 12/2017-Central Tax (Rate), Entry 74"
)

#: SAC codes. Defaults only — a PricingComponent row may override per line.
SAC_NURSING_EXEMPT = "999314"          # paramedical / home clinical care
SAC_TELECONSULT_EXEMPT = "999312"      # medical consultation
SAC_NON_MEDICAL_CARE = "999334"        # social & personal domestic assistance
SAC_CONSUMABLES = "998599"             # support services / handling
SAC_PLATFORM_FEE_B2B = "998314"        # platform technology fee billed to partner

#: Footer note on customer receipts that carry an exempt healthcare line — this
#: is what keeps the marketplace position defensible.
MARKETPLACE_NOTE = (
    "Healthcare services marked exempt above are delivered directly by an "
    "independent, qualified practitioner. The platform acts as an electronic "
    "commerce marketplace."
)

#: Pure-agent citation for statutory recoveries (e-stamp advance) on the
#: partner payout statement.
PURE_AGENT_NOTE = "Pure Agent per Rule 33, CGST Rules"
