"""
/api/standings/* — Championship standings and schedule via Ergast/Jolpica.
"""
from fastapi import APIRouter, Path, Query
from typing import Optional
from ..services import ergast

router = APIRouter(prefix="/api/standings", tags=["standings"])


@router.get("/drivers")
async def driver_standings(year: Optional[int] = Query(None)):
    """Current driver championship standings."""
    try:
        data = await ergast.get_driver_standings(year)
        return data or []
    except Exception:
        return []


@router.get("/constructors")
async def constructor_standings(year: Optional[int] = Query(None)):
    """Current constructor championship standings."""
    try:
        data = await ergast.get_constructor_standings(year)
        return data or []
    except Exception:
        return []


@router.get("/schedule")
async def race_schedule(year: Optional[int] = Query(None)):
    """Season race calendar."""
    try:
        data = await ergast.get_race_schedule(year)
        return data or []
    except Exception:
        return []


@router.get("/results/{year}/{round}")
async def race_results(year: int = Path(...), round: int = Path(...)):
    """Race results from Ergast."""
    try:
        return await ergast.get_race_results(year, round)
    except Exception:
        return []


@router.get("/qualifying/{year}/{round}")
async def qualifying_results(year: int = Path(...), round: int = Path(...)):
    """Qualifying results."""
    try:
        return await ergast.get_qualifying_results(year, round)
    except Exception:
        return []
