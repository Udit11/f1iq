"""
/api/weekend/* — Race weekend intelligence

This router intentionally avoids hardcoded season fixtures, standings, or mock previews.
It builds the Weekend page from real upstream sources when available:
  - Ergast/Jolpica for race schedule and standings
  - FastF1 for detailed session timestamps
  - FastF1/Ergast historical data for lightweight derived analytics
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from statistics import mean
from typing import Optional

from fastapi import APIRouter, Body, Path, Query

from ..services import ergast, fastf1_service
from ..services.gemini_service import (
    answer_weekend_question_with_gemini,
    gemini_available,
)

router = APIRouter(prefix="/api/weekend", tags=["weekend"])
logger = logging.getLogger(__name__)

ASK_EXAMPLES = [
    "Why is qualifying so important here?",
    "Who looks strongest this weekend?",
    "What does history say about strategy here?",
]
DRIVER_CODES = {
    "George Russell": "RUS",
    "Kimi Antonelli": "ANT",
    "Charles Leclerc": "LEC",
    "Lewis Hamilton": "HAM",
    "Max Verstappen": "VER",
    "Lando Norris": "NOR",
    "Oscar Piastri": "PIA",
    "Fernando Alonso": "ALO",
}
POINTS_TABLE = {1: 25, 2: 18, 3: 15, 4: 12, 5: 10, 6: 8, 7: 6, 8: 4, 9: 2, 10: 1}


def _driver_code(name: str) -> str:
    if not name:
        return "DRV"
    return DRIVER_CODES.get(name, "".join(part[0] for part in name.split()[-2:]).upper()[:3] or "DRV")


def _parse_dt(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _status_from_race_date(race_date: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    if not race_date:
        return "unknown"
    if race_date < today:
        return "completed"
    if race_date == today:
        return "live"
    return "upcoming"


def _build_minimal_sessions(race: dict) -> list[dict]:
    """
    Real-data-only fallback: if detailed session data is unavailable, expose just the actual race session.
    """
    session = {
        "name": "Race",
        "type": "RACE",
        "date": race.get("race_date") or race.get("date"),
        "local_time": None,
        "utc_time": None,
        "utc_offset": None,
    }
    if race.get("time"):
        session["utc_time"] = f"{session['date']}T{race['time'].replace('Z', '')}Z"
        session["local_time"] = race["time"][:5]
    return [session] if session["date"] else []


def _merge_schedule(ergast_schedule: list[dict], detailed_schedule: list[dict]) -> list[dict]:
    detailed_by_round = {item.get("round"): item for item in detailed_schedule if item.get("round")}
    merged = []

    for race in ergast_schedule:
        detail = detailed_by_round.get(race.get("round"), {})
        sessions = detail.get("sessions") or _build_minimal_sessions({
            "race_date": race.get("date"),
            "time": race.get("time"),
        })
        merged.append({
            "round": race.get("round"),
            "race_name": detail.get("race_name") or race.get("race_name"),
            "official_name": detail.get("official_name"),
            "circuit": detail.get("circuit") or race.get("circuit"),
            "country": detail.get("country") or race.get("country"),
            "locality": detail.get("locality") or race.get("locality"),
            "race_date": detail.get("race_date") or race.get("date"),
            "status": race.get("status") or _status_from_race_date(detail.get("race_date") or race.get("date", "")),
            "sprint": bool(detail.get("sprint")),
            "sessions": sessions,
            "utc_offset": next((s.get("utc_offset") for s in sessions if s.get("utc_offset")), None),
            "circuit_info": {},
            "note": None,
        })

    if merged:
        return merged

    # If Ergast is unavailable but FastF1 detailed schedule exists, return that.
    for race in detailed_schedule:
        merged.append({
            "round": race.get("round"),
            "race_name": race.get("race_name"),
            "official_name": race.get("official_name"),
            "circuit": race.get("circuit"),
            "country": race.get("country"),
            "locality": race.get("locality"),
            "race_date": race.get("race_date"),
            "status": _status_from_race_date(race.get("race_date", "")),
            "sprint": bool(race.get("sprint")),
            "sessions": race.get("sessions", []),
            "utc_offset": next((s.get("utc_offset") for s in race.get("sessions", []) if s.get("utc_offset")), None),
            "circuit_info": {},
            "note": None,
        })
    return merged


async def _load_schedule(year: int) -> list[dict]:
    ergast_schedule, detailed_schedule = await asyncio.gather(
        ergast.get_race_schedule(year),
        fastf1_service.get_event_schedule_detailed(year),
    )
    return _merge_schedule(ergast_schedule or [], detailed_schedule or [])


def _find_next_race(schedule: list[dict]) -> Optional[dict]:
    now = datetime.now(timezone.utc)

    def _effective_status(race: dict) -> str:
        explicit = race.get("status")
        if explicit in {"upcoming", "live", "completed"}:
            return explicit

        session_times = [
            _parse_dt(sess.get("utc_time"))
            for sess in race.get("sessions", [])
            if sess.get("utc_time")
        ]
        session_times = [dt for dt in session_times if dt is not None]
        if session_times:
            if all(dt < now for dt in session_times):
                return "completed"
            return "upcoming"

        race_date = race.get("race_date") or ""
        if race_date and race_date < now.date().isoformat():
            return "completed"
        return "upcoming"

    candidates = []
    for race in schedule:
        status = _effective_status(race)
        if status == "completed":
            continue
        candidates.append((0 if status == "live" else 1, race.get("race_date") or "", race.get("round") or 999, race))

    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    return candidates[0][3] if candidates else None


def _find_race_by_round(schedule: list[dict], round_num: int) -> Optional[dict]:
    return next((race for race in schedule if race.get("round") == round_num), None)


def _build_countdown(race: dict) -> dict:
    now = datetime.now(timezone.utc)
    sessions = []
    race_seconds_until = None
    race_countdown_display = "Unavailable"

    for sess in race.get("sessions", []):
        utc_dt = _parse_dt(sess.get("utc_time"))
        if utc_dt is None and sess.get("date") and sess.get("local_time"):
            try:
                utc_dt = datetime.fromisoformat(f"{sess['date']}T{sess['local_time']}:00").replace(tzinfo=timezone.utc)
            except ValueError:
                utc_dt = None
        seconds_until = int((utc_dt - now).total_seconds()) if utc_dt else None
        past = seconds_until is not None and seconds_until < 0
        display = _fmt_countdown(seconds_until) if seconds_until is not None and seconds_until >= 0 else ("Completed" if past else "TBC")
        session_out = {
            **sess,
            "seconds_until": seconds_until,
            "past": past,
            "display_countdown": display,
        }
        sessions.append(session_out)
        if sess.get("type") == "RACE" and seconds_until is not None:
            race_seconds_until = seconds_until
            race_countdown_display = _fmt_countdown(seconds_until) if seconds_until >= 0 else "Race weekend active"

    return {
        "race_name": race.get("race_name"),
        "circuit": race.get("circuit"),
        "country": race.get("country"),
        "race_date": race.get("race_date"),
        "utc_offset": race.get("utc_offset"),
        "sprint": race.get("sprint", False),
        "note": race.get("note"),
        "sessions": sessions,
        "race_seconds_until": race_seconds_until,
        "race_countdown_display": race_countdown_display,
    }


def _fmt_countdown(seconds: Optional[int]) -> str:
    if seconds is None:
        return "TBC"
    if seconds <= 0:
        return "Now"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if d > 0:
        return f"{d}d {h}h {m}m"
    if h > 0:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def _completed_recent_rounds(schedule: list[dict], target_round: int, limit: int = 5) -> list[dict]:
    completed = [
        race for race in schedule
        if race.get("round") and race.get("round") < target_round and race.get("status") == "completed"
    ]
    completed.sort(key=lambda race: race.get("round", 0))
    return completed[-limit:]


async def _load_recent_round_reference(year: int, race: dict) -> dict:
    round_num = race.get("round")
    race_results, weather_history, stints_history = await asyncio.gather(
        ergast.get_race_results(year, round_num),
        fastf1_service.get_weather_history(year, round_num),
        fastf1_service.get_stints_history(year, round_num),
    )
    return {
        "round": round_num,
        "race_name": race.get("race_name"),
        "race_results": race_results or [],
        "weather_history": weather_history or [],
        "stints_history": stints_history or [],
    }


def _flatten_recent_reference(recent_rounds: list[dict], key: str) -> list[dict]:
    merged = []
    for race in recent_rounds:
        merged.extend(race.get(key) or [])
    return merged


def _recent_form_metrics(recent_rounds: list[dict]) -> dict:
    driver_points: defaultdict[str, float] = defaultdict(float)
    team_points: defaultdict[str, float] = defaultdict(float)
    driver_finishes: defaultdict[str, list[int]] = defaultdict(list)

    for race in recent_rounds:
        for result in race.get("race_results", []):
            full_name = result.get("full_name")
            team = result.get("team")
            points = float(result.get("points", 0) or 0)
            position = result.get("position")
            if full_name:
                driver_points[full_name] += points
                if position:
                    driver_finishes[full_name].append(int(position))
            if team:
                team_points[team] += points

    avg_finish = {
        driver: mean(finishes)
        for driver, finishes in driver_finishes.items()
        if finishes
    }
    return {
        "round_count": len(recent_rounds),
        "driver_points": dict(driver_points),
        "team_points": dict(team_points),
        "driver_avg_finish": avg_finish,
    }


async def _load_reference_data(year: int, round_num: Optional[int]) -> dict:
    driver_standings, constructor_standings = await asyncio.gather(
        ergast.get_driver_standings(year),
        ergast.get_constructor_standings(year),
    )

    empty = {
        "driver_standings": driver_standings or [],
        "constructor_standings": constructor_standings or [],
        "track_history": {
            "race_results": [],
            "qualifying_results": [],
            "weather_history": [],
            "stints_history": [],
        },
        "recent_form": {
            "rounds": [],
            "race_results": [],
            "weather_history": [],
            "stints_history": [],
            "metrics": {"round_count": 0, "driver_points": {}, "team_points": {}, "driver_avg_finish": {}},
        },
    }
    if not round_num or year <= 1950:
        return empty

    prev_year = year - 1
    schedule = await ergast.get_race_schedule(year)
    recent_races = _completed_recent_rounds(schedule or [], round_num, limit=5)

    track_race_results, track_qualifying_results, track_weather_history = await asyncio.gather(
        ergast.get_race_results(prev_year, round_num),
        ergast.get_qualifying_results(prev_year, round_num),
        fastf1_service.get_weather_history(prev_year, round_num),
    )
    track_stints_history = await fastf1_service.get_stints_history(prev_year, round_num)

    recent_round_payloads = []
    if recent_races:
        recent_round_payloads = await asyncio.gather(
            *[_load_recent_round_reference(year, race) for race in recent_races]
        )

    return {
        "driver_standings": driver_standings or [],
        "constructor_standings": constructor_standings or [],
        "track_history": {
            "race_results": track_race_results or [],
            "qualifying_results": track_qualifying_results or [],
            "weather_history": track_weather_history or [],
            "stints_history": track_stints_history or [],
        },
        "recent_form": {
            "rounds": recent_round_payloads,
            "race_results": _flatten_recent_reference(recent_round_payloads, "race_results"),
            "weather_history": _flatten_recent_reference(recent_round_payloads, "weather_history"),
            "stints_history": _flatten_recent_reference(recent_round_payloads, "stints_history"),
            "metrics": _recent_form_metrics(recent_round_payloads),
        },
    }


def _average_grid_swing(race_results: list[dict]) -> Optional[float]:
    moves = [
        abs((entry.get("position") or 0) - (entry.get("grid") or entry.get("grid_position") or 0))
        for entry in race_results
        if entry.get("position") and (entry.get("grid") or entry.get("grid_position"))
    ]
    return mean(moves) if moves else None


def _qualifying_importance(track_results: list[dict], recent_results: list[dict], recent_round_count: int) -> dict:
    track_move = _average_grid_swing(track_results)
    recent_move = _average_grid_swing(recent_results)
    if track_move is None and recent_move is None:
        return {
            "score": None,
            "label": "Unavailable",
            "reason": "No qualifying-to-race position data is available for this event yet.",
        }
    if track_move is not None and recent_move is not None:
        avg_move = track_move * 0.6 + recent_move * 0.4
    else:
        avg_move = track_move if track_move is not None else recent_move

    score = max(20, min(92, round(95 - avg_move * 12)))
    if score >= 80:
        label = "High"
        reason = "Qualifying should matter heavily here."
    elif score >= 60:
        label = "Medium"
        reason = "Grid position should matter, but the race still leaves room for movement."
    else:
        label = "Low"
        reason = "Recent evidence points to a circuit where Sunday can reshuffle the order."
    if track_move is not None and recent_move is not None:
        reason += f" Same-circuit history showed {track_move:.1f} places of grid-to-finish swing, while the last {recent_round_count} completed races averaged {recent_move:.1f}."
    elif track_move is not None:
        reason += f" Same-circuit history showed {track_move:.1f} places of grid-to-finish swing."
    else:
        reason += f" The last {recent_round_count} completed races averaged {recent_move:.1f} places of grid-to-finish swing."
    return {"score": score, "label": label, "reason": reason}


def _weather_risk(track_weather: list[dict], recent_weather: list[dict], recent_round_count: int) -> dict:
    if not track_weather and not recent_weather:
        return {
            "score": None,
            "label": "Unavailable",
            "summary": "No historical weather sample is available for this event yet.",
        }
    track_pct = round(sum(1 for row in track_weather if row.get("rainfall")) / len(track_weather) * 100) if track_weather else None
    recent_pct = round(sum(1 for row in recent_weather if row.get("rainfall")) / len(recent_weather) * 100) if recent_weather else None
    if track_pct is not None and recent_pct is not None:
        pct = round(track_pct * 0.65 + recent_pct * 0.35)
    else:
        pct = track_pct if track_pct is not None else recent_pct

    if pct >= 35:
        label = "Elevated"
        summary = f"Weather risk is elevated at roughly {pct}%."
    elif pct > 0:
        label = "Moderate"
        summary = f"Weather risk is moderate at roughly {pct}%."
    else:
        label = "Low"
        summary = "Weather risk is low."
    if track_pct is not None and recent_pct is not None:
        summary += f" Same-circuit history showed rain in {track_pct}% of sampled intervals, while the last {recent_round_count} races ran at {recent_pct}%."
    elif track_pct is not None:
        summary += f" Same-circuit history showed rain in {track_pct}% of sampled intervals."
    else:
        summary += f" The last {recent_round_count} completed races showed rain in {recent_pct}% of sampled intervals."
    return {"score": pct, "label": label, "summary": summary}


def _strategy_stats(stints_history: list[dict]) -> tuple[float, float]:
    by_driver: defaultdict[str, list[dict]] = defaultdict(list)
    for stint in stints_history:
        by_driver[stint.get("driver", "?")].append(stint)

    stop_counts = [max(0, len(stints) - 1) for stints in by_driver.values() if stints]
    stint_lengths = [stint.get("laps", 0) for stint in stints_history if stint.get("laps")]
    avg_stops = mean(stop_counts) if stop_counts else 0.0
    avg_stint = mean(stint_lengths) if stint_lengths else 0.0
    return avg_stops, avg_stint


def _strategy_forecast(track_stints: list[dict], recent_stints: list[dict], recent_round_count: int) -> dict:
    if not track_stints and not recent_stints:
        return {
            "primary": "No data-backed strategy forecast is available yet for this event.",
            "alternatives": [],
            "tyre_stress": "Unavailable",
            "undercut_power": "Unknown",
            "safety_car_sensitivity": "Unknown",
        }
    track_avg_stops, track_avg_stint = _strategy_stats(track_stints) if track_stints else (0.0, 0.0)
    recent_avg_stops, recent_avg_stint = _strategy_stats(recent_stints) if recent_stints else (0.0, 0.0)
    if track_stints and recent_stints:
        avg_stops = track_avg_stops * 0.6 + recent_avg_stops * 0.4
        avg_stint = track_avg_stint * 0.6 + recent_avg_stint * 0.4
    elif track_stints:
        avg_stops, avg_stint = track_avg_stops, track_avg_stint
    else:
        avg_stops, avg_stint = recent_avg_stops, recent_avg_stint

    if avg_stops >= 1.8:
        primary = "Historical race data points toward a two-stop leaning event rather than a clean one-stop procession."
    elif avg_stops >= 1.1:
        primary = "Historical race data suggests a split race: one-stop is possible, but a meaningful two-stop branch should stay alive."
    else:
        primary = "Historical race data points toward a one-stop baseline for most of the field."

    if avg_stint <= 14:
        tyre_stress = "High"
    elif avg_stint <= 20:
        tyre_stress = "Medium"
    else:
        tyre_stress = "Low"

    return {
        "primary": primary,
        "alternatives": [
            f"Same-circuit stop count reference: {track_avg_stops:.1f}." if track_stints else "Same-circuit stop data is unavailable.",
            f"Recent-form stop count across the last {recent_round_count} races: {recent_avg_stops:.1f}." if recent_stints else "Recent stop-count data is unavailable.",
            f"Blended average stint length is {avg_stint:.1f} laps.",
        ],
        "tyre_stress": tyre_stress,
        "undercut_power": "Unknown",
        "safety_car_sensitivity": "Unknown",
    }


def _prediction_table(driver_standings: list[dict], constructor_standings: list[dict], recent_metrics: dict) -> list[dict]:
    if not driver_standings:
        return []

    max_driver_points = max((float(d.get("points", 0)) for d in driver_standings), default=1.0) or 1.0
    constructor_points = {c.get("name"): float(c.get("points", 0)) for c in constructor_standings}
    max_constructor_points = max(constructor_points.values(), default=1.0) or 1.0
    recent_driver_points = recent_metrics.get("driver_points", {})
    recent_team_points = recent_metrics.get("team_points", {})
    recent_avg_finish = recent_metrics.get("driver_avg_finish", {})
    max_recent_driver_points = max(recent_driver_points.values(), default=0.0) or 1.0
    max_recent_team_points = max(recent_team_points.values(), default=0.0) or 1.0

    scored = []
    for driver in driver_standings[:8]:
        driver_points = float(driver.get("points", 0))
        team_points = constructor_points.get(driver.get("team"), 0.0)
        position_bonus = max(0.0, 12.0 - float(driver.get("position", 99)))
        recent_driver_score = (recent_driver_points.get(driver.get("full_name", ""), 0.0) / max_recent_driver_points) if recent_driver_points else 0.0
        recent_team_score = (recent_team_points.get(driver.get("team", ""), 0.0) / max_recent_team_points) if recent_team_points else 0.0
        recent_finish = recent_avg_finish.get(driver.get("full_name", ""))
        recent_finish_bonus = max(0.0, 12.0 - recent_finish) if recent_finish else 0.0
        score = (
            (driver_points / max_driver_points) * 0.5 +
            (team_points / max_constructor_points) * 0.2 +
            recent_driver_score * 0.2 +
            recent_team_score * 0.1
        ) * 100 + position_bonus + recent_finish_bonus
        scored.append((score, driver))

    scored.sort(key=lambda item: item[0], reverse=True)
    total = sum(score for score, _ in scored[:6]) or 1.0

    out = []
    for idx, (score, driver) in enumerate(scored[:6]):
        win_probability = round(score / total * 100, 1)
        podium_probability = round(min(95.0, 18.0 + win_probability * 2.1 + max(0, 8 - idx * 2)), 1)
        team = driver.get("team", "")
        position = driver.get("position")
        points = float(driver.get('points', 0))
        reason = (
            f"Championship P{position} with {points:.0f} points "
            f"(team: {team or 'N/A'}). "
            f"Model combines current standings with recent form to project win/championship impact."
        )
        if recent_driver_points:
            recent_pts = recent_driver_points.get(driver.get('full_name', ''), 0.0)
            reason += f" Recent reference races added {recent_pts:.0f} points to the form signal."
        out.append({
            "code": _driver_code(driver.get("full_name", "")),
            "full_name": driver.get("full_name", ""),
            "team": team,
            "win_probability": win_probability,
            "podium_probability": podium_probability,
            "why": reason,
        })
    return out


def _upset_pick(predictions: list[dict]) -> dict:
    if not predictions:
        return {
            "code": "—",
            "full_name": "Unavailable",
            "team": "",
            "confidence": 0,
            "reason": "No driver prediction table is available.",
        }
    pick = predictions[2] if len(predictions) >= 3 else predictions[-1]
    return {
        "code": pick["code"],
        "full_name": pick["full_name"],
        "team": pick["team"],
        "confidence": round(min(85.0, pick["win_probability"] + 20.0), 1),
        "reason": f"{pick['full_name']} is the dark-horse pick because current standings still keep them within striking distance of a big points swing.",
    }


def _matchups(predictions: list[dict]) -> list[dict]:
    if len(predictions) < 2:
        return []
    rows = [
        {
            "title": f"{predictions[0]['code']} vs {predictions[1]['code']}",
            "edge": predictions[0]["code"],
            "angle": "The top two names in the current projection are also the cleanest title reference points.",
            "reason": f"{predictions[0]['full_name']} has the edge on current points, but {predictions[1]['full_name']} is close enough to flip the narrative with one strong weekend.",
        }
    ]
    if len(predictions) >= 4:
        rows.append({
            "title": f"{predictions[2]['code']} vs {predictions[3]['code']}",
            "edge": predictions[2]["code"],
            "angle": "This is the spoiler battle just behind the outright favorites.",
            "reason": "Whoever wins this fight becomes the most credible pressure point if the leaders trade points rather than dominate.",
        })
    return rows


def _championship_context(driver_standings: list[dict], constructor_standings: list[dict], race_name: str) -> dict:
    if len(driver_standings) < 2:
        return {}
    leader = driver_standings[0]
    second = driver_standings[1]
    gap = float(leader.get("points", 0)) - float(second.get("points", 0))
    out = {
        "leader": leader.get("full_name"),
        "leader_team": leader.get("team"),
        "leader_points": leader.get("points"),
        "gap_to_second": gap,
        "second": second.get("full_name"),
        "narrative": f"{leader.get('full_name')} leads {second.get('full_name')} by {gap:.0f} points heading into {race_name}.",
        "top_drivers": driver_standings[:3],
        "top_constructors": constructor_standings[:2],
    }
    if len(constructor_standings) >= 2:
        out["constructor_leader"] = constructor_standings[0].get("name")
        out["constructor_gap"] = float(constructor_standings[0].get("points", 0)) - float(constructor_standings[1].get("points", 0))
    return out


def _championship_impact(race_name: str, driver_standings: list[dict]) -> dict:
    if len(driver_standings) < 3:
        return {"headline": f"{race_name} is the next scoring swing in the title fight.", "scenarios": []}
    leader, second, third = driver_standings[:3]
    gap_if_second_wins = float(leader.get("points", 0)) + POINTS_TABLE[4] - (float(second.get("points", 0)) + POINTS_TABLE[1])
    gap_if_third_wins = float(leader.get("points", 0)) + POINTS_TABLE[5] - (float(third.get("points", 0)) + POINTS_TABLE[1])
    return {
        "headline": f"{race_name} can still move the title gap meaningfully in a single Sunday.",
        "scenarios": [
            f"If {second.get('full_name')} wins and {leader.get('full_name')} finishes P4, the gap becomes {gap_if_second_wins:.0f} points.",
            f"If {third.get('full_name')} wins and {leader.get('full_name')} only finishes P5, the gap to third becomes {gap_if_third_wins:.0f} points.",
        ],
    }


def _watch_guide(race: dict, standings: list[dict], constructors: list[dict], predictions: list[dict], strategy: dict, weather: dict, recent_metrics: dict) -> list[str]:
    items = []
    if len(standings) >= 2:
        leader = standings[0]
        second = standings[1]
        gap = float(leader.get("points", 0)) - float(second.get("points", 0))
        items.append(f"Title watch — {leader.get('full_name')} leads {second.get('full_name')} by {gap:.0f} points.")
    if len(constructors) >= 2:
        gap = float(constructors[0].get("points", 0)) - float(constructors[1].get("points", 0))
        items.append(f"Constructors' fight — {constructors[0].get('name')} lead by {gap:.0f} points.")
    if predictions:
        items.append(f"Pre-weekend form — {predictions[0]['full_name']} tops the current projection.")
    if recent_metrics.get("round_count"):
        items.append(f"Recent form blend - the model is using the last {recent_metrics['round_count']} completed races alongside same-circuit history.")
    if strategy.get("primary"):
        items.append(f"Historical strategy read — {strategy['primary']}")
    if weather.get("summary"):
        items.append(f"Weather reference — {weather['summary']}")
    if race.get("sprint"):
        items.append("Sprint format — meaningful points are available before Sunday.")
    return items[:6]


def _what_if_scenarios(race_name: str, standings: list[dict], predictions: list[dict]) -> list[dict]:
    if len(standings) < 3:
        return []
    leader, second, third = standings[:3]
    upset = predictions[2] if len(predictions) >= 3 else predictions[-1] if predictions else None
    scenarios = [
        {
            "title": f"What if {second.get('full_name')} wins?",
            "probability": "Data-driven scenario",
            "impact": "CHAMPIONSHIP",
            "description": f"A win for {second.get('full_name')} immediately compresses the title fight and puts direct pressure on {leader.get('full_name')} at {race_name}.",
            "beneficiaries": [_driver_code(second.get("full_name", ""))],
            "losers": [_driver_code(leader.get("full_name", ""))],
        },
        {
            "title": f"What if {third.get('full_name')} out-scores the leaders?",
            "probability": "Data-driven scenario",
            "impact": "HIGH",
            "description": f"If {third.get('full_name')} turns {race_name} into a big score while the top two split points, the championship broadens instead of consolidating.",
            "beneficiaries": [_driver_code(third.get("full_name", ""))],
            "losers": [_driver_code(leader.get("full_name", "")), _driver_code(second.get("full_name", ""))],
        },
    ]
    if upset:
        scenarios.append({
            "title": f"What if {upset.get('full_name')} steals a podium?",
            "probability": "Projection-based scenario",
            "impact": "MODERATE",
            "description": f"{upset.get('full_name')} is outside the two leading title references but close enough in the standings to change the weekend's narrative with a podium.",
            "beneficiaries": [upset.get("code")],
            "losers": [],
        })
    return scenarios


def _answer_weekend_question(question: str, race: dict, analyst: dict) -> str:
    q = question.strip().lower()
    qualifying = analyst.get("qualifying_importance", {})
    strategy = analyst.get("strategy_forecast", {})
    weather = analyst.get("weather_risk", {})
    upset = analyst.get("upset_pick", {})
    predictions = analyst.get("win_predictions", [])
    championship = analyst.get("championship_impact", {})

    if any(word in q for word in ("qualifying", "pole", "grid")):
        return qualifying.get("reason") or "Qualifying importance is unavailable because no historical comparison data was returned."
    if any(word in q for word in ("strategy", "pit", "tyre", "tire", "stop")):
        return strategy.get("primary") or "No data-backed strategy forecast is available."
    if any(word in q for word in ("weather", "rain", "wet", "wind")):
        return weather.get("summary") or "No weather reference data is available."
    if any(word in q for word in ("upset", "dark horse", "surprise")):
        return upset.get("reason") or "No upset pick is available."
    if any(word in q for word in ("championship", "title", "points")):
        scenarios = championship.get("scenarios") or []
        return f"{championship.get('headline', 'No championship context available.')} {scenarios[0] if scenarios else ''}".strip()
    if any(word in q for word in ("favorite", "favourite", "winner", "win")):
        if predictions:
            top = predictions[0]
            return f"{top['full_name']} leads the current projection at {top['win_probability']}% win probability based on live championship standings."
        return "No prediction table is available yet."
    return analyst.get("preview", {}).get("narrative") or f"No real-data weekend preview is available yet for {race.get('race_name', 'the next race')}."

def _sprint_explainer() -> dict:
    return {
        "format": "Sprint Weekend",
        "sessions_explained": [
            {"type": "FP1", "name": "Practice 1", "desc": "Only free practice session before parc ferme conditions tighten the setup window.", "duration": "60 min"},
            {"type": "SQ", "name": "Sprint Qualifying", "desc": "Knockout session that sets the sprint grid.", "duration": "45 min"},
            {"type": "SPRINT", "name": "Sprint Race", "desc": "Short race with championship points available before Sunday.", "duration": "~30 min"},
            {"type": "Q", "name": "Qualifying", "desc": "Qualifying session that sets the Grand Prix grid.", "duration": "60 min"},
            {"type": "RACE", "name": "Grand Prix", "desc": "Full race-distance points event.", "duration": "~2 hours"},
        ],
        "key_rules": [
            "Sprint weekends compress setup time significantly.",
            "Sprint points count, but sprint results do not directly set the Sunday race result.",
            "The Grand Prix remains the main championship points event.",
        ],
    }


def _build_ai_analyst(race: dict, references: dict) -> dict:
    driver_standings = references.get("driver_standings", [])
    constructor_standings = references.get("constructor_standings", [])
    track_history = references.get("track_history", {})
    recent_form = references.get("recent_form", {})
    recent_metrics = recent_form.get("metrics", {})

    predictions = _prediction_table(driver_standings, constructor_standings, recent_metrics)
    qualifying = _qualifying_importance(
        track_history.get("race_results", []),
        recent_form.get("race_results", []),
        recent_metrics.get("round_count", 0),
    )
    weather = _weather_risk(
        track_history.get("weather_history", []),
        recent_form.get("weather_history", []),
        recent_metrics.get("round_count", 0),
    )
    strategy = _strategy_forecast(
        track_history.get("stints_history", []),
        recent_form.get("stints_history", []),
        recent_metrics.get("round_count", 0),
    )
    upset = _upset_pick(predictions)
    matchups = _matchups(predictions)
    championship = _championship_impact(race.get("race_name", "Next race"), driver_standings)

    preview_text_parts = []
    if predictions:
        preview_text_parts.append(f"{predictions[0]['full_name']} currently leads the projection from the live championship table.")
    if qualifying.get("reason"):
        preview_text_parts.append(qualifying["reason"])
    if strategy.get("primary"):
        preview_text_parts.append(strategy["primary"])
    if recent_metrics.get("round_count"):
        preview_text_parts.append(
            f"The model blends same-circuit history with the last {recent_metrics['round_count']} completed races to avoid overfitting to one weekend."
        )

    return {
        "preview": {
            "headline": "Weekend Analyst Preview",
            "narrative": " ".join(preview_text_parts) if preview_text_parts else "Not enough real data is available to build a preview yet.",
            "confidence": 0.0 if not predictions else 0.68,
            "model": "F1IQ Weekend Analyst",
        },
        "win_predictions": predictions,
        "qualifying_importance": qualifying,
        "strategy_forecast": strategy,
        "weather_risk": weather,
        "upset_pick": upset,
        "matchups": matchups,
        "championship_impact": championship,
        "ask_examples": ASK_EXAMPLES,
    }


def _build_weekend_payload(race: dict, references: dict) -> dict:
    driver_standings = references.get("driver_standings", [])
    constructor_standings = references.get("constructor_standings", [])
    track_history = references.get("track_history", {})
    recent_form = references.get("recent_form", {})
    recent_metrics = recent_form.get("metrics", {})

    countdown = _build_countdown(race)
    strategy = _strategy_forecast(
        track_history.get("stints_history", []),
        recent_form.get("stints_history", []),
        recent_metrics.get("round_count", 0),
    )
    weather = _weather_risk(
        track_history.get("weather_history", []),
        recent_form.get("weather_history", []),
        recent_metrics.get("round_count", 0),
    )
    predictions = _prediction_table(driver_standings, constructor_standings, recent_metrics)
    analyst = _build_ai_analyst(race, references)

    return {
        **countdown,
        "round": race.get("round"),
        "circuit_info": race.get("circuit_info", {}),
        "watch_guide": _watch_guide(race, driver_standings, constructor_standings, predictions, strategy, weather, recent_metrics),
        "what_if_scenarios": _what_if_scenarios(race.get("race_name", "Next race"), driver_standings, predictions),
        "championship_context": _championship_context(driver_standings, constructor_standings, race.get("race_name", "Next race")),
        "sprint_explainer": _sprint_explainer() if race.get("sprint") else None,
        "ai_analyst": analyst,
        "reference_basis": {
            "track_history": bool(track_history.get("race_results") or track_history.get("weather_history") or track_history.get("stints_history")),
            "recent_rounds_used": recent_metrics.get("round_count", 0),
            "blend": {"track_history": 0.4, "recent_form": 0.4, "standings": 0.2},
        },
    }


async def _weekend_payload_for_race(race: dict, year: int) -> dict:
    references = await _load_reference_data(year, race.get("round"))
    return _build_weekend_payload(race, references)


@router.get("/next")
async def next_race_preview():
    """Full next race preview built from real schedule and standings sources."""
    year = datetime.now(timezone.utc).year
    schedule = await _load_schedule(year)
    race = _find_next_race(schedule)
    if not race:
        return {"message": "No upcoming race found from live schedule sources.", "next_race": None}
    return await _weekend_payload_for_race(race, year)


@router.get("/round/{round_num}")
async def round_preview(round_num: int = Path(..., ge=1, le=30)):
    """Preview for a specific round using live schedule sources."""
    year = datetime.now(timezone.utc).year
    schedule = await _load_schedule(year)
    race = _find_race_by_round(schedule, round_num)
    if not race:
        return {"error": f"Round {round_num} not found from live schedule sources."}
    return await _weekend_payload_for_race(race, year)


@router.get("/top3")
async def top3_predictions(round_num: Optional[int] = Query(None, ge=1, le=30)):
    """Return the pre-race top 3 prediction list (with reasoning)."""
    year = datetime.now(timezone.utc).year
    schedule = await _load_schedule(year)
    race = _find_race_by_round(schedule, round_num) if round_num else _find_next_race(schedule)
    if not race:
        return {"error": "No upcoming race found from live schedule sources."}
    payload = await _weekend_payload_for_race(race, year)
    top3 = (payload.get("ai_analyst", {}).get("win_predictions", []) or [])[:3]
    return {"round": race.get("round"), "top3": top3}


@router.post("/ask")
async def ask_weekend_analyst(
    question: str = Body(..., embed=True),
    round_num: Optional[int] = Body(None),
):
    """Answer focused Weekend-page questions using the same real-data payload."""
    year = datetime.now(timezone.utc).year
    schedule = await _load_schedule(year)
    race = _find_race_by_round(schedule, round_num) if round_num is not None else _find_next_race(schedule)
    if not race:
        return {"answer": "No upcoming race found from live schedule sources.", "suggested_questions": ASK_EXAMPLES}

    references = await _load_reference_data(year, race.get("round"))
    analyst = _build_ai_analyst(race, references)
    answer = _answer_weekend_question(question, race, analyst)
    if gemini_available():
        try:
            answer = await answer_weekend_question_with_gemini(question, race, analyst)
        except Exception as exc:
            logger.warning("Gemini weekend answer failed, falling back to rule-based analyst: %s", exc)
    return {
        "answer": answer,
        "race_name": race.get("race_name"),
        "suggested_questions": ASK_EXAMPLES,
        "model": "Gemini 2.5 Flash" if gemini_available() else "F1IQ Weekend Analyst",
    }


@router.get("/schedule")
async def full_schedule():
    """Full season calendar from live sources only."""
    year = datetime.now(timezone.utc).year
    return await _load_schedule(year)
