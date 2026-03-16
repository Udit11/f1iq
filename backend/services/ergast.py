"""
Ergast API via Jolpica mirror — standings, schedule, results.
Base URL: https://api.jolpi.ca/ergast/f1
No API key required.
"""
import httpx
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

BASE = "https://api.jolpi.ca/ergast/f1"
_cache: dict[str, tuple[Any, datetime]] = {}


def _race_status(race_date: str, race_time: str) -> str:
    """
    Determine race status from the scheduled UTC start time instead of only the date.
    """
    if not race_date:
        return "unknown"

    now = datetime.now(timezone.utc)

    start_dt = None
    if race_time:
        try:
            start_dt = datetime.fromisoformat(f"{race_date}T{race_time.replace('Z', '+00:00')}")
        except ValueError:
            start_dt = None

    if start_dt is None:
        today = now.date().isoformat()
        if race_date < today:
            return "completed"
        if race_date == today:
            return "live"
        return "upcoming"

    # Treat the race as live from lights out until a generous post-race window.
    race_end_dt = start_dt + timedelta(hours=4)
    if now < start_dt:
        return "upcoming"
    if now <= race_end_dt:
        return "live"
    return "completed"


async def _get(path: str, ttl: int = 3600) -> Any:
    key = path
    now = datetime.utcnow()
    if key in _cache:
        data, exp = _cache[key]
        if now < exp:
            return data
    url = f"{BASE}{path}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            _cache[key] = (data, now + timedelta(seconds=ttl))
            return data
    except Exception as e:
        logger.error(f"Ergast/Jolpica request failed: {url} — {e}")
        if key in _cache:
            return _cache[key][0]
        return {}


async def get_driver_standings(year: int = None, round_num: int = None) -> list:
    y = year or datetime.utcnow().year
    path = f"/{y}/driverStandings.json" if round_num is None else f"/{y}/{round_num}/driverStandings.json"
    data = await _get(path)
    try:
        standings_list = (
            data["MRData"]["StandingsTable"]["StandingsLists"]
        )
        if not standings_list:
            return []
        out = []
        for entry in standings_list[0]["DriverStandings"]:
            drv = entry["Driver"]
            con = entry["Constructors"][0] if entry["Constructors"] else {}
            out.append({
                "position": int(entry["position"]),
                "driver_id": drv.get("driverId", ""),
                "full_name": f"{drv.get('givenName','')} {drv.get('familyName','')}".strip(),
                "nationality": drv.get("nationality", ""),
                "team": con.get("name", ""),
                "wins": int(entry.get("wins", 0)),
                "points": float(entry.get("points", 0)),
            })
        return out
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Driver standings parse error: {e}")
        return []


async def get_constructor_standings(year: int = None, round_num: int = None) -> list:
    y = year or datetime.utcnow().year
    path = (
        f"/{y}/constructorStandings.json"
        if round_num is None else
        f"/{y}/{round_num}/constructorStandings.json"
    )
    data = await _get(path)
    try:
        standings_list = (
            data["MRData"]["StandingsTable"]["StandingsLists"]
        )
        if not standings_list:
            return []
        out = []
        for entry in standings_list[0]["ConstructorStandings"]:
            con = entry["Constructor"]
            out.append({
                "position": int(entry["position"]),
                "constructor_id": con.get("constructorId", ""),
                "name": con.get("name", ""),
                "nationality": con.get("nationality", ""),
                "wins": int(entry.get("wins", 0)),
                "points": float(entry.get("points", 0)),
            })
        return out
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Constructor standings parse error: {e}")
        return []


async def get_race_schedule(year: int = None) -> list:
    y = year or datetime.utcnow().year
    data = await _get(f"/{y}.json", ttl=86400)
    try:
        races = data["MRData"]["RaceTable"]["Races"]
        out = []
        for race in races:
            race_date = race.get("date", "")
            race_time = race.get("time", "")
            status = _race_status(race_date, race_time)
            circuit = race.get("Circuit", {})
            out.append({
                "round": int(race.get("round", 0)),
                "race_name": race.get("raceName", ""),
                "circuit": circuit.get("circuitName", ""),
                "country": circuit.get("Location", {}).get("country", ""),
                "locality": circuit.get("Location", {}).get("locality", ""),
                "date": race_date,
                "time": race_time,
                "status": status,
                "url": race.get("url", ""),
            })
        return out
    except (KeyError, TypeError) as e:
        logger.error(f"Schedule parse error: {e}")
        return []


async def get_race_results(year: int, round_num: int) -> list:
    data = await _get(f"/{year}/{round_num}/results.json", ttl=86400)
    try:
        races = data["MRData"]["RaceTable"]["Races"]
        if not races:
            return []
        out = []
        for res in races[0]["Results"]:
            drv = res["Driver"]
            con = res["Constructor"]
            fl = res.get("FastestLap", {})
            out.append({
                "position": int(res.get("position", 99)),
                "driver_id": drv.get("driverId", ""),
                "full_name": f"{drv.get('givenName','')} {drv.get('familyName','')}".strip(),
                "abbreviation": drv.get("code", ""),
                "team": con.get("name", ""),
                "laps": int(res.get("laps", 0)),
                "grid": int(res.get("grid", 0)),
                "status": res.get("status", ""),
                "points": float(res.get("points", 0)),
                "fastest_lap_rank": int(fl.get("rank", 99)) if fl else 99,
                "fastest_lap_time": fl.get("Time", {}).get("time", "") if fl else "",
            })
        return out
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Race results parse error: {e}")
        return []


async def get_qualifying_results(year: int, round_num: int) -> list:
    data = await _get(f"/{year}/{round_num}/qualifying.json", ttl=86400)
    try:
        races = data["MRData"]["RaceTable"]["Races"]
        if not races:
            return []
        out = []
        for res in races[0]["QualifyingResults"]:
            drv = res["Driver"]
            con = res["Constructor"]
            out.append({
                "position": int(res.get("position", 99)),
                "driver_id": drv.get("driverId", ""),
                "full_name": f"{drv.get('givenName','')} {drv.get('familyName','')}".strip(),
                "abbreviation": drv.get("code", ""),
                "team": con.get("name", ""),
                "q1": res.get("Q1", ""),
                "q2": res.get("Q2", ""),
                "q3": res.get("Q3", ""),
            })
        return out
    except (KeyError, IndexError, TypeError) as e:
        logger.error(f"Qualifying results parse error: {e}")
        return []
