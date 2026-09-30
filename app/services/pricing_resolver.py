"""Turns a Booking into rate-card input for `pricing_engine`.

This is the only place that knows how to get from database rows to
`PricingComponent` objects. The engine itself stays pure; everything that
touches SQLAlchemy lives here.

Resolution order for a booking:

  1. `PricingComponent` rows configured for the booked package or service.
     This is the modular charges table — the intended path.
  2. No rows configured -> a single legacy component synthesised from
     `booking.base_amount + surge_amount`, priced on the `customer_rate`
     basis at the offering's commission_pct.

Step 2 is what keeps every pre-existing catalogue entry working unchanged.
A booking created before the rate card existed produces exactly the customer
total and the same nurse split it always did; configuring components for an
offering is an opt-in upgrade, not a migration everyone must complete first.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.company import (
    GST_EXEMPTION_NOTE,
    SAC_NURSING_EXEMPT,
)
from app.core.config import settings
from app.models.models import (
    Booking,
    CarePackage,
    PricingComponent as PricingComponentRow,
    ServiceCatalogue,
)
from app.services.pricing_engine import (
    Deduction,
    PricingBreakdown,
    PricingComponent,
    compute_breakdown,
    money,
)


async def default_commission_pct(db: AsyncSession, booking: Booking) -> Decimal:
    """Platform's share for this booking's offering, else the platform default
    (settings.PLATFORM_COMMISSION_PCT, 20 -> the nurse keeps 80%)."""
    pct: Optional[Decimal] = None
    if booking.service_id:
        res = await db.execute(
            select(ServiceCatalogue.commission_pct).where(
                ServiceCatalogue.id == booking.service_id
            )
        )
        pct = res.scalar_one_or_none()
    elif booking.package_id:
        res = await db.execute(
            select(CarePackage.commission_pct).where(CarePackage.id == booking.package_id)
        )
        pct = res.scalar_one_or_none()
    if pct is None:
        pct = Decimal(str(settings.PLATFORM_COMMISSION_PCT))
    return Decimal(pct)


async def load_rate_card(db: AsyncSession, booking: Booking) -> list[PricingComponent]:
    """Configured components for the booked offering, in display order."""
    stmt = select(PricingComponentRow).where(PricingComponentRow.is_active.is_(True))
    if booking.package_id:
        stmt = stmt.where(PricingComponentRow.package_id == booking.package_id)
    elif booking.service_id:
        stmt = stmt.where(PricingComponentRow.service_id == booking.service_id)
    else:
        return []
    stmt = stmt.order_by(PricingComponentRow.display_order, PricingComponentRow.created_at)

    rows = (await db.execute(stmt)).scalars().all()
    return [
        PricingComponent(
            code=r.component_code,
            label=r.label,
            input_amount=Decimal(r.input_amount),
            basis=r.basis,  # type: ignore[arg-type]
            gst_rate_pct=Decimal(r.gst_rate_pct or 0),
            gst_inclusive=bool(r.gst_inclusive),
            earns_to=r.earns_to,  # type: ignore[arg-type]
            commission_pct=Decimal(r.commission_pct) if r.commission_pct is not None else None,
            sac_code=r.sac_code,
            exemption_note=r.exemption_note,
        )
        for r in rows
    ]


GENERIC_LABELS = {
    "professional nursing service",
    "nursing service",
    "service",
    "professional service",
}


async def resolve_offering_name(db: AsyncSession, booking: Booking) -> Optional[str]:
    """Name of what the customer actually purchased — the source of truth for
    every invoice/receipt label. Package wins over the service id that may
    also be stored on the row; service is used for one-time service bookings.

    ROOT CAUSE (generic invoice text): the legacy single-line fallback and the
    composite invoice hardcoded "Professional Nursing Service", and the rate
    card's generic component label was printed as-is, so nothing on the
    invoice said which package/service was bought.
    """
    from app.models.models import CarePackage, ServiceCatalogue

    if booking.package_id:
        r = await db.execute(select(CarePackage.name).where(CarePackage.id == booking.package_id))
        name = r.scalar_one_or_none()
        if name:
            return name
    if booking.service_id:
        r = await db.execute(select(ServiceCatalogue.name).where(ServiceCatalogue.id == booking.service_id))
        name = r.scalar_one_or_none()
        if name:
            return name
    return None


def _legacy_component(booking: Booking, offering_name: Optional[str] = None) -> PricingComponent:
    """The single-line fallback for offerings with no rate card configured.

    Uses base + surge as the customer-facing service value, which is exactly
    what `total_amount` was built from at booking creation, and carries the
    booking's own tax_amount as its GST (0 for the exempt healthcare bookings
    that make up the current catalogue).
    """
    service_value = money(Decimal(booking.base_amount or 0) + Decimal(booking.surge_amount or 0))
    tax = money(Decimal(booking.tax_amount or 0))

    if tax > 0 and service_value > 0:
        # Reconstruct the rate actually charged rather than assuming 18 — a
        # legacy booking's tax is whatever was stored on it.
        rate = (tax / service_value * Decimal("100")).quantize(Decimal("0.01"))
        return PricingComponent(
            code="service",
            label=offering_name or "Professional Nursing Service",
            input_amount=money(service_value + tax),
            basis="customer_rate",
            gst_rate_pct=rate,
            gst_inclusive=True,
        )

    return PricingComponent(
        code="service",
        label=offering_name or "Professional Nursing Service",
        input_amount=service_value,
        basis="customer_rate",
        gst_rate_pct=Decimal("0"),
        gst_inclusive=True,
        sac_code=SAC_NURSING_EXEMPT,
        exemption_note=GST_EXEMPTION_NOTE,
    )


async def build_components(db: AsyncSession, booking: Booking) -> list[PricingComponent]:
    name = await resolve_offering_name(db, booking)
    components = await load_rate_card(db, booking)
    if not components:
        return [_legacy_component(booking, name)]
    if name:
        # A rate-card row still carrying the generic label is re-labelled with
        # the purchased package/service; specific labels (e.g. "Consumables kit")
        # are left alone.
        from dataclasses import replace as _replace
        components = [
            _replace(c, label=name) if (c.label or "").strip().lower() in GENERIC_LABELS else c
            for c in components
        ]
    return components


async def price_booking(
    db: AsyncSession,
    booking: Booking,
    *,
    deductions: Optional[list[Deduction]] = None,
) -> PricingBreakdown:
    """The one call every caller uses to price a booking.

    Booking creation, invoice generation, the admin payout screen and the
    nurse's payout statement all route through here, which is what makes it
    impossible for them to disagree about a number.
    """
    components = await build_components(db, booking)
    return compute_breakdown(
        components,
        default_commission_pct=await default_commission_pct(db, booking),
        deductions=deductions,
        subsidy_amount=money(Decimal(booking.subsidy_amount or 0)),
    )
