"""
FastF1 service — historical session data, lap times, stints, telemetry.
https://docs.fastf1.dev/
"""
import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from datetime import timezone
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# Lazy import so FastF1 only loads when needed
_ff1 = None
_executor = ThreadPoolExecutor(max_workers=2)

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../../.fastf1_cache")


def _get_ff1():
    global _ff1
    if _ff1 is None:
        import fastf1
        os.makedirs(CACHE_DIR, exist_ok=True)
        fastf1.Cache.enable_cache(CACHE_DIR)
        _ff1 = fastf1
    return _ff1


def _load_session_sync(year: int, round_num: int, session_type: str):
    """Blocking FastF1 session load — runs in thread pool."""
    ff1 = _get_ff1()
    try:
        session = ff1.get_session(year, round_num, session_type)
        session.load(laps=True, telemetry=False, weather=True, messages=True)
        return session
    except Exception as e:
        logger.error(f"FastF1 load failed: year={year} round={round_num} type={session_type} — {e}")
        return None


async def load_session(year: int, round_num: int, session_type: str = "R"):
    """Async wrapper around FastF1 session load."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(
        _executor,
        _load_session_sync,
        year, round_num, session_type
    )


async def get_lap_times(year: int, round_num: int) -> list:
    """Return lap-by-lap data for all drivers in a completed race."""
    session = await load_session(year, round_num, "R")
    if session is None:
        return []

    try:
        laps = session.laps
        result = []
        for _, lap in laps.iterrows():
            lt = lap.get("LapTime")
            result.append({
                "driver": lap.get("Driver", ""),
                "team": lap.get("Team", ""),
                "lap_number": int(lap.get("LapNumber", 0)),
                "lap_time_s": lt.total_seconds() if lt and hasattr(lt, "total_seconds") else None,
                "compound": lap.get("Compound", ""),
                "tyre_life": int(lap.get("TyreLife", 0)),
                "stint": int(lap.get("Stint", 0)),
                "position": int(lap.get("Position", 0)) if lap.get("Position") else None,
                "is_valid": bool(lap.get("IsPersonalBest", False) or lap.get("LapTime") is not None),
            })
        return result
    except Exception as e:
        logger.error(f"get_lap_times failed: {e}")
        return []


async def get_stints_history(year: int, round_num: int) -> list:
    """Return full stint data for all drivers."""
    session = await load_session(year, round_num, "R")
    if session is None:
        return []

    try:
        laps = session.laps
        result = {}
        for _, lap in laps.iterrows():
            driver = lap.get("Driver", "")
            stint = int(lap.get("Stint", 0))
            key = (driver, stint)
            if key not in result:
                result[key] = {
                    "driver": driver,
                    "team": lap.get("Team", ""),
                    "stint": stint,
                    "compound": lap.get("Compound", ""),
                    "start_lap": int(lap.get("LapNumber", 0)),
                    "end_lap": int(lap.get("LapNumber", 0)),
                    "laps": 1,
                }
            else:
                result[key]["end_lap"] = int(lap.get("LapNumber", 0))
                result[key]["laps"] += 1
        return list(result.values())
    except Exception as e:
        logger.error(f"get_stints_history failed: {e}")
        return []


async def get_session_results(year: int, round_num: int) -> list:
    """Final race results — position, points, fastest lap, etc."""
    session = await load_session(year, round_num, "R")
    if session is None:
        return []

    try:
        results = session.results
        out = []
        for _, row in results.iterrows():
            fl = row.get("FastestLapTime")
            out.append({
                "driver": row.get("Abbreviation", ""),
                "full_name": f"{row.get('FirstName','')} {row.get('LastName','')}".strip(),
                "team": row.get("TeamName", ""),
                "team_colour": "#" + str(row.get("TeamColor", "888888")),
                "position": int(row.get("Position", 99)) if row.get("Position") else 99,
                "grid_position": int(row.get("GridPosition", 0)) if row.get("GridPosition") else 0,
                "points": float(row.get("Points", 0)),
                "status": row.get("Status", ""),
                "fastest_lap": str(fl) if fl else None,
                "laps_completed": int(row.get("NumberOfLaps", 0)),
                "pit_stops": int(row.get("NumberOfPitStops", 0)) if row.get("NumberOfPitStops") else 0,
            })
        return sorted(out, key=lambda x: x["position"])
    except Exception as e:
        logger.error(f"get_session_results failed: {e}")
        return []


async def get_weather_history(year: int, round_num: int) -> list:
    """Weather data sampled during the race."""
    session = await load_session(year, round_num, "R")
    if session is None:
        return []

    try:
        wx = session.weather_data
        out = []
        for _, row in wx.iterrows():
            out.append({
                "time": str(row.get("Time", "")),
                "air_temp": float(row.get("AirTemp", 0)),
                "track_temp": float(row.get("TrackTemp", 0)),
                "humidity": float(row.get("Humidity", 0)),
                "wind_speed": float(row.get("WindSpeed", 0)),
                "wind_direction": float(row.get("WindDirection", 0)),
                "rainfall": bool(row.get("Rainfall", False)),
            })
        return out
    except Exception as e:  
        logger.error(f"get_weather_history failed: {e}")
        return []


async def get_event_schedule(year: int) -> list:
    """Full season calendar from FastF1."""
    loop = asyncio.get_event_loop()

    def _load():
        ff1 = _get_ff1()
        try:
            schedule = ff1.get_event_schedule(year)
            out = []
            for _, row in schedule.iterrows():
                out.append({
                    "round": int(row.get("RoundNumber", 0)),
                    "name": row.get("EventName", ""),
                    "circuit": row.get("Location", ""),
                    "country": row.get("Country", ""),
                    "date": str(row.get("EventDate", ""))[:10],
                    "format": row.get("EventFormat", ""),
                })
            return out
        except Exception as e:
            logger.error(f"get_event_schedule failed: {e}")
            return []

    return await loop.run_in_executor(_executor, _load)


async def get_event_schedule_detailed(year: int) -> list:
    """Detailed season calendar including session names and timestamps when available."""
    loop = asyncio.get_event_loop()

    def _fmt_offset(ts) -> Optional[str]:
        try:
            if ts is None or getattr(ts, "tzinfo", None) is None:
                return None
            raw = ts.strftime("%z")
            if not raw:
                return None
            return f"{raw[:3]}:{raw[3:]}"
        except Exception:
            return None

    def _fmt_session(row, idx: int) -> Optional[dict]:
        name = row.get(f"Session{idx}")
        if not name:
            return None

        def _to_dt(ts):
            if ts is None:
                return None
            try:
                if pd.isna(ts):
                    return None
            except Exception:
                pass
            try:
                if hasattr(ts, "to_pydatetime"):
                    ts = ts.to_pydatetime()
            except Exception:
                pass
            if ts is None:
                return None
            return ts

        local_ts = _to_dt(row.get(f"Session{idx}Date"))
        utc_ts = _to_dt(row.get(f"Session{idx}DateUtc"))

        if local_ts is None and utc_ts is not None:
            local_ts = utc_ts
        if utc_ts is None and local_ts is not None and getattr(local_ts, "tzinfo", None) is not None:
            utc_ts = local_ts.astimezone(timezone.utc)

        session_type = str(name).upper()
        mapping = {
            "PRACTICE 1": "FP1",
            "PRACTICE 2": "FP2",
            "PRACTICE 3": "FP3",
            "QUALIFYING": "Q",
            "SPRINT QUALIFYING": "SQ",
            "SPRINT SHOOTOUT": "SQ",
            "SPRINT": "SPRINT",
            "RACE": "RACE",
        }
        return {
            "name": str(name),
            "type": mapping.get(session_type, session_type.replace("PRACTICE ", "FP")),
            "date": local_ts.strftime("%Y-%m-%d") if local_ts else None,
            "local_time": local_ts.strftime("%H:%M") if local_ts else None,
            "utc_time": utc_ts.strftime("%Y-%m-%dT%H:%M:%SZ") if utc_ts else None,
            "utc_offset": _fmt_offset(local_ts),
        }

    def _load():
        ff1 = _get_ff1()
        try:
            schedule = ff1.get_event_schedule(year)
            out = []
            for _, row in schedule.iterrows():
                sessions = []
                for idx in range(1, 6):
                    session = _fmt_session(row, idx)
                    if session:
                        sessions.append(session)

                event_date = row.get("EventDate")
                if event_date is not None:
                    try:
                        if pd.isna(event_date):
                            event_date = None
                        elif hasattr(event_date, "to_pydatetime"):
                            event_date = event_date.to_pydatetime()
                    except Exception:
                        event_date = None

                out.append({
                    "round": int(row.get("RoundNumber", 0)),
                    "race_name": row.get("EventName", "") or row.get("OfficialEventName", ""),
                    "official_name": row.get("OfficialEventName", ""),
                    "country": row.get("Country", ""),
                    "locality": row.get("Location", ""),
                    "circuit": row.get("Location", ""),
                    "race_date": event_date.strftime("%Y-%m-%d") if event_date else "",
                    "format": row.get("EventFormat", ""),
                    "sessions": sessions,
                    "sprint": "sprint" in str(row.get("EventFormat", "")).lower(),
                })
            return out
        except Exception as e:
            logger.error(f"get_event_schedule_detailed failed: {e}")
            return []

    return await loop.run_in_executor(_executor, _load)


async def get_best_laps_by_session(year: int, round_num: int, session_type: str) -> dict:
    """Return best (fastest) lap time per driver (seconds) for a FastF1 session."""
    session = await load_session(year, round_num, session_type)
    if session is None or not hasattr(session, "laps"):
        return {}

    try:
        laps = session.laps
        laps = laps[laps["LapTime"].notna()]
        if laps.empty:
            return {}

        best = laps.groupby("DriverNumber")["LapTime"].min()
        out = {}
        for dn, lap in best.items():
            try:
                key = int(dn)
            except Exception:
                key = dn
            try:
                secs = float(lap.total_seconds())
            except Exception:
                continue
            out[key] = secs
        return out
    except Exception as e:
        logger.error(f"get_best_laps_by_session failed: {e}")
        return {}


async def get_best_laps_across_sessions(year: int, round_num: int, session_types: list[str]) -> dict:
    """Return best lap time per driver across multiple sessions."""
    best: dict = {}
    for st in session_types:
        laps = await get_best_laps_by_session(year, round_num, st)
        for dn, secs in laps.items():
            if dn not in best or secs < best[dn]:
                best[dn] = secs
    return best
