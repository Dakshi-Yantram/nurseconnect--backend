"""THE single pricing / GST / commission calculation for the platform.

Every rupee the customer is charged, every rupee a nurse earns, and every
rupee of GST is computed here and nowhere else. Booking creation, the
customer invoice, the admin payout screen and the nurse payout statement all
call into this module, so they can never disagree with one another.

Deliberately dependency-free: `Decimal` and stdlib only. No SQLAlchemy, no
FastAPI. That makes the money maths unit-testable without a database, which
is the whole reason it lives apart from `pricing_resolver.py` (the DB glue).

--------------------------------------------------------------------------
The rate card
--------------------------------------------------------------------------
A booking is priced from a list of `PricingComponent` rows — the modular
"charges table". One row per chargeable element, e.g.

  | label                | input   | basis         | gst | earns_to |
  |----------------------|---------|---------------|-----|----------|
  | Injection Admin.     |  480.00 | earning_rate  |  0% | worker   |
  | Consumables Kit      |  150.00 | customer_rate | 18% | platform |
  | Care Protection Fee  |   35.40 | customer_rate | 18% | platform |

Change a number in that table and the customer price, the nurse's take-home,
the GST and the admin breakdown all move together. Nothing else to edit.

`basis` is what makes one table serve both pricing styles:

  earning_rate   the input IS the nurse's net take-home. The customer price
                 is grossed UP so the platform's commission fits inside it:
                     customer_value = net / (1 - commission%)
                 With net=480 and 20%: customer_value = 600, platform = 120.
                 This is the style the invoice templates use.

  customer_rate  the input IS the customer-facing price. The commission is
                 taken OUT of it:
                     platform_fee = customer_value * commission%
                 With 600 and 20%: nurse nets 480. This is how bookings
                 priced off ServiceCatalogue.base_price have always worked,
                 so it stays the default and legacy bookings don't move.

Both directions are exact inverses, so an offering can be configured either
way and the 80/20 split holds.

--------------------------------------------------------------------------
GST
--------------------------------------------------------------------------
Rates are per-component, never global: an exempt paramedical nursing line and
an 18% consumables line sit side by side on one invoice. A line declares its
own rate, and 0% lines carry the exemption citation.

Two invariants the rounding is built to preserve, because auditors check them:
  * taxable + gst == gross, exactly, to the paisa;
  * cgst + sgst == gst, exactly (the odd paisa goes to SGST).

--------------------------------------------------------------------------
Visibility
--------------------------------------------------------------------------
`CustomerView` exposes only what the patient may see: a consolidated line
value, its GST and the line total. The commission split is structurally
absent from it — not hidden by a flag, simply not present in the object.
`AdminView` carries the complete calculation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Literal, Optional

from app.core.company import (
    GST_EXEMPTION_NOTE,
    PLATFORM_FEE_GST_RATE,
    STANDARD_GST_RATE,
)

Basis = Literal["earning_rate", "customer_rate"]
EarnsTo = Literal["worker", "platform"]

ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


def money(value: Decimal | int | float | str) -> Decimal:
    """Round to paisa, half-up. The only rounding rule in the system."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def split_cgst_sgst(gst: Decimal) -> tuple[Decimal, Decimal]:
    """Halve GST into CGST/SGST so the two always re-add to the total.

    An odd number of paisa can't be halved evenly (₹137.29 -> 68.645), so
    CGST takes the floor and SGST absorbs the remainder. Printing 68.645
    twice would overstate the tax by half a paisa on every such invoice.
    """
    gst = money(gst)
    cgst = (gst / 2).quantize(_CENT, rounding=ROUND_DOWN)
    return cgst, money(gst - cgst)


def decompose_inclusive(gross: Decimal, rate_pct: Decimal) -> tuple[Decimal, Decimal]:
    """Split a GST-inclusive amount into (taxable, gst).

    gst is derived by subtraction rather than computed independently, which
    is what guarantees taxable + gst == gross exactly.
    """
    gross = money(gross)
    if rate_pct <= 0:
        return gross, ZERO
    taxable = money(gross / (Decimal("1") + rate_pct / Decimal("100")))
    return taxable, money(gross - taxable)


def add_exclusive(taxable: Decimal, rate_pct: Decimal) -> tuple[Decimal, Decimal]:
    """Add GST on top of a taxable value, returning (gst, gross)."""
    taxable = money(taxable)
    if rate_pct <= 0:
        return ZERO, taxable
    gst = money(taxable * rate_pct / Decimal("100"))
    return gst, money(taxable + gst)


# ===========================================================================
# Rate card input
# ===========================================================================
@dataclass(frozen=True)
class PricingComponent:
    """One configurable row of the charges table."""

    code: str
    label: str
    input_amount: Decimal
    basis: Basis = "customer_rate"
    gst_rate_pct: Decimal = ZERO
    #: Only meaningful when basis == "customer_rate": is `input_amount` already
    #: GST-inclusive (a ₹150 shelf price) or pre-tax (₹127.12 + tax)?
    gst_inclusive: bool = True
    earns_to: EarnsTo = "worker"
    #: Platform's share of this line. None -> the platform default (20%).
    #: Ignored when earns_to == "platform" (the platform keeps all of it).
    commission_pct: Optional[Decimal] = None
    sac_code: Optional[str] = None
    exemption_note: Optional[str] = None
    quantity: int = 1

    @property
    def is_exempt(self) -> bool:
        return self.gst_rate_pct <= 0


# ===========================================================================
# Computed output
# ===========================================================================
@dataclass(frozen=True)
class ComputedLine:
    """A fully-priced component. Holds both the customer and internal figures;
    the view builders below decide which of them anyone actually gets to see."""

    code: str
    label: str
    sac_code: Optional[str]

    # --- customer-facing ---
    service_value: Decimal      # pre-GST value of the line
    gst_rate_pct: Decimal
    gst_amount: Decimal
    cgst_amount: Decimal
    sgst_amount: Decimal
    line_total: Decimal         # what the customer pays for this line
    exemption_note: Optional[str]

    # --- internal only ---
    worker_earning: Decimal         # nurse's net take-home from this line
    earns_to: EarnsTo               # who the line's service value belongs to
    platform_fee_gross: Decimal     # platform's cut, GST-inclusive
    platform_fee_taxable: Decimal   # ex-GST portion of that cut
    platform_fee_gst: Decimal       # GST the platform owes on its own fee
    commission_pct: Decimal

    @property
    def is_exempt(self) -> bool:
        return self.gst_rate_pct <= 0


def compute_line(
    component: PricingComponent,
    *,
    default_commission_pct: Decimal,
) -> ComputedLine:
    """Price one rate-card row.

    Order of operations matters and is fixed:
      1. establish the line's pre-GST service value and the nurse's earning
         (direction depends on `basis`);
      2. apply the line's own output GST to the service value;
      3. decompose the platform's cut into fee + GST-on-fee.

    Step 3 is separate from step 2 on purpose: output GST is collected from
    the customer for the government, whereas GST on the platform fee is what
    the platform owes on its B2B service to the nurse. Conflating them is how
    a marketplace ends up paying tax twice on the same rupee.
    """
    amount = money(component.input_amount) * component.quantity
    amount = money(amount)

    if component.earns_to == "platform":
        commission_pct = Decimal("100")
    else:
        commission_pct = (
            component.commission_pct
            if component.commission_pct is not None
            else default_commission_pct
        )
    commission_pct = Decimal(str(commission_pct))

    # --- 1. service value + nurse earning -------------------------------
    if component.basis == "earning_rate":
        # Input is the nurse's take-home; gross it up so the commission fits
        # inside the customer price.
        worker_earning = amount
        retained = Decimal("1") - commission_pct / Decimal("100")
        if retained <= 0:
            # 100% commission on an "earning rate" is contradictory; treat the
            # input as pure platform revenue rather than dividing by zero.
            service_value = amount
            worker_earning = ZERO
        else:
            service_value = money(amount / retained)
        platform_fee_gross = money(service_value - worker_earning)
    else:
        # Input is the customer price; take the commission out of it.
        if component.gst_inclusive:
            service_value, _ = decompose_inclusive(amount, component.gst_rate_pct)
        else:
            service_value = amount
        platform_fee_gross = money(service_value * commission_pct / Decimal("100"))
        worker_earning = money(service_value - platform_fee_gross)

    # --- 2. output GST charged to the customer --------------------------
    if component.basis == "customer_rate" and component.gst_inclusive:
        gst_amount = money(amount - service_value)
        line_total = amount
    else:
        gst_amount, line_total = add_exclusive(service_value, component.gst_rate_pct)

    cgst, sgst = split_cgst_sgst(gst_amount)

    # --- 3. GST on the platform's own fee -------------------------------
    fee_taxable, fee_gst = decompose_inclusive(platform_fee_gross, PLATFORM_FEE_GST_RATE)

    note = component.exemption_note
    if component.is_exempt and note is None:
        note = GST_EXEMPTION_NOTE

    return ComputedLine(
        code=component.code,
        label=component.label,
        sac_code=component.sac_code,
        service_value=service_value,
        gst_rate_pct=Decimal(str(component.gst_rate_pct)),
        gst_amount=gst_amount,
        cgst_amount=cgst,
        sgst_amount=sgst,
        line_total=line_total,
        exemption_note=note,
        worker_earning=worker_earning,
        earns_to=component.earns_to,
        platform_fee_gross=platform_fee_gross,
        platform_fee_taxable=fee_taxable,
        platform_fee_gst=fee_gst,
        commission_pct=commission_pct,
    )


# ===========================================================================
# Whole-booking breakdown
# ===========================================================================
@dataclass(frozen=True)
class Deduction:
    """A post-commission recovery from the nurse's payout (e-stamp advance,
    TDS). Kept separate from commission because these are withholdings, not
    platform revenue, and the payout statement must show them below the
    take-home subtotal."""

    code: str
    label: str
    amount: Decimal
    note: Optional[str] = None


@dataclass(frozen=True)
class PricingBreakdown:
    lines: list[ComputedLine]
    default_commission_pct: Decimal
    deductions: list[Deduction] = field(default_factory=list)
    subsidy_amount: Decimal = ZERO

    # --- customer totals ---
    @property
    def taxable_value(self) -> Decimal:
        return money(sum((l.service_value for l in self.lines if not l.is_exempt), ZERO))

    @property
    def exempt_value(self) -> Decimal:
        return money(sum((l.service_value for l in self.lines if l.is_exempt), ZERO))

    @property
    def total_gst(self) -> Decimal:
        return money(sum((l.gst_amount for l in self.lines), ZERO))

    @property
    def total_cgst(self) -> Decimal:
        return money(sum((l.cgst_amount for l in self.lines), ZERO))

    @property
    def total_sgst(self) -> Decimal:
        return money(sum((l.sgst_amount for l in self.lines), ZERO))

    @property
    def subtotal(self) -> Decimal:
        """Pre-GST value of everything, exempt and taxable together."""
        return money(sum((l.service_value for l in self.lines), ZERO))

    @property
    def customer_total(self) -> Decimal:
        """THE number shown to the customer and charged via Razorpay."""
        return money(
            sum((l.line_total for l in self.lines), ZERO) - money(self.subsidy_amount)
        )

    # --- internal totals ---
    @property
    def worker_lines(self) -> list[ComputedLine]:
        """Lines the nurse actually earns from.

        Platform-owned lines (consumables kits, protection fees) are excluded:
        they are the platform's own supply to the customer, so they belong on
        the patient's invoice but have no place on a payout statement.
        """
        return [l for l in self.lines if l.earns_to == "worker"]

    @property
    def worker_service_value(self) -> Decimal:
        """Gross service value earned by the nurse, BEFORE the platform fee —
        the 'Clinical Service Fee Earned' line on the payout statement."""
        return money(sum((l.service_value for l in self.worker_lines), ZERO))

    @property
    def worker_platform_fee_gross(self) -> Decimal:
        """Platform fee billed back to the nurse, GST-inclusive."""
        return money(sum((l.platform_fee_gross for l in self.worker_lines), ZERO))

    @property
    def worker_platform_fee_taxable(self) -> Decimal:
        return money(sum((l.platform_fee_taxable for l in self.worker_lines), ZERO))

    @property
    def worker_platform_fee_gst(self) -> Decimal:
        return money(sum((l.platform_fee_gst for l in self.worker_lines), ZERO))

    @property
    def worker_gross(self) -> Decimal:
        """Nurse's earnings before deductions — the 80% side of the split."""
        return money(sum((l.worker_earning for l in self.lines), ZERO))

    @property
    def platform_fee_gross(self) -> Decimal:
        return money(sum((l.platform_fee_gross for l in self.lines), ZERO))

    @property
    def platform_fee_taxable(self) -> Decimal:
        return money(sum((l.platform_fee_taxable for l in self.lines), ZERO))

    @property
    def platform_fee_gst(self) -> Decimal:
        return money(sum((l.platform_fee_gst for l in self.lines), ZERO))

    @property
    def total_deductions(self) -> Decimal:
        return money(sum((d.amount for d in self.deductions), ZERO))

    @property
    def worker_net_payable(self) -> Decimal:
        """Final disbursal to the nurse's bank account. This is the amount
        handed to Razorpay Payouts — never a number computed anywhere else."""
        return money(self.worker_gross - self.total_deductions)

    def with_deductions(self, deductions: list[Deduction]) -> "PricingBreakdown":
        return PricingBreakdown(
            lines=self.lines,
            default_commission_pct=self.default_commission_pct,
            deductions=deductions,
            subsidy_amount=self.subsidy_amount,
        )


def compute_breakdown(
    components: list[PricingComponent],
    *,
    default_commission_pct: Decimal,
    deductions: Optional[list[Deduction]] = None,
    subsidy_amount: Decimal = ZERO,
) -> PricingBreakdown:
    """Price a whole booking from its rate card."""
    lines = [
        compute_line(c, default_commission_pct=Decimal(str(default_commission_pct)))
        for c in components
    ]
    return PricingBreakdown(
        lines=lines,
        default_commission_pct=Decimal(str(default_commission_pct)),
        deductions=list(deductions or []),
        subsidy_amount=money(subsidy_amount),
    )


# ===========================================================================
# Views — the customer/admin boundary
# ===========================================================================
def customer_view(breakdown: PricingBreakdown) -> dict:
    """Exactly what a patient may see.

    The commission split is not in the returned structure at all. There is no
    filtering step downstream that could forget to run, and no field an
    over-eager serializer could pick up: consolidated line value, its GST,
    line total, totals. That's the whole object.
    """
    return {
        "line_items": [
            {
                "label": l.label,
                "sac_code": l.sac_code,
                "amount": str(l.service_value),
                "gst_rate_pct": str(l.gst_rate_pct),
                "gst_amount": str(l.gst_amount),
                "cgst_amount": str(l.cgst_amount),
                "sgst_amount": str(l.sgst_amount),
                "line_total": str(l.line_total),
                "is_exempt": l.is_exempt,
                "exemption_note": l.exemption_note if l.is_exempt else None,
            }
            for l in breakdown.lines
        ],
        "taxable_value": str(breakdown.taxable_value),
        "exempt_value": str(breakdown.exempt_value),
        "cgst_amount": str(breakdown.total_cgst),
        "sgst_amount": str(breakdown.total_sgst),
        "total_gst": str(breakdown.total_gst),
        "subsidy_amount": str(breakdown.subsidy_amount),
        "total_amount": str(breakdown.customer_total),
    }


def admin_view(breakdown: PricingBreakdown) -> dict:
    """The complete calculation, for admin/finance eyes only.

    Mirrors the modular charges table: per line you get the package rate, GST
    on it, the platform fee, GST on the platform fee, and the summed customer
    price — plus the nurse-side split and every deduction.
    """
    return {
        "rate_card": [
            {
                "code": l.code,
                "label": l.label,
                "sac_code": l.sac_code,
                # column 1 — package rate (pre-GST customer value)
                "package_rate": str(l.service_value),
                # column 2 — GST charged on it
                "gst_rate_pct": str(l.gst_rate_pct),
                "gst_on_service": str(l.gst_amount),
                # column 3 — platform fee (ex-GST)
                "platform_fee": str(l.platform_fee_taxable),
                # column 4 — GST on the platform fee
                "gst_on_platform_fee": str(l.platform_fee_gst),
                "platform_fee_gross": str(l.platform_fee_gross),
                # column 5 — the summation shown to the customer
                "customer_line_total": str(l.line_total),
                "worker_earning": str(l.worker_earning),
                "commission_pct": str(l.commission_pct),
                "is_exempt": l.is_exempt,
            }
            for l in breakdown.lines
        ],
        "customer": {
            "taxable_value": str(breakdown.taxable_value),
            "exempt_value": str(breakdown.exempt_value),
            "cgst_amount": str(breakdown.total_cgst),
            "sgst_amount": str(breakdown.total_sgst),
            "total_gst": str(breakdown.total_gst),
            "subsidy_amount": str(breakdown.subsidy_amount),
            "total_amount": str(breakdown.customer_total),
        },
        "split": {
            "commission_pct": str(breakdown.default_commission_pct),
            "worker_service_value": str(breakdown.worker_service_value),
            "worker_share_pct": str(Decimal("100") - breakdown.default_commission_pct),
            "worker_gross": str(breakdown.worker_gross),
            "platform_fee_gross": str(breakdown.platform_fee_gross),
            "platform_fee_taxable": str(breakdown.platform_fee_taxable),
            "platform_fee_gst": str(breakdown.platform_fee_gst),
        },
        "deductions": [
            {"code": d.code, "label": d.label, "amount": str(d.amount), "note": d.note}
            for d in breakdown.deductions
        ],
        "total_deductions": str(breakdown.total_deductions),
        "worker_net_payable": str(breakdown.worker_net_payable),
    }


def to_storable(breakdown: PricingBreakdown) -> dict:
    """JSONB snapshot persisted on the Invoice row.

    Frozen at invoice time so a later change to the rate card can never
    retroactively alter what a customer was actually billed.
    """
    return {
        "version": 1,
        "customer": customer_view(breakdown),
        "internal": admin_view(breakdown),
    }
