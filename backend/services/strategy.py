"""
Pit Strategy Calculator.

Computes optimal pit windows, undercut/overcut recommendations,
and tyre degradation estimates from live OpenF1 data.
"""
import logging
from typing import Optional
from ..services import openf1

logger = logging.getLogger(__name__)

# Tyre degradation model — seconds lost per additional lap on compound
DEG_RATES = {
    "SOFT":   0.095,
    "MEDIUM": 0.055,
    "HARD":   0.030,
    "INTERMEDIATE": 0.040,
    "WET":    0.025,
}

# Minimum / maximum viable stint lengths (laps)
STINT_LIMITS = {
    "SOFT":   {"min": 8,  "max": 28},
    "MEDIUM": {"min": 14, "max": 45},
    "HARD":   {"min": 18, "max": 55},
    "INTERMEDIATE": {"min": 5,  "max": 40},
    "WET":    {"min": 5,  "max": 50},
}


def _tyre_health(compound: str, age: int) -> float:
    """Estimate tyre health 0–100 based on compound and age."""
    limits = STINT_LIMITS.get(compound, {"max": 35})
    max_laps = limits["max"]
    health = max(0.0, 100.0 - (age / max_laps) * 100.0)
    return round(health, 1)


def _deg_penalty(compound: str, age: int) -> float:
    """Extra lap time (seconds) due to degradation."""
    rate = DEG_RATES.get(compound, 0.06)
    # Degradation accelerates after 60% of max stint
    limits = STINT_LIMITS.get(compound, {"max": 35})
    cliff = limits["max"] * 0.60
    if age > cliff:
        rate *= 1 + (age - cliff) / limits["max"] * 2
    return round(rate * age, 3)


def _optimal_window(
    current_lap: int,
    total_laps: int,
    compound: str,
    tyre_age: int,
    gap_to_ahead: Optional[float],
    gap_to_behind: Optional[float],
) -> tuple[Optional[int], Optional[int]]:
    """
    Calculate optimal pit window start and end laps.
    Returns (window_start, window_end) or (None, None) if no pit needed.
    """
    remaining = total_laps - current_lap
    limits = STINT_LIMITS.get(compound, {"min": 10, "max": 35})
    remaining_tyre_life = limits["max"] - tyre_age

    # Already past cliff — immediate pit
    if remaining_tyre_life <= 0:
        return current_lap + 1, current_lap + 3

    # Don't need to pit if tyre lasts to the end
    if remaining_tyre_life >= remaining:
        return None, None

    # Optimal window: pit 5–10 laps before tyre runs out
    window_start = current_lap + max(1, remaining_tyre_life - 10)
    window_end = current_lap + remaining_tyre_life - 2

    # Clamp to race end
    window_start = min(window_start, total_laps - 5)
    window_end = min(window_end, total_laps - 2)

    return int(window_start), int(window_end)


def _classify_recommendation(
    compound: str,
    tyre_age: int,
    window_start: Optional[int],
    window_end: Optional[int],
    current_lap: int,
) -> str:
    """Return 'optimal', 'alert', or 'danger'."""
    if window_start is None:
        return "optimal"
    laps_left_in_window = (window_start or current_lap) - current_lap
    health = _tyre_health(compound, tyre_age)

    if health < 20 or laps_left_in_window < 0:
        return "danger"
    elif health < 45 or laps_left_in_window <= 5:
        return "alert"
    return "optimal"


def _build_recommendation_text(
    driver: str,
    position: int,
    compound: str,
    tyre_age: int,
    rec_type: str,
    window_start: Optional[int],
    window_end: Optional[int],
    current_lap: int,
    total_laps: int,
    gap_ahead: Optional[float],
) -> tuple[str, str, list[str]]:
    """Returns (title, text, tactics)."""
    remaining = total_laps - current_lap
    health = _tyre_health(compound, tyre_age)
    deg = _deg_penalty(compound, tyre_age)

    if window_start is None:
        title = "Stay Out — Tyre Life Sufficient"
        text = (
            f"Current {compound} tyre has {health:.0f}% health remaining. "
            f"Estimated {STINT_LIMITS.get(compound,{}).get('max',35) - tyre_age} viable laps left — "
            f"sufficient to reach the end of the race. "
            f"Degradation penalty currently +{deg:.3f}s per lap. "
            f"Recommend staying out unless rivals force a strategic response."
        )
        tactics = ["stay"]
        if gap_ahead and gap_ahead < 2.0:
            tactics.append("overcut")
        return title, text, tactics

    laps_to_window = window_start - current_lap

    if rec_type == "danger":
        title = f"⚠ PIT IMMEDIATELY — Tyre at {health:.0f}% Health"
        text = (
            f"{driver} on {compound} tyre aged {tyre_age} laps — "
            f"health critical at {health:.0f}%. "
            f"Lap time delta widening at +{DEG_RATES.get(compound, 0.06):.3f}s per additional lap. "
            f"Immediate pit recommended. Any further delay risks blistering or failure."
        )
        tactics = ["pit-now"]
    elif rec_type == "alert":
        title = f"Pit Window: Laps {window_start}–{window_end}"
        text = (
            f"{driver} P{position} on {tyre_age}-lap {compound}. "
            f"Tyre health at {health:.0f}% — entering degradation phase. "
            f"Optimal pit window opens lap {window_start} (in {laps_to_window} laps). "
            f"{"Undercut opportunity if rival pits first. " if gap_ahead and gap_ahead < 5 else ""}"
            f"After stop: {remaining - laps_to_window} laps to end on fresh compound."
        )
        tactics = ["alert"]
        if gap_ahead and gap_ahead < 3.0:
            tactics = ["undercut", "pit-now"]
        elif gap_ahead and gap_ahead > 8.0:
            tactics = ["overcut", "stay"]
        else:
            tactics = ["undercut", "stay"]
    else:
        title = "Strategy On Track — No Action Required"
        text = (
            f"{driver} P{position} managing {compound} tyre well at {health:.0f}% health. "
            f"Pit window opens lap {window_start} (in {laps_to_window} laps). "
            f"Current pace sustainable for {STINT_LIMITS.get(compound,{}).get('max',35) - tyre_age} more laps. "
            f"Monitor rivals' strategies."
        )
        tactics = ["stay"]
        if gap_ahead and gap_ahead < 2.0:
            tactics = ["overcut", "stay"]

    return title, text, tactics


async def get_strategy_recommendations(session_key: int = None) -> list:
    """
    Build strategy recommendations for all drivers using live OpenF1 data.
    """
    # Fetch all needed data in parallel
    import asyncio
    drivers_data, stints_data, positions_data, intervals_data, laps_data, session_data = await asyncio.gather(
        openf1.get_drivers(session_key),
        openf1.get_stints(session_key),
        openf1.get_positions(session_key),
        openf1.get_intervals(session_key),
        openf1.get_latest_laps(session_key),
        openf1.get_latest_session(),
    )

    # Index by driver number
    drivers_by_num = {d["driver_number"]: d for d in drivers_data}
    positions_by_num = {p["driver_number"]: p for p in positions_data}
    intervals_by_num = {i["driver_number"]: i for i in intervals_data}
    laps_by_num = {l["driver_number"]: l for l in laps_data}

    # Latest stint per driver (highest stint_number)
    stints_by_num: dict[int, dict] = {}
    all_stints_by_num: dict[int, list] = {}
    for s in stints_data:
        dn = s.get("driver_number")
        if dn is None:
            continue
        all_stints_by_num.setdefault(dn, []).append(s)
        if dn not in stints_by_num or s.get("stint_number", 0) > stints_by_num[dn].get("stint_number", 0):
            stints_by_num[dn] = s

    # Session metadata
    current_lap = laps_data[0].get("lap_number", 1) if laps_data else 1
    total_laps = session_data.get("total_laps") or 60

    results = []
    sorted_drivers = sorted(
        drivers_by_num.values(),
        key=lambda d: positions_by_num.get(d["driver_number"], {}).get("position", 99)
    )

    for driver in sorted_drivers[:10]:   # Top 10 only
        dn = driver["driver_number"]
        pos_data = positions_by_num.get(dn, {})
        int_data = intervals_by_num.get(dn, {})
        stint = stints_by_num.get(dn, {})
        all_driver_stints = all_stints_by_num.get(dn, [])

        position = pos_data.get("position", 99)
        compound = stint.get("compound", "MEDIUM")
        tyre_age = stint.get("lap_end", current_lap) - stint.get("lap_start", 1) + 1

        # Parse gap
        gap_raw = int_data.get("gap_to_leader")
        gap_to_leader: Optional[float] = None
        if gap_raw and isinstance(gap_raw, (int, float)):
            gap_to_leader = float(gap_raw)

        int_raw = int_data.get("interval")
        interval: Optional[float] = None
        if int_raw and isinstance(int_raw, (int, float)):
            interval = float(int_raw)

        window_start, window_end = _optimal_window(
            current_lap, total_laps, compound, tyre_age,
            interval, None
        )
        rec_type = _classify_recommendation(compound, tyre_age, window_start, window_end, current_lap)
        title, text, tactics = _build_recommendation_text(
            driver.get("name_acronym", ""), position, compound, tyre_age,
            rec_type, window_start, window_end, current_lap, total_laps, interval
        )

        # Build stint list for visualisation
        stint_list = []
        sorted_stints = sorted(all_driver_stints, key=lambda s: s.get("stint_number", 0))
        for s in sorted_stints:
            sn = s.get("stint_number", 0)
            s_start = s.get("lap_start", 1)
            s_end = s.get("lap_end") or (current_lap if sn == len(sorted_stints) else s_start)
            stint_list.append({
                "compound": s.get("compound", "UNKNOWN"),
                "start_lap": s_start,
                "end_lap": s_end,
                "lap_count": s_end - s_start + 1,
            })

        pit_laps = [s.get("lap_start", 0) for s in sorted_stints[1:]]  # Each stint start = pit lap

        results.append({
            "driver_number": dn,
            "name_acronym": driver.get("name_acronym", ""),
            "full_name": driver.get("full_name", ""),
            "team_name": driver.get("team_name", ""),
            "team_colour": "#" + driver.get("team_colour", "888888"),
            "current_position": position,
            "stints": stint_list,
            "pit_laps_done": pit_laps,
            "current_lap": current_lap,
            "total_laps": total_laps,
            "compound": compound,
            "tyre_age": tyre_age,
            "tyre_health": _tyre_health(compound, tyre_age),
            "recommendation_type": rec_type,
            "recommendation_title": title,
            "recommendation_text": text,
            "tactics": tactics,
            "optimal_pit_window_start": window_start,
            "optimal_pit_window_end": window_end,
        })

    return results
