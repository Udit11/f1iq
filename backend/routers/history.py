"""
/api/history/* — FastF1 historical session data
"""
from fastapi import APIRouter, Path, Query
from typing import Optional
from ..services import fastf1_service

router = APIRouter(prefix="/api/history", tags=["history"])


@router.get("/schedule/{year}")
async def season_schedule(year: int = Path(..., ge=1950, le=2030)):
    """Full season event schedule."""
    return await fastf1_service.get_event_schedule(year)


@router.get("/results/{year}/{round}")
async def race_results(
    year: int = Path(...),
    round: int = Path(...),
):
    """Final race results for a completed round."""
    return await fastf1_service.get_session_results(year, round)


@router.get("/laps/{year}/{round}")
async def lap_times(
    year: int = Path(...),
    round: int = Path(...),
):
    """All lap time data for a race."""
    return await fastf1_service.get_lap_times(year, round)


@router.get("/stints/{year}/{round}")
async def stint_data(
    year: int = Path(...),
    round: int = Path(...),
):
    """Tyre stint data for a race."""
    return await fastf1_service.get_stints_history(year, round)


@router.get("/weather/{year}/{round}")
async def weather_history(
    year: int = Path(...),
    round: int = Path(...),
):
    """Weather data during a race."""
    return await fastf1_service.get_weather_history(year, round)
