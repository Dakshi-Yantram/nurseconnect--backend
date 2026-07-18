"""Service catalogue + Care packages — discovery endpoints."""
from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.models import CarePackage, ChecklistTemplate, ServiceCatalogue
from app.schemas.schemas import CarePackageOut, PackageServiceSummary, ServiceOut

router = APIRouter(tags=["catalog"])


@router.get("/services", response_model=List[ServiceOut])
async def list_services(
    category: Optional[str] = None,
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
):
    conds = []
    if active_only:
        conds.append(ServiceCatalogue.is_active.is_(True))
    if category:
        conds.append(ServiceCatalogue.category == category)
    res = await db.execute(select(ServiceCatalogue).where(and_(*conds)) if conds else select(ServiceCatalogue))
    return [ServiceOut.model_validate(s) for s in res.scalars().all()]


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
        data = CarePackageOut.model_validate(p).model_dump()
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
    db: AsyncSession = Depends(get_db),
):
    conds = []
    if active_only:
        conds.append(CarePackage.is_active.is_(True))
    res = await db.execute(select(CarePackage).where(and_(*conds)) if conds else select(CarePackage))
    items = res.scalars().all()
    if city:
        items = [p for p in items if not p.available_cities or city in p.available_cities]
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