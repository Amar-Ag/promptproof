"""Target registration and versioning.

Targets are immutable (ADR-0004): there is deliberately no PUT, PATCH or DELETE
route here. Editing means creating a new version.
"""

import uuid

from fastapi import APIRouter, HTTPException, status
from promptproof_core.logging import get_logger
from promptproof_core.models import Target, TargetFamily
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..deps import SessionDep
from ..schemas import TargetCreate, TargetFamilyOut, TargetOut, TargetVersionCreate

router = APIRouter(tags=["targets"])
log = get_logger(__name__)

# Two concurrent version inserts both read the same max(version). The unique
# constraint on (family_id, version) makes the loser fail rather than duplicate,
# and we retry it. A handful of attempts is plenty: contention is per family.
_VERSION_INSERT_ATTEMPTS = 5


def _spec_columns(spec: TargetCreate | TargetVersionCreate) -> dict[str, object]:
    return {
        "kind": spec.kind,
        "model": spec.model,
        "system_prompt": spec.system_prompt,
        "endpoint_url": spec.endpoint_url,
        "endpoint_auth_header": spec.endpoint_auth_header,
        "ownership_confirmed": spec.ownership_confirmed,
    }


async def _insert_next_version(
    session: AsyncSession,
    family_id: uuid.UUID,
    spec: TargetCreate | TargetVersionCreate,
) -> Target:
    """Insert version max+1 for a family, retrying if another writer won the race."""
    last_error: IntegrityError | None = None

    for _ in range(_VERSION_INSERT_ATTEMPTS):
        next_version = (
            await session.scalar(
                select(func.coalesce(func.max(Target.version), 0) + 1).where(
                    Target.family_id == family_id
                )
            )
        ) or 1

        target = Target(family_id=family_id, version=next_version, **_spec_columns(spec))
        try:
            # A savepoint, so a constraint violation does not poison the
            # surrounding transaction and we can simply try the next version.
            async with session.begin_nested():
                session.add(target)
                await session.flush()
            return target
        except IntegrityError as exc:
            last_error = exc
            continue

    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Could not allocate a target version under contention; retry the request.",
    ) from last_error


@router.post(
    "/targets",
    response_model=TargetOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a target",
)
async def create_target(payload: TargetCreate, session: SessionDep) -> TargetOut:
    family = TargetFamily(name=payload.name)
    session.add(family)
    await session.flush()

    target = await _insert_next_version(session, family.id, payload)

    # Note what was created, never the prompt itself (docs/observability.md).
    log.info(
        "target.created",
        target_id=str(target.id),
        family_id=str(family.id),
        kind=target.kind,
        version=target.version,
        system_prompt_length=len(target.system_prompt or ""),
    )
    return TargetOut.of(target, family.name)


@router.get("/targets", response_model=list[TargetFamilyOut], summary="List target families")
async def list_targets(session: SessionDep, limit: int = 100) -> list[TargetFamilyOut]:
    limit = max(1, min(limit, 200))
    families = list(
        (
            await session.scalars(
                select(TargetFamily).order_by(TargetFamily.created_at.desc()).limit(limit)
            )
        ).all()
    )
    if not families:
        return []

    versions = list(
        (
            await session.scalars(
                select(Target).where(Target.family_id.in_([f.id for f in families]))
            )
        ).all()
    )
    by_family: dict[uuid.UUID, list[Target]] = {f.id: [] for f in families}
    for target in versions:
        by_family[target.family_id].append(target)

    return [TargetFamilyOut.of(f, by_family[f.id]) for f in families]


@router.get("/targets/{target_id}", response_model=TargetOut, summary="Get one target version")
async def get_target(target_id: uuid.UUID, session: SessionDep) -> TargetOut:
    row = (
        await session.execute(
            select(Target, TargetFamily.name)
            .join(TargetFamily, TargetFamily.id == Target.family_id)
            .where(Target.id == target_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    target, name = row
    return TargetOut.of(target, name)


@router.post(
    "/targets/{target_id}/versions",
    response_model=TargetOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new version of a target",
)
async def create_version(
    target_id: uuid.UUID, payload: TargetVersionCreate, session: SessionDep
) -> TargetOut:
    row = (
        await session.execute(
            select(Target.family_id, TargetFamily.name)
            .join(TargetFamily, TargetFamily.id == Target.family_id)
            .where(Target.id == target_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    family_id, name = row

    target = await _insert_next_version(session, family_id, payload)
    log.info(
        "target.version_created",
        target_id=str(target.id),
        family_id=str(family_id),
        version=target.version,
    )
    return TargetOut.of(target, name)


@router.get(
    "/target-families/{family_id}",
    response_model=TargetFamilyOut,
    summary="Get a family and all its versions",
)
async def get_family(family_id: uuid.UUID, session: SessionDep) -> TargetFamilyOut:
    family = await session.get(TargetFamily, family_id)
    if family is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Target not found")
    versions = list(
        (await session.scalars(select(Target).where(Target.family_id == family_id))).all()
    )
    return TargetFamilyOut.of(family, versions)
