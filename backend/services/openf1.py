"""
OpenF1 API service — https://openf1.org
Docs: https://openf1.org/#introduction
"""
import asyncio
import httpx
import logging
import time
from datetime import datetime, timedelta
from typing import Optional, Any

logger = logging.getLogger(__name__)

OPENF1_BASE = "https://api.openf1.org/v1"
MIN_REQUEST_INTERVAL_SECONDS = 0.35

# Simple in-memory cache: key -> (data, expires_at)
_cache: dict[str, tuple[Any, datetime]] = {}
_request_lock = asyncio.Lock()
_last_request_ts = 0.0


def _normalise_session_key(session_key: Any) -> Optional[int]:
    """
    Accept ints/strings and discard FastAPI Query(...) objects or other invalid values.
    """
    if session_key is None:
        return None
    if isinstance(session_key, bool):
        return int(session_key)
    if isinstance(session_key, int):
        return session_key
    if isinstance(session_key, str):
        value = session_key.strip()
        if value.isdigit():
            return int(value)
        return None
    return None


async def _throttle_requests() -> None:
    """
    Keep OpenF1 traffic under the public API rate limit.
    """
    global _last_request_ts
    async with _request_lock:
        now = time.monotonic()
        wait_for = MIN_REQUEST_INTERVAL_SECONDS - (now - _last_request_ts)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        _last_request_ts = time.monotonic()


async def _get(path: str, params: dict = None, ttl_seconds: int = 4) -> Any:
    """
    GET from OpenF1 with short-lived cache.
    Live endpoints use ttl=4s (refreshes during race).
    Historical endpoints use ttl=300s.
    """
    cache_key = path + str(sorted((params or {}).items()))
    now = datetime.utcnow()

    if cache_key in _cache:
        data, expires = _cache[cache_key]
        if now < expires:
            return data

    url = f"{OPENF1_BASE}{path}"
    try:
        await _throttle_requests()
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            _cache[cache_key] = (data, now + timedelta(seconds=ttl_seconds))
            return data
    except httpx.HTTPError as e:
        logger.error(f"OpenF1 request failed: {url} — {e}")
        # Return stale cache if available
        if cache_key in _cache:
            return _cache[cache_key][0]
        return []


# ── Session ──────────────────────────────────────

async def get_latest_session() -> dict:
    """Get the most recent or currently live session."""
    data = await _get("/sessions", {"session_key": "latest"}, ttl_seconds=30)
    if data:
        return data[-1]
    return {}


async def get_session(session_key: int) -> dict:
    session_key = _normalise_session_key(session_key)
    if session_key is None:
        return {}
    data = await _get("/sessions", {"session_key": session_key}, ttl_seconds=300)
    return data[0] if data else {}


async def get_sessions_for_year(year: int) -> list:
    return await _get("/sessions", {"year": year}, ttl_seconds=300)


# ── Drivers ──────────────────────────────────────

async def get_drivers(session_key: int = None) -> list:
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    return await _get("/drivers", params, ttl_seconds=60)


# ── Positions ────────────────────────────────────

async def get_positions(session_key: int = None) -> list:
    """Latest position for each driver."""
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    data = await _get("/position", params, ttl_seconds=4)
    # Keep only the latest entry per driver
    latest: dict[int, dict] = {}
    for entry in data:
        dn = entry.get("driver_number")
        if dn and (dn not in latest or entry["date"] > latest[dn]["date"]):
            latest[dn] = entry
    return list(latest.values())


# ── Intervals / Gaps ─────────────────────────────

async def get_intervals(session_key: int = None) -> list:
    """Gap to leader and interval for each driver."""
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    data = await _get("/intervals", params, ttl_seconds=4)
    latest: dict[int, dict] = {}
    for entry in data:
        dn = entry.get("driver_number")
        if dn and (dn not in latest or entry["date"] > latest[dn]["date"]):
            latest[dn] = entry
    return list(latest.values())


# ── Laps ─────────────────────────────────────────

async def get_latest_laps(session_key: int = None) -> list:
    """Most recent completed lap per driver."""
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    data = await _get("/laps", params, ttl_seconds=10)
    latest: dict[int, dict] = {}
    for entry in data:
        dn = entry.get("driver_number")
        lap = entry.get("lap_number", 0)
        if dn and (dn not in latest or lap > latest[dn].get("lap_number", 0)):
            latest[dn] = entry
    return list(latest.values())


async def get_all_laps(session_key: int = None, driver_number: int = None) -> list:
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    if driver_number:
        params["driver_number"] = driver_number
    return await _get("/laps", params, ttl_seconds=30)


# ── Car Data (telemetry) ─────────────────────────

async def get_car_data(driver_number: int, session_key: int = None) -> list:
    """Latest telemetry samples for a driver."""
    params = {
        "session_key": _normalise_session_key(session_key) or "latest",
        "driver_number": driver_number,
    }
    data = await _get("/car_data", params, ttl_seconds=2)
    # Return last 50 samples
    return data[-50:] if len(data) > 50 else data


# ── Stints (tyre data) ───────────────────────────

async def get_stints(session_key: int = None, driver_number: int = None) -> list:
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    if driver_number:
        params["driver_number"] = driver_number
    return await _get("/stints", params, ttl_seconds=15)


# ── Pit Stops ────────────────────────────────────

async def get_pit_stops(session_key: int = None) -> list:
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    return await _get("/pit", params, ttl_seconds=10)


# ── Race Control ─────────────────────────────────

async def get_race_control(session_key: int = None) -> list:
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    data = await _get("/race_control", params, ttl_seconds=5)
    # Most recent messages first, cap at 50
    return list(reversed(data))[:50]


# ── Weather ──────────────────────────────────────

async def get_weather(session_key: int = None) -> dict:
    params = {"session_key": _normalise_session_key(session_key) or "latest"}
    data = await _get("/weather", params, ttl_seconds=30)
    return data[-1] if data else {}


# ── Location (track map) ─────────────────────────

async def get_location(driver_number: int, session_key: int = None) -> list:
    """X/Y car positions for track map."""
    params = {
        "session_key": _normalise_session_key(session_key) or "latest",
        "driver_number": driver_number,
    }
    data = await _get("/location", params, ttl_seconds=2)
    return data[-20:] if len(data) > 20 else data


# ── Meetings ─────────────────────────────────────

async def get_meetings(year: int = None) -> list:
    params = {}
    if year:
        params["year"] = year
    return await _get("/meetings", params, ttl_seconds=3600)
