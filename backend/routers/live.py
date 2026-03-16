"""
/api/live/* — All live race data from OpenF1.
No demo fallback data is provided.
"""
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query
from typing import Optional
from ..services import openf1
from ..services.strategy import get_strategy_recommendations
from ..services.predictor import get_win_probabilities

router = APIRouter(prefix="/api/live", tags=["live"])


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _session_is_live(session: dict) -> bool:
    """
    OpenF1 marks "latest/current" around live sessions, but the dashboard should only
    behave as live while a session is active or has just ended.
    """
    if not session:
        return False

    now = datetime.now(timezone.utc)
    start = _parse_dt(session.get("date_start"))
    end = _parse_dt(session.get("date_end"))

    if start and now < start:
        return False
    if end:
        return now <= (end + timedelta(minutes=30))
    return False


def _empty_live_state(session: Optional[dict] = None, *, no_session: bool, reason: str) -> dict:
    session = session or {}
    return {
        "session_key": session.get("session_key"),
        "meeting_name": session.get("meeting_name", ""),
        "circuit": session.get("circuit_short_name", ""),
        "country": session.get("country_name", ""),
        "session_type": session.get("session_name", ""),
        "lap": None,
        "total_laps": session.get("total_laps"),
        "track_status": "Live data unavailable",
        "safety_car": False,
        "virtual_sc": False,
        "drivers": [],
        "weather": {},
        "race_control_latest": [],
        "live_unavailable": True,
        "message": reason,
    }


async def build_live_timing(
    session_key: Optional[int] = None,
) -> dict:
    """
    Shared timing builder used by both REST and WebSocket code paths.
    """
    import asyncio

    session = await (openf1.get_session(session_key) if session_key is not None else openf1.get_latest_session())

    # Default dashboard view: outside an active session, do not pretend there is live timing.
    if session_key is None and not _session_is_live(session):
        return _empty_live_state(session, no_session=True, reason="No live Formula 1 session is active right now.")

    sk = session.get("session_key") or session_key

    # Session-specific timing request can still try historical data snapshots.
    try:
        drivers, positions, intervals, laps, stints, pit_stops, race_control, weather, session = \
            await asyncio.gather(
                openf1.get_drivers(sk),
                openf1.get_positions(sk),
                openf1.get_intervals(sk),
                openf1.get_latest_laps(sk),
                openf1.get_stints(sk),
                openf1.get_pit_stops(sk),
                openf1.get_race_control(sk),
                openf1.get_weather(sk),
                openf1.get_session(sk) if sk is not None else openf1.get_latest_session(),
            )

        if not drivers:
            raise ValueError("No live driver data")

    except Exception:
        return _empty_live_state(session, no_session=False, reason="Live timing is currently unavailable from OpenF1.")

    # Index
    drivers_by_num = {d["driver_number"]: d for d in drivers}
    positions_by_num = {p["driver_number"]: p for p in positions}
    intervals_by_num = {i["driver_number"]: i for i in intervals}
    laps_by_num = {l["driver_number"]: l for l in laps}

    # Latest stint per driver
    stints_by_num: dict = {}
    all_stints: dict = {}
    for s in stints:
        dn = s.get("driver_number")
        if dn is None:
            continue
        all_stints.setdefault(dn, []).append(s)
        if dn not in stints_by_num or s.get("stint_number", 0) > stints_by_num[dn].get("stint_number", 0):
            stints_by_num[dn] = s

    current_lap = max((l.get("lap_number", 0) for l in laps), default=0)
    total_laps = session.get("total_laps") or 60

    track_status = "Green Flag"
    safety_car = False
    virtual_sc = False
    for msg in race_control[:10]:
        flag = str(msg.get("flag", ""))
        message = str(msg.get("message", "")).upper()
        if "SAFETY CAR" in message and "VIRTUAL" not in message:
            track_status = "Safety Car"; safety_car = True; break
        elif "VIRTUAL" in message and "SAFETY CAR" in message:
            track_status = "Virtual Safety Car"; virtual_sc = True; break
        elif flag == "RED":  track_status = "Red Flag"; break
        elif flag == "YELLOW": track_status = "Yellow Flag"; break

    pitted_drivers = {p["driver_number"] for p in pit_stops if p.get("lap_number") == current_lap}

    driver_timing = []
    for dn, driver in drivers_by_num.items():
        pos = positions_by_num.get(dn, {})
        inv = intervals_by_num.get(dn, {})
        lap = laps_by_num.get(dn, {})
        stint = stints_by_num.get(dn, {})
        position = pos.get("position", 99)
        compound = stint.get("compound", "")
        tyre_age = max(0, (stint.get("lap_end") or current_lap) - (stint.get("lap_start") or 1) + 1) if stint else 0

        from ..services.strategy import _tyre_health
        health = _tyre_health(compound, tyre_age) if compound else None

        gap = inv.get("gap_to_leader")
        gap_str = "LEADER" if (gap == 0 or position == 1) else (f"+{gap:.3f}" if isinstance(gap, (int, float)) else str(gap or "—"))
        interval = inv.get("interval")
        int_str = f"+{interval:.3f}" if isinstance(interval, (int, float)) else str(interval or "—")

        def _fmt(t):
            if t is None: return None
            if isinstance(t, (int, float)):
                m = int(t // 60); s = t % 60
                return f"{m}:{s:06.3f}"
            return str(t)

        driver_timing.append({
            "driver_number": dn,
            "broadcast_name": driver.get("broadcast_name", ""),
            "full_name": driver.get("full_name", ""),
            "name_acronym": driver.get("name_acronym", ""),
            "team_name": driver.get("team_name", ""),
            "team_colour": "#" + driver.get("team_colour", "888888"),
            "position": position,
            "gap_to_leader": gap_str,
            "interval": int_str,
            "last_lap_time": _fmt(lap.get("lap_duration")),
            "best_lap_time": _fmt(lap.get("duration_sector_1")),
            "sector_1": _fmt(lap.get("duration_sector_1")),
            "sector_2": _fmt(lap.get("duration_sector_2")),
            "sector_3": _fmt(lap.get("duration_sector_3")),
            "is_pit_out_lap": lap.get("is_pit_out_lap", False),
            "tyre_compound": compound,
            "tyre_age": tyre_age,
            "tyre_health": health,
            "drs_open": False,
            "in_pit": dn in pitted_drivers,
            "retired": False,
            "speed_trap": None,
        })

    driver_timing.sort(key=lambda x: x["position"])

    return {
        "session_key": session.get("session_key", 0),
        "meeting_name": session.get("meeting_name", ""),
        "circuit": session.get("circuit_short_name", ""),
        "country": session.get("country_name", ""),
        "session_type": session.get("session_name", ""),
        "lap": current_lap,
        "total_laps": total_laps,
        "track_status": track_status,
        "safety_car": safety_car,
        "virtual_sc": virtual_sc,
        "drivers": driver_timing,
        "weather": weather,
        "race_control_latest": race_control[:5],
    }


@router.get("/session")
async def current_session(session_key: Optional[int] = Query(None)):
    """Current or latest session metadata."""
    try:
        data = await (openf1.get_session(session_key) if session_key else openf1.get_latest_session())
        return data if data else {"session_key": None, "status": "no-session"}
    except Exception:
        return {"session_key": None, "status": "no-session"}


@router.get("/timing")
async def live_timing(session_key: Optional[int] = Query(None)):
    """
    Full timing tower — positions, gaps, tyre data, lap times.
    Outside an active session this returns a no-session payload unless demo mode is enabled.
    """
    return await build_live_timing(session_key)


@router.get("/car/{driver_number}")
async def car_telemetry(driver_number: int, session_key: Optional[int] = Query(None)):
    """Latest telemetry samples for a specific driver."""
    try:
        data = await openf1.get_car_data(driver_number, session_key)
        return data if data else []
    except Exception:
        return []


@router.get("/pit-stops")
async def pit_stops(session_key: Optional[int] = Query(None)):
    """All pit stop records for the session."""
    try:
        data = await openf1.get_pit_stops(session_key)
        drivers = await openf1.get_drivers(session_key)
        if not data:
            raise ValueError("no data")
        driver_map = {d["driver_number"]: d for d in drivers}
        out = []
        for p in data:
            dn = p.get("driver_number")
            drv = driver_map.get(dn, {})
            out.append({
                "driver_number": dn,
                "name_acronym": drv.get("name_acronym", ""),
                "team_name": drv.get("team_name", ""),
                "team_colour": "#" + drv.get("team_colour", "888888"),
                "lap_number": p.get("lap_number"),
                "pit_duration": p.get("pit_duration"),
                "date": p.get("date", ""),
            })
        return sorted(out, key=lambda x: x.get("lap_number") or 0)
    except Exception:
        return []


@router.get("/race-control")
async def race_control(session_key: Optional[int] = Query(None)):
    """Race control messages — flags, safety car, penalties."""
    try:
        data = await openf1.get_race_control(session_key)
        return data or []
    except Exception:
        return []


@router.get("/weather")
async def weather(session_key: Optional[int] = Query(None)):
    """Live weather data."""
    try:
        data = await openf1.get_weather(session_key)
        return data or {}
    except Exception:
        return {}


@router.get("/strategy")
async def strategy(session_key: Optional[int] = Query(None)):
    """Pit strategy recommendations for top 10 drivers."""
    if session_key is None:
        session = await openf1.get_latest_session()
        if not _session_is_live(session):
            return []
    try:
        result = await get_strategy_recommendations(session_key)
        return result or []
    except Exception:
        return []


@router.get("/predictor")
async def predictor(session_key: Optional[int] = Query(None)):
    """Win probability predictions."""
    empty = {
        "session_key": None,
        "lap": None,
        "total_laps": None,
        "safety_car": False,
        "model_confidence": 0.0,
        "predictions": [],
        "feature_weights": {},
    }
    if session_key is None:
        session = await openf1.get_latest_session()
        if not _session_is_live(session):
            return empty
    try:
        result = await get_win_probabilities(session_key)
        return result if result.get("predictions") else empty
    except Exception:
        return empty


@router.get("/stints")
async def stints(
    session_key: Optional[int] = Query(None),
    driver_number: Optional[int] = Query(None),
):
    """Tyre stint data."""
    try:
        return await openf1.get_stints(session_key, driver_number)
    except Exception:
        return []
