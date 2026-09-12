"""Service catalogue + Care packages — discovery endpoints."""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.enums import WorkerType
from app.models.models import CarePackage, ChecklistTemplate, ServiceCatalogue
from app.schemas.schemas import CarePackageOut, PackageServiceSummary, ServiceOut

router = APIRouter(tags=["catalog"])


# ---------------------------------------------------------------------------
# Provider-Type filtering.
#
# Both ServiceCatalogue and CarePackage already carry `allowed_provider_types`
# and it is already enforced at qualification time
# (app/services/qualification.py -> PROVIDER_TYPE_NOT_ALLOWED). What was
# missing is the *discovery* half: every worker was shown the entire
# catalogue and only found out a package didn't apply to them when they
# tried to opt in. These helpers make the same rule drive what is listed,
# so a Nurse sees Nurse packages, a Doctor sees Doctor packages, and so on.
#
# Deliberately additive: `provider_type` is an optional query param and
# omitting it returns exactly what these endpoints returned before, so the
# consumer-facing booking screens are unaffected.
# ---------------------------------------------------------------------------
def _matches_provider_type(offering, provider_type: Optional[WorkerType]) -> bool:
    """True if this service/package is offered by the given provider type.

    A NULL/empty `allowed_provider_types` means "no restriction" and matches
    every provider type — that is the existing back-compat contract and is
    what keeps pre-existing catalogue rows visible.
    """
    if provider_type is None:
        return True
    allowed = list(getattr(offering, "allowed_provider_types", None) or [])
    if not allowed:
        return True
    return provider_type.value in allowed


def _parse_provider_type(raw: Optional[str]) -> Optional[WorkerType]:
    if raw is None or not str(raw).strip():
        return None
    try:
        return WorkerType(str(raw).strip().lower())
    except ValueError:
        valid = ", ".join(t.value for t in WorkerType)
        raise HTTPException(
            status_code=422,
            detail=f"Unknown provider_type '{raw}'. Expected one of: {valid}",
        ) from None


@router.get("/services", response_model=List[ServiceOut])
async def list_services(
    category: Optional[str] = None,
    active_only: bool = True,
    provider_type: Optional[str] = Query(
        None,
        description=(
            "Only return services this Provider Type may deliver "
            "(nurse | caregiver | doctor | dentist | physiotherapist | "
            "mother_baby_caregiver). Omit for the full catalogue."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    ptype = _parse_provider_type(provider_type)
    conds = []
    if active_only:
        conds.append(ServiceCatalogue.is_active.is_(True))
    if category:
        conds.append(ServiceCatalogue.category == category)
    res = await db.execute(select(ServiceCatalogue).where(and_(*conds)) if conds else select(ServiceCatalogue))
    # Filtered in Python rather than SQL: allowed_provider_types is a
    # nullable ARRAY and "NULL means unrestricted" does not express cleanly
    # as an indexable predicate. The catalogue is small and already fully
    # loaded here, so this costs nothing measurable.
    return [
        ServiceOut.model_validate(s)
        for s in res.scalars().all()
        if _matches_provider_type(s, ptype)
    ]


@router.get("/services/{service_id}", response_model=ServiceOut)
async def get_service(service_id: UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id == service_id))
    s = res.scalar_one_or_none()
    if not s:
        raise HTTPException(status_code=404, detail="Service not found")
    return ServiceOut.model_validate(s)


def _package_included_ids(package: CarePackage) -> List[UUID]:
    """A package's own service ids: included_service_ids plus primary_service_id
    (deduplicated, primary first). This is the single source of truth for
    'which services belong to this package' — never the full catalogue."""
    ids: List[UUID] = []
    if package.primary_service_id:
        ids.append(package.primary_service_id)
    for sid in (package.included_service_ids or []):
        if sid not in ids:
            ids.append(sid)
    return ids


async def _care_packages_out(
    packages: List[CarePackage], db: AsyncSession
) -> List[CarePackageOut]:
    """Resolve and embed each package's own service(s) in one batched query,
    so callers get a self-contained response and never need to fall back to
    the generic /services catalogue to know what a package includes."""
    all_ids: set = set()
    for p in packages:
        all_ids.update(_package_included_ids(p))

    services_by_id: dict = {}
    if all_ids:
        sres = await db.execute(select(ServiceCatalogue).where(ServiceCatalogue.id.in_(all_ids)))
        services_by_id = {s.id: s for s in sres.scalars().all()}

    out: List[CarePackageOut] = []
    for p in packages:
        included_ids = _package_included_ids(p)
        # BUGFIX: this used to call CarePackageOut.model_validate(p) FIRST and
        # only overwrite `included_service_ids` afterwards. But `model_validate`
        # reads the raw ORM column straight off `p` — CarePackage.included_service_ids
        # is nullable and is None for any package that only sets a
        # primary_service_id (i.e. almost every package), while the schema
        # field is a plain `List[UUID]`. Pydantic rejects None for a list
        # field outright (ValidationError: "Input should be a valid list"),
        # so ONE such package crashed the entire /care-packages list with a
        # 500 for every consumer — this is what showed up in the app as
        # "Couldn't load this / Request failed" on the booking screen.
        # Fix: build the dict from the ORM columns ourselves and set
        # `included_service_ids` to the already-normalised list *before*
        # constructing CarePackageOut, instead of validating the raw
        # (possibly-None) column first.
        data = {column.name: getattr(p, column.name) for column in CarePackage.__table__.columns}
        data["included_service_ids"] = included_ids
        data["services"] = [
            PackageServiceSummary(id=s.id, service_code=s.service_code, name=s.name)
            for sid in included_ids
            if (s := services_by_id.get(sid)) is not None
        ]
        out.append(CarePackageOut(**data))
    return out


@router.get("/care-packages", response_model=List[CarePackageOut])
async def list_care_packages(
    city: Optional[str] = None,
    active_only: bool = True,
    provider_type: Optional[str] = Query(
        None,
        description=(
            "Only return packages this Provider Type may deliver, so a Nurse "
            "sees Nurse packages, a Doctor sees Doctor packages, etc. "
            "Omit for the full catalogue (consumer booking screens)."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    ptype = _parse_provider_type(provider_type)
    # Deleted packages never appear in any list — admin's active_only=false
    # is only meant to surface disabled-but-not-deleted packages.
    conds = [CarePackage.is_deleted.is_(False)]
    if active_only:
        conds.append(CarePackage.is_active.is_(True))
    res = await db.execute(select(CarePackage).where(and_(*conds)))
    items = list(res.scalars().all())
    if city:
        items = [p for p in items if not p.available_cities or city in p.available_cities]
    items = [p for p in items if _matches_provider_type(p, ptype)]
    return await _care_packages_out(items, db)


@router.get("/care-packages/{package_id}", response_model=CarePackageOut)
async def get_care_package(package_id: UUID, db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(CarePackage).where(CarePackage.id == package_id))
    p = res.scalar_one_or_none()
    if not p:
        raise HTTPException(status_code=404, detail="Care package not found")
    return (await _care_packages_out([p], db))[0]


@router.get("/care/checklist-template/{template_id}")
async def get_checklist_template(template_id: UUID, db: AsyncSession = Depends(get_db), _=Depends(get_current_user)):
    res = await db.execute(select(ChecklistTemplate).where(ChecklistTemplate.id == template_id))
    t = res.scalar_one_or_none()
    if not t:
        raise HTTPException(status_code=404, detail="Template not found")
    return {
        "id": str(t.id),
        "code": t.code,
        "name": t.name,
        "phase": t.phase.value,
        "version": t.version,
        "questions": t.questions,
    }