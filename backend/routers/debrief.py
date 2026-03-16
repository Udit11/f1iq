"""
/api/debrief/* -- real-data post-race debrief analytics.
"""
import asyncio
import math
import logging
import statistics
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Body, Query

from ..services import ergast, fastf1_service
from ..services.gemini_service import (
    answer_debrief_question_with_gemini,
    gemini_available,
)

router = APIRouter(prefix="/api/debrief", tags=["debrief"])
logger = logging.getLogger(__name__)

TEAM_ALIASES = {
    "redbull": {"redbull", "redbullracing", "oracleredbullracing"},
    "racingbulls": {"racingbulls", "rb", "visacashapprb", "visacashappracingbulls"},
    "sauber": {"sauber", "kicksauber", "stakef1teamkicksauber"},
    "mercedes": {"mercedes", "mercedesamgf1", "mercedesamgpetronas"},
    "astonmartin": {"astonmartin", "astonmartinaramco"},
    "mclaren": {"mclaren"},
    "ferrari": {"ferrari"},
    "alpine": {"alpine"},
    "williams": {"williams"},
    "haas": {"haas"},
}


def _norm(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _team_keys(name: str) -> set[str]:
    base = _norm(name)
    if not base:
        return set()
    keys = {base}
    for alias_group in TEAM_ALIASES.values():
        if base in alias_group:
            keys.update(alias_group)
    return keys


def _team_match(candidate: str, team_name: str) -> bool:
    cand_keys = _team_keys(candidate)
    team_keys = _team_keys(team_name)
    if not cand_keys or not team_keys:
        return False
    if cand_keys & team_keys:
        return True
    return any(a in b or b in a for a in cand_keys for b in team_keys)


def _driver_code(name: str) -> str:
    parts = (name or "").split()
    if not parts:
        return "DRV"
    if len(parts) == 1:
        return parts[0][:3].upper()
    return (parts[0][0] + parts[-1][:2]).upper()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _fmt_pos(value: Optional[int]) -> str:
    return f"P{value}" if isinstance(value, int) and value > 0 else "unclassified"


def _fmt_delta(delta: Optional[int]) -> str:
    if delta is None:
        return "flat"
    if delta > 0:
        return f"+{delta}"
    return str(delta)


def _fmt_seconds(value: Optional[float]) -> str:
    if value is None or not math.isfinite(value):
        return "n/a"
    mins = int(value // 60)
    secs = value - mins * 60
    return f"{mins}:{secs:06.3f}"


def _latest_completed(schedule: list[dict]) -> Optional[dict]:
    completed = [race for race in schedule if race.get("status") == "completed"]
    if completed:
        return max(completed, key=lambda race: race.get("round", 0))
    return None


def _completed_races(schedule: list[dict]) -> list[dict]:
    return sorted(
        [race for race in schedule if race.get("status") == "completed"],
        key=lambda race: race.get("round", 0),
        reverse=True,
    )


async def _resolve_race(year: Optional[int], round_num: Optional[int]) -> tuple[Optional[int], Optional[int], Optional[dict], list[dict]]:
    season = year or datetime.now(timezone.utc).year
    schedule = await ergast.get_race_schedule(season)
    completed = _completed_races(schedule)
    if round_num is not None:
        return season, round_num, next((item for item in schedule if item.get("round") == round_num), None), completed

    race = completed[0] if completed else None
    if race:
        return season, race.get("round"), race, completed

    previous_season = season - 1
    if previous_season >= 1950:
        previous_schedule = await ergast.get_race_schedule(previous_season)
        previous_completed = _completed_races(previous_schedule)
        previous_race = previous_completed[0] if previous_completed else (previous_schedule[-1] if previous_schedule else None)
        if previous_race:
            return previous_season, previous_race.get("round"), previous_race, previous_completed

    return season, None, None, completed


def _parse_session_results(session) -> list[dict]:
    try:
        rows = []
        for _, row in session.results.iterrows():
            fl = row.get("FastestLapTime")
            rows.append({
                "driver": row.get("Abbreviation", ""),
                "full_name": f"{row.get('FirstName', '')} {row.get('LastName', '')}".strip(),
                "team": row.get("TeamName", ""),
                "position": _safe_int(row.get("Position"), 99),
                "grid_position": _safe_int(row.get("GridPosition"), 0),
                "points": _safe_float(row.get("Points"), 0.0),
                "status": row.get("Status", ""),
                "fastest_lap": str(fl) if fl else None,
                "laps_completed": _safe_int(row.get("NumberOfLaps"), 0),
                "pit_stops": _safe_int(row.get("NumberOfPitStops"), 0),
            })
        return sorted(rows, key=lambda item: item["position"])
    except Exception:
        return []


def _parse_stints(session) -> list[dict]:
    try:
        grouped = {}
        for _, lap in session.laps.iterrows():
            driver = lap.get("Driver", "")
            stint = _safe_int(lap.get("Stint"), 0)
            key = (driver, stint)
            lap_number = _safe_int(lap.get("LapNumber"), 0)
            if key not in grouped:
                grouped[key] = {
                    "driver": driver,
                    "team": lap.get("Team", ""),
                    "stint": stint,
                    "compound": lap.get("Compound", ""),
                    "start_lap": lap_number,
                    "end_lap": lap_number,
                    "laps": 1,
                }
            else:
                grouped[key]["end_lap"] = lap_number
                grouped[key]["laps"] += 1
        return list(grouped.values())
    except Exception:
        return []


def _pace_by_driver(session) -> dict[str, float]:
    try:
        samples: dict[str, list[float]] = {}
        for _, lap in session.laps.iterrows():
            lap_time = lap.get("LapTime")
            if not lap_time or not hasattr(lap_time, "total_seconds"):
                continue
            lap_seconds = float(lap_time.total_seconds())
            if not math.isfinite(lap_seconds):
                continue
            driver = lap.get("Driver", "")
            samples.setdefault(driver, []).append(lap_seconds)
        return {
            driver: statistics.median(times)
            for driver, times in samples.items()
            if times
        }
    except Exception:
        return {}


def _weather_summary(session) -> dict:
    try:
        rows = []
        for _, row in session.weather_data.iterrows():
            rows.append({
                "air_temp": _safe_float(row.get("AirTemp"), 0.0),
                "track_temp": _safe_float(row.get("TrackTemp"), 0.0),
                "humidity": _safe_float(row.get("Humidity"), 0.0),
                "rainfall": bool(row.get("Rainfall", False)),
            })
        if not rows:
            return {"summary": "No race weather sample was available."}
        air_vals = [item["air_temp"] for item in rows if math.isfinite(item["air_temp"])]
        track_vals = [item["track_temp"] for item in rows if math.isfinite(item["track_temp"])]
        humidity_vals = [item["humidity"] for item in rows if math.isfinite(item["humidity"])]
        if not air_vals or not track_vals or not humidity_vals:
            return {"summary": "No race weather sample was available."}
        air = statistics.mean(air_vals)
        track = statistics.mean(track_vals)
        humidity = statistics.mean(humidity_vals)
        wet_ratio = sum(1 for item in rows if item["rainfall"]) / len(rows)
        summary = (
            f"Average air temperature was {air:.1f}C and track temperature averaged {track:.1f}C. "
            f"Humidity sat around {humidity:.0f}%."
        )
        summary += (
            f" Rain was present in {wet_ratio * 100:.0f}% of sampled laps."
            if wet_ratio > 0 else
            " The race ran in dry conditions."
        )
        return {
            "avg_air_temp": round(air, 1),
            "avg_track_temp": round(track, 1),
            "avg_humidity": round(humidity, 0),
            "wet_ratio": round(wet_ratio, 2),
            "summary": summary,
        }
    except Exception:
        return {"summary": "No race weather sample was available."}


def _find_team_entry(entries: list[dict], team_name: str, key: str = "team") -> Optional[dict]:
    return next((entry for entry in entries if _team_match(entry.get(key, ""), team_name)), None)


def _find_driver_entry(entries: list[dict], full_name: str) -> Optional[dict]:
    target = _norm(full_name)
    return next((entry for entry in entries if _norm(entry.get("full_name", "")) == target), None)


def _is_retirement(status: str) -> bool:
    lower = (status or "").lower()
    return bool(lower) and "finished" not in lower and "lap" not in lower


def _grid_delta(item: dict) -> Optional[int]:
    finish_position = item.get("position")
    grid_position = item.get("grid_position", item.get("grid"))
    if not grid_position or finish_position is None or finish_position >= 90:
        return None
    return grid_position - finish_position


def _decorate_constructors(constructors: list[dict], results: list[dict]) -> list[dict]:
    decorated = []
    for constructor in constructors:
        copy = dict(constructor)
        copy["race_points"] = sum(
            item.get("points", 0.0)
            for item in results
            if _team_match(item.get("team", ""), constructor.get("name", ""))
        )
        copy["best_finish"] = min(
            (
                item.get("position", 99)
                for item in results
                if _team_match(item.get("team", ""), constructor.get("name", ""))
            ),
            default=None,
        )
        decorated.append(copy)
    return decorated


def _driver_review_note(review: dict) -> str:
    status = (review.get("status") or "").lower()
    delta = review.get("delta")
    if status and "finished" not in status and "lap" not in status:
        return f"{review['driver']} failed to reach the flag: {review['status']}."
    if delta is not None and delta >= 3:
        return f"{review['driver']} climbed from {_fmt_pos(review['grid_position'])} to {_fmt_pos(review['finish_position'])} on race pace."
    if delta is not None and delta <= -3:
        return f"{review['driver']} lost ground relative to the grid and spent the race recovering."
    if review.get("pit_stops", 0) >= 2:
        return f"{review['driver']} ran a higher-stop race that increased traffic exposure."
    return f"{review['driver']} delivered a relatively stable race from {_fmt_pos(review['grid_position'])} to {_fmt_pos(review['finish_position'])}."


def _strategy_shape(team_stints: list[dict]) -> dict:
    if not team_stints:
        return {
            "label": "Unavailable",
            "summary": "FastF1 stint history was not available for this race.",
            "notes": [],
            "confidence": 0.2,
        }

    stops_by_driver = {}
    compounds_by_driver = {}
    for stint in sorted(team_stints, key=lambda item: (item.get("driver", ""), item.get("stint", 0))):
        driver = stint.get("driver", "")
        stops_by_driver[driver] = max(stops_by_driver.get(driver, 0), max(stint.get("stint", 1) - 1, 0))
        compounds_by_driver.setdefault(driver, []).append(
            f"{stint.get('compound', 'UNK')} L{stint.get('start_lap', '?')}-{stint.get('end_lap', '?')}"
        )

    stop_counts = list(stops_by_driver.values())
    average_stops = statistics.mean(stop_counts)
    if len(set(stop_counts)) > 1:
        label = "Split strategy"
    elif average_stops >= 2:
        label = "Aggressive multi-stop"
    elif average_stops <= 1:
        label = "Conservative one-stop"
    else:
        label = "Balanced"

    notes = [
        f"{driver}: {stops_by_driver[driver]} stops, {' | '.join(compounds_by_driver.get(driver, []))}"
        for driver in sorted(stops_by_driver)
    ]
    summary = (
        f"{label}. The team averaged {average_stops:.1f} stops per car and covered "
        f"{len({stint.get('compound', '') for stint in team_stints if stint.get('compound')})} compounds."
    )
    return {
        "label": label,
        "summary": summary,
        "notes": notes,
        "confidence": 0.73,
    }


def _team_summary(team_name: str, team_results: list[dict], constructor_now: Optional[dict], race_name: str) -> tuple[str, str]:
    if not team_results:
        executive = f"{team_name} has no classified race-result data for {race_name}."
        return executive, executive

    scored = sum(item.get("points", 0.0) for item in team_results)
    grids = [item.get("grid_position", 0) for item in team_results if item.get("grid_position", 0) > 0]
    finishes = [item.get("position", 99) for item in team_results if item.get("position", 99) < 90]
    net_gain = (sum(grids) - sum(finishes)) if grids and finishes else 0
    current_pos = constructor_now.get("position") if constructor_now else None

    executive = (
        f"{team_name} left {race_name} with {scored:.0f} points and "
        f"{'a net gain' if net_gain >= 0 else 'a net loss'} of {abs(net_gain)} grid places across both cars. "
        f"The team currently sits {_fmt_pos(current_pos)} in the constructors' standings."
        if current_pos else
        f"{team_name} scored {scored:.0f} points in {race_name}."
    )
    detailed = (
        f"{team_name} came away from {race_name} with {scored:.0f} points. "
        f"Across both cars the team converted a combined grid tally of {sum(grids) if grids else 'n/a'} "
        f"into a combined finish tally of {sum(finishes) if finishes else 'n/a'}, "
        f"which translates to a {_fmt_delta(net_gain)} swing in track position. "
        f"The race outcome was shaped more by execution than raw headline pace, with one car often setting the ceiling and the other exposing the weak point. "
        f"That leaves the team at {_fmt_pos(current_pos)} in the championship."
        if current_pos else
        f"{team_name} produced a data-backed but partial debrief because championship context was unavailable."
    )
    return executive, detailed


def _championship_impact(
    team_name: str,
    constructor_now: Optional[dict],
    constructor_prev: Optional[dict],
    driver_now: list[dict],
    driver_prev: list[dict],
    race_points: float,
) -> dict:
    if not constructor_now:
        return {
            "summary": f"Championship context for {team_name} was unavailable.",
            "constructor": None,
            "drivers": [],
        }

    leader_points = constructor_now.get("leader_points", constructor_now.get("points", 0.0))
    gap_to_leader = max(0.0, leader_points - constructor_now.get("points", 0.0))
    prev_gap = None
    if constructor_prev and constructor_prev.get("leader_points") is not None:
        prev_gap = max(0.0, constructor_prev["leader_points"] - constructor_prev.get("points", 0.0))

    constructor_block = {
        "position": constructor_now.get("position"),
        "previous_position": constructor_prev.get("position") if constructor_prev else None,
        "points": constructor_now.get("points"),
        "previous_points": constructor_prev.get("points") if constructor_prev else None,
        "race_points": race_points,
        "gap_to_leader": gap_to_leader,
        "gap_change": (prev_gap - gap_to_leader) if prev_gap is not None else None,
    }
    driver_blocks = []
    for driver in driver_now:
        previous = _find_driver_entry(driver_prev, driver.get("full_name", ""))
        driver_blocks.append({
            "name": driver.get("full_name"),
            "position": driver.get("position"),
            "previous_position": previous.get("position") if previous else None,
            "points": driver.get("points"),
            "previous_points": previous.get("points") if previous else None,
            "team": driver.get("team"),
        })

    position_delta = (
        constructor_prev.get("position", constructor_now.get("position")) - constructor_now.get("position")
        if constructor_prev else 0
    )
    summary = (
        f"{team_name} scored {race_points:.0f} points and now sits {_fmt_pos(constructor_now.get('position'))} "
        f"on {constructor_now.get('points', 0):.0f} points, {gap_to_leader:.0f} behind the championship lead."
    )
    if constructor_prev:
        if position_delta > 0:
            summary += f" The team climbed {position_delta} place(s) relative to the previous round."
        elif position_delta < 0:
            summary += f" The team lost {abs(position_delta)} place(s) relative to the previous round."
        else:
            summary += " The team held the same constructors' position as the previous round."

    return {
        "summary": summary,
        "constructor": constructor_block,
        "drivers": driver_blocks,
    }


def _build_action_items(team_results: list[dict], strategy: dict, championship: dict) -> list[str]:
    actions: list[str] = []
    if any(item.get("status") and "finished" not in item.get("status", "").lower() and "lap" not in item.get("status", "").lower() for item in team_results):
        actions.append("Close the reliability and execution issues that prevented a clean finish across both cars.")
    deltas = [item.get("grid_position", 0) - item.get("position", 99) for item in team_results if item.get("grid_position", 0) > 0 and item.get("position", 99) < 90]
    if deltas and statistics.mean(deltas) < 0:
        actions.append("Convert qualifying position more cleanly on Sunday; race management cost net track position.")
    if strategy.get("label") == "Aggressive multi-stop":
        actions.append("Reduce tyre degradation or pit-loss sensitivity so the team is not forced onto the higher-stop branch.")
    elif strategy.get("label") == "Conservative one-stop":
        actions.append("Protect long-run tyre life while finding one more pace step in the final stint.")
    if championship.get("constructor") and championship["constructor"].get("gap_to_leader", 999) > 25:
        actions.append("Prioritise maximising both cars in the points to stop the championship gap from opening further.")
    if not actions:
        actions.append("Keep refining race execution because the weekend was broadly clean but still left small margins on the table.")
    return actions[:4]


def _build_incidents(team_results: list[dict], weather: dict) -> list[str]:
    incidents = []
    for item in team_results:
        status = item.get("status", "")
        lower = status.lower()
        if status and "finished" not in lower and "lap" not in lower:
            incidents.append(f"{item.get('driver') or item.get('full_name')} did not see the flag: {status}.")
    if weather.get("wet_ratio", 0) > 0:
        incidents.append("Weather changed the strategic risk profile, so pit timing and tyre calls carried more downside than a dry baseline.")
    if not incidents:
        incidents.append("No major incident was logged for this team; the result was driven mainly by pace, tyre life, and track position.")
    return incidents[:3]


def _build_driver_reviews(
    team_name: str,
    team_results: list[dict],
    team_qualifying: list[dict],
    team_drivers_now: list[dict],
    pace_lookup: dict[str, float],
) -> list[dict]:
    qual_by_name = {_norm(item.get("full_name", "")): item for item in team_qualifying}
    standings_by_name = {_norm(item.get("full_name", "")): item for item in team_drivers_now}
    reviews = []
    for item in team_results:
        full_name = item.get("full_name", "")
        qual = qual_by_name.get(_norm(full_name), {})
        standings = standings_by_name.get(_norm(full_name), {})
        finish_position = item.get("position") if item.get("position", 99) < 90 else None
        grid_position = item.get("grid_position") or qual.get("position")
        delta = (grid_position - finish_position) if grid_position and finish_position else None
        review = {
            "driver": item.get("driver") or _driver_code(full_name),
            "full_name": full_name,
            "team": team_name,
            "finish_position": finish_position,
            "grid_position": grid_position,
            "delta": delta,
            "points": item.get("points", 0.0),
            "status": item.get("status", ""),
            "pit_stops": item.get("pit_stops", 0),
            "average_lap_s": pace_lookup.get(item.get("driver", "")),
            "championship_position": standings.get("position"),
            "championship_points": standings.get("points"),
            "qualifying_position": qual.get("position"),
        }
        review["note"] = _driver_review_note(review)
        reviews.append(review)
    return sorted(reviews, key=lambda item: item.get("finish_position") or 99)


def _build_over_under(driver_reviews: list[dict]) -> tuple[list[dict], list[dict]]:
    enriched = []
    for review in driver_reviews:
        delta = review.get("delta")
        if delta is None:
            continue
        enriched.append({
            "driver": review.get("driver"),
            "full_name": review.get("full_name"),
            "delta": delta,
            "note": review.get("note"),
        })
    over = sorted([item for item in enriched if item["delta"] > 0], key=lambda item: item["delta"], reverse=True)
    under = sorted([item for item in enriched if item["delta"] < 0], key=lambda item: item["delta"])
    return over[:2], under[:2]


def _build_biggest_calls(driver_reviews: list[dict]) -> tuple[dict, dict]:
    if not driver_reviews:
        empty = {"title": "Unavailable", "detail": "No classified team result was available."}
        return empty, empty

    best = max(
        driver_reviews,
        key=lambda item: (
            item.get("delta") if item.get("delta") is not None else -999,
            -(item.get("finish_position") or 99),
        ),
    )
    worst = min(
        driver_reviews,
        key=lambda item: (
            item.get("delta") if item.get("delta") is not None else 999,
            item.get("finish_position") or 99,
        ),
    )
    biggest_win = {
        "title": f"{best['driver']} gained the most",
        "detail": f"{best['full_name']} moved from {_fmt_pos(best.get('grid_position'))} to {_fmt_pos(best.get('finish_position'))}, a {_fmt_delta(best.get('delta'))} swing.",
    }
    biggest_loss = {
        "title": f"{worst['driver']} lost the most",
        "detail": f"{worst['full_name']} went from {_fmt_pos(worst.get('grid_position'))} to {_fmt_pos(worst.get('finish_position'))}. {worst.get('note')}",
    }
    return biggest_win, biggest_loss


def _race_strategy_overview(stint_rows: list[dict]) -> dict:
    if not stint_rows:
        return {
            "label": "Unavailable",
            "summary": "FastF1 stint history was not available for this race.",
            "notes": [],
        }

    stops_by_driver: dict[str, int] = {}
    for stint in stint_rows:
        driver = stint.get("driver", "")
        stops_by_driver[driver] = max(stops_by_driver.get(driver, 0), max(stint.get("stint", 1) - 1, 0))

    stop_counts = list(stops_by_driver.values())
    average_stops = statistics.mean(stop_counts) if stop_counts else 0.0
    histogram = {
        stop_count: sum(1 for item in stop_counts if item == stop_count)
        for stop_count in sorted(set(stop_counts))
    }
    if average_stops <= 1.2:
        label = "One-stop leaning"
    elif average_stops >= 1.8:
        label = "Two-stop leaning"
    else:
        label = "Mixed stop count"

    notes = [
        f"{count} driver(s) completed the race with {stops} stop{'s' if stops != 1 else ''}."
        for stops, count in histogram.items()
    ]
    summary = (
        f"{label}. The field averaged {average_stops:.1f} stops per car, "
        f"which points to {'track-position preservation' if average_stops <= 1.2 else 'more aggressive stint splitting'}."
    )
    return {
        "label": label,
        "summary": summary,
        "notes": notes,
        "average_stops": round(average_stops, 1),
    }


def _race_championship_impact(
    driver_now: list[dict],
    driver_prev: list[dict],
    constructor_now: list[dict],
    constructor_prev: list[dict],
) -> dict:
    driver_leader = driver_now[0] if driver_now else None
    previous_driver_leader = driver_prev[0] if driver_prev else None
    constructor_leader = constructor_now[0] if constructor_now else None
    previous_constructor_leader = constructor_prev[0] if constructor_prev else None

    summary_parts = []
    if driver_leader:
        summary_parts.append(
            f"{driver_leader.get('full_name')} leaves the round leading the drivers' standings on {driver_leader.get('points', 0):.0f} points."
        )
    if previous_driver_leader and driver_leader and _norm(previous_driver_leader.get("full_name", "")) != _norm(driver_leader.get("full_name", "")):
        summary_parts.append("The race changed the identity of the championship leader.")
    if constructor_leader:
        summary_parts.append(
            f"{constructor_leader.get('name')} leads the constructors on {constructor_leader.get('points', 0):.0f} points."
        )
    if previous_constructor_leader and constructor_leader and _norm(previous_constructor_leader.get("name", "")) != _norm(constructor_leader.get("name", "")):
        summary_parts.append("The constructors' lead changed hands this weekend.")

    return {
        "summary": " ".join(summary_parts) if summary_parts else "Championship context was unavailable after this race.",
        "drivers": driver_now[:5],
        "constructors": constructor_now[:5],
    }


async def _load_round_dataset(season: int, round_num: int) -> dict[str, Any]:
    result_data, qualifying, constructor_now, driver_now = await asyncio.gather(
        ergast.get_race_results(season, round_num),
        ergast.get_qualifying_results(season, round_num),
        ergast.get_constructor_standings(season, round_num),
        ergast.get_driver_standings(season, round_num),
    )

    previous_constructor = []
    previous_driver = []
    if round_num > 1:
        previous_constructor, previous_driver = await asyncio.gather(
            ergast.get_constructor_standings(season, round_num - 1),
            ergast.get_driver_standings(season, round_num - 1),
        )

    session = await fastf1_service.load_session(season, round_num, "R")
    session_results = _parse_session_results(session) if session else []
    stint_rows = _parse_stints(session) if session else []
    pace_lookup = _pace_by_driver(session) if session else {}
    weather = _weather_summary(session) if session else {"summary": "No race weather sample was available."}

    merged_results = session_results or []
    if not merged_results and result_data:
        merged_results = [
            {
                "driver": item.get("abbreviation") or _driver_code(item.get("full_name", "")),
                "full_name": item.get("full_name", ""),
                "team": item.get("team", ""),
                "position": item.get("position", 99),
                "grid_position": item.get("grid", 0),
                "points": item.get("points", 0.0),
                "status": item.get("status", ""),
                "fastest_lap": item.get("fastest_lap_time"),
                "laps_completed": item.get("laps", 0),
                "pit_stops": 0,
            }
            for item in result_data
        ]

    if session_results and result_data:
        ergast_lookup = {_norm(item.get("full_name", "")): item for item in result_data}
        for row in merged_results:
            source = ergast_lookup.get(_norm(row.get("full_name", "")))
            if source:
                row["status"] = source.get("status") or row.get("status")

    return {
        "result_data": result_data,
        "qualifying": qualifying,
        "constructor_now": constructor_now,
        "driver_now": driver_now,
        "previous_constructor": previous_constructor,
        "previous_driver": previous_driver,
        "session_results": session_results,
        "stint_rows": stint_rows,
        "pace_lookup": pace_lookup,
        "weather": weather,
        "merged_results": merged_results,
    }


async def _build_race_debrief(year: Optional[int], round_num: Optional[int]) -> dict:
    season, resolved_round, race, _ = await _resolve_race(year, round_num)
    if resolved_round is None or race is None:
        return {
            "available": False,
            "error": "No completed race could be resolved from the live season schedule.",
        }

    dataset = await _load_round_dataset(season, resolved_round)
    merged_results = dataset["merged_results"]
    if not merged_results:
        return {
            "available": False,
            "season": season,
            "round": resolved_round,
            "error": "Race-result data was unavailable for the selected round.",
        }

    winner = merged_results[0]
    podium = merged_results[:3]
    retirees = [item for item in merged_results if _is_retirement(item.get("status", ""))]
    movers = [
        {
            "driver": item.get("driver") or _driver_code(item.get("full_name", "")),
            "full_name": item.get("full_name"),
            "team": item.get("team"),
            "delta": _grid_delta(item),
            "grid_position": item.get("grid_position"),
            "finish_position": item.get("position"),
        }
        for item in merged_results
        if _grid_delta(item) is not None
    ]
    biggest_gainers = sorted([item for item in movers if item["delta"] > 0], key=lambda item: item["delta"], reverse=True)[:3]
    biggest_losers = sorted([item for item in movers if item["delta"] < 0], key=lambda item: item["delta"])[:3]
    strategy = _race_strategy_overview(dataset["stint_rows"])
    constructors = _decorate_constructors(dataset["constructor_now"], dataset["result_data"])
    championship = _race_championship_impact(
        dataset["driver_now"],
        dataset["previous_driver"],
        dataset["constructor_now"],
        dataset["previous_constructor"],
    )

    podium_line = ", ".join(
        f"P{entry.get('position')}: {entry.get('full_name')} ({entry.get('team')})"
        for entry in podium
    )
    executive_summary = (
        f"{winner.get('full_name')} won the {race.get('race_name')} for {winner.get('team')}. "
        f"The podium read {podium_line}. "
        f"{len(retirees)} retirement{'s' if len(retirees) != 1 else ''} shaped the final classification."
    )
    storylines = [
        f"{winner.get('full_name')} converted P{winner.get('grid_position') or '?'} into victory and banked {winner.get('points', 0):.0f} points.",
        strategy.get("summary"),
        dataset["weather"].get("summary", "No weather sample was available."),
    ]
    if biggest_gainers:
        storylines.append(
            f"Best charge: {biggest_gainers[0]['full_name']} gained {biggest_gainers[0]['delta']} places from the grid."
        )
    if retirees:
        storylines.append(
            "Retirements: " + ", ".join(
                f"{item.get('driver') or _driver_code(item.get('full_name', ''))} ({item.get('status')})"
                for item in retirees[:4]
            ) + "."
        )

    return _json_safe({
        "available": True,
        "season": season,
        "round": resolved_round,
        "race": {
            "name": race.get("race_name"),
            "date": race.get("date"),
            "circuit": race.get("circuit"),
            "locality": race.get("locality"),
            "country": race.get("country"),
        },
        "headline": executive_summary,
        "executive_summary": executive_summary,
        "podium": podium,
        "strategy_overview": strategy,
        "weather": dataset["weather"],
        "storylines": storylines,
        "retirements": [
            {
                "driver": item.get("driver") or _driver_code(item.get("full_name", "")),
                "full_name": item.get("full_name"),
                "team": item.get("team"),
                "status": item.get("status"),
            }
            for item in retirees
        ],
        "biggest_gainers": biggest_gainers,
        "biggest_losers": biggest_losers,
        "constructor_points": sorted(
            constructors,
            key=lambda item: (-item.get("race_points", 0.0), item.get("position", 99)),
        ),
        "championship_impact": championship,
    })


async def _build_team_debrief(team_name: str, year: Optional[int], round_num: Optional[int]) -> dict:
    season, resolved_round, race, _ = await _resolve_race(year, round_num)
    if resolved_round is None or race is None:
        return {
            "available": False,
            "team": team_name,
            "error": "No completed race could be resolved from the live season schedule.",
        }

    dataset = await _load_round_dataset(season, resolved_round)
    result_data = dataset["result_data"]
    qualifying = dataset["qualifying"]
    constructor_now = dataset["constructor_now"]
    driver_now = dataset["driver_now"]
    previous_constructor = dataset["previous_constructor"]
    previous_driver = dataset["previous_driver"]
    stint_rows = dataset["stint_rows"]
    pace_lookup = dataset["pace_lookup"]
    weather = dataset["weather"]
    merged_results = dataset["merged_results"]

    team_results = [item for item in merged_results if _team_match(item.get("team", ""), team_name)]
    team_qualifying = [item for item in qualifying if _team_match(item.get("team", ""), team_name)]
    team_stints = [item for item in stint_rows if _team_match(item.get("team", ""), team_name)]
    team_drivers_now = [item for item in driver_now if _team_match(item.get("team", ""), team_name)]
    team_drivers_prev = [item for item in previous_driver if _team_match(item.get("team", ""), team_name)]

    constructor_entry = _find_team_entry(constructor_now, team_name, key="name")
    constructor_prev = _find_team_entry(previous_constructor, team_name, key="name")
    if constructor_entry and constructor_now:
        constructor_entry = dict(constructor_entry)
        constructor_entry["leader_points"] = max(item.get("points", 0.0) for item in constructor_now)
    if constructor_prev and previous_constructor:
        constructor_prev = dict(constructor_prev)
        constructor_prev["leader_points"] = max(item.get("points", 0.0) for item in previous_constructor)

    driver_reviews = _build_driver_reviews(team_name, team_results, team_qualifying, team_drivers_now, pace_lookup)
    strategy = _strategy_shape(team_stints)
    executive_summary, team_summary = _team_summary(team_name, team_results, constructor_entry, race.get("race_name", "the race"))
    race_points = sum(item.get("points", 0.0) for item in team_results)
    championship = _championship_impact(team_name, constructor_entry, constructor_prev, team_drivers_now, team_drivers_prev, race_points)
    incidents = _build_incidents(team_results, weather)
    action_items = _build_action_items(team_results, strategy, championship)
    overperformers, underperformers = _build_over_under(driver_reviews)
    biggest_win, biggest_loss = _build_biggest_calls(driver_reviews)

    suggestions = []
    if len(driver_reviews) >= 2:
        suggestions.append(f"Why did {driver_reviews[0]['driver']} finish ahead of {driver_reviews[1]['driver']}?")
    suggestions.extend([
        f"Was {team_name} strategy aggressive or conservative?",
        f"What changed in the championship after {race.get('race_name', 'this race')}?",
        f"What should {team_name} fix before the next round?",
    ])

    return _json_safe({
        "available": True,
        "team": team_name,
        "season": season,
        "round": resolved_round,
        "race": {
            "name": race.get("race_name"),
            "date": race.get("date"),
            "circuit": race.get("circuit"),
            "locality": race.get("locality"),
            "country": race.get("country"),
        },
        "constructor": constructor_entry,
        "executive_summary": executive_summary,
        "team_debrief_summary": team_summary,
        "strategy_audit": strategy,
        "driver_reviews": driver_reviews,
        "biggest_win": biggest_win,
        "biggest_loss": biggest_loss,
        "incident_explanations": incidents,
        "championship_impact": championship,
        "action_items": action_items,
        "overperformers": overperformers,
        "underperformers": underperformers,
        "weather": weather,
        "ask_suggestions": suggestions[:4],
    })


def _answer_question(report: dict, question: str) -> str:
    q = (question or "").lower()
    if not report.get("available"):
        return report.get("error", "Debrief data is unavailable.")

    if any(word in q for word in ("strategy", "pit", "undercut", "overcut", "stint", "tyre")):
        audit = report.get("strategy_audit", {})
        notes = audit.get("notes") or []
        suffix = f" Key execution detail: {notes[0]}." if notes else ""
        return f"{audit.get('summary', 'Strategy data was limited.')}{suffix}"

    if any(word in q for word in ("championship", "title", "points", "standings")):
        return report.get("championship_impact", {}).get("summary", "Championship data was unavailable.")

    if any(word in q for word in ("fix", "improve", "next", "weakness")):
        items = report.get("action_items") or []
        if not items:
            return "No clear action items were derived from the available race data."
        return "Next-race priorities: " + " ".join(f"{idx + 1}. {item}" for idx, item in enumerate(items[:3]))

    reviews = report.get("driver_reviews") or []
    mentioned = [review for review in reviews if review.get("driver", "").lower() in q or review.get("full_name", "").lower() in q]
    if "why" in q and mentioned:
        review = mentioned[0]
        return (
            f"{review['full_name']} finished {_fmt_pos(review.get('finish_position'))} from {_fmt_pos(review.get('grid_position'))}. "
            f"{review.get('note')} Their median race pace was {_fmt_seconds(review.get('average_lap_s'))}."
        )

    if ("compare" in q or "better" in q) and len(reviews) >= 2:
        first, second = reviews[0], reviews[1]
        return (
            f"{first['full_name']} finished ahead on the road at {_fmt_pos(first.get('finish_position'))} versus {_fmt_pos(second.get('finish_position'))}. "
            f"Grid-to-flag swing was {_fmt_delta(first.get('delta'))} for {first['driver']} and {_fmt_delta(second.get('delta'))} for {second['driver']}. "
            f"Median pace was {_fmt_seconds(first.get('average_lap_s'))} against {_fmt_seconds(second.get('average_lap_s'))}."
        )

    return (
        f"{report.get('executive_summary', '')} "
        f"{report.get('biggest_win', {}).get('detail', '')} "
        f"{report.get('biggest_loss', {}).get('detail', '')}"
    ).strip()


@router.get("/summary")
async def debrief_summary(
    year: Optional[int] = Query(None),
    round_num: Optional[int] = Query(None),
):
    season, resolved_round, race, completed_races = await _resolve_race(year, round_num)
    if resolved_round is None or race is None:
        return {
            "available": False,
            "season": season,
            "completed_races": completed_races,
            "constructors": [],
            "error": "No completed race was available for debrief analysis.",
        }

    constructors, results = await asyncio.gather(
        ergast.get_constructor_standings(season, resolved_round),
        ergast.get_race_results(season, resolved_round),
    )
    constructors = _decorate_constructors(constructors, results)

    return _json_safe({
        "available": True,
        "season": season,
        "round": resolved_round,
        "race": {
            "name": race.get("race_name"),
            "date": race.get("date"),
            "circuit": race.get("circuit"),
            "locality": race.get("locality"),
            "country": race.get("country"),
        },
        "completed_races": [
            {
                "round": item.get("round"),
                "race_name": item.get("race_name"),
                "date": item.get("date"),
                "country": item.get("country"),
            }
            for item in completed_races
        ],
        "constructors": constructors,
    })


@router.get("/race")
async def debrief_race(
    year: Optional[int] = Query(None),
    round_num: Optional[int] = Query(None),
):
    return _json_safe(await _build_race_debrief(year, round_num))


@router.get("/team")
async def debrief_team(
    team_name: str = Query(...),
    year: Optional[int] = Query(None),
    round_num: Optional[int] = Query(None),
):
    return _json_safe(await _build_team_debrief(team_name, year, round_num))


@router.post("/ask")
async def debrief_ask(
    payload: dict = Body(...),
    year: Optional[int] = Query(None),
):
    team_name = payload.get("team_name")
    question = (payload.get("question") or "").strip()
    round_num = payload.get("round_num")
    if not team_name or not question:
        return {"answer": "A team name and question are required."}

    report = await _build_team_debrief(team_name, year, round_num)
    answer = _answer_question(report, question)
    if gemini_available() and report.get("available"):
        try:
            answer = await answer_debrief_question_with_gemini(question, report)
        except Exception as exc:
            logger.warning("Gemini debrief answer failed, falling back to rule-based analyst: %s", exc)
    return _json_safe({
        "answer": answer,
        "model": "Gemini 2.5 Flash" if gemini_available() and report.get("available") else "F1IQ Debrief Analyst",
    })
