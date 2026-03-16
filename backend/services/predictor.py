"""
F1IQ — Race Win Predictor (ML-powered)

Uses the F1MLPredictor ensemble from ml_engine:
  - XGBoost       → win probability
  - LightGBM      → podium probability
  - Logistic Reg  → points finish probability

Feature vector built from live OpenF1 data:
  position, gap, interval, tyre compound/age/health,
  laps remaining, race progress, pit stops, lap delta,
  safety car status, DRS availability, constructor strength
"""
import logging
from typing import Optional
from ..services import openf1, fastf1_service
from ..services.ml_engine import get_predictor, get_sc_model, CONSTRUCTOR_STRENGTH

logger = logging.getLogger(__name__)

# Warm up models at import time in background
import threading
_warmup_thread = threading.Thread(target=get_predictor, daemon=True)
_warmup_thread.start()


def _parse_lap_time(t) -> Optional[float]:
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return float(t)
    if isinstance(t, str) and ":" in t:
        try:
            parts = t.split(":")
            return float(parts[0]) * 60 + float(parts[1])
        except Exception:
            return None
    return None


async def _get_recent_form_factors(session_key: int = None) -> dict:
    """Compute a simple practice/qual/sprint form multiplier per driver.

    Uses FastF1 session data (best lap times across available sessions) to
    score each driver relative to the field. The multiplier is then applied
    to the raw win probability.
    """
    try:
        session = await openf1.get_session(session_key) if session_key else await openf1.get_latest_session()
        year = session.get("year")
        date_start = session.get("date_start", "")
        if not year or not date_start:
            return {}

        event_date = date_start[:10]
        schedule = await fastf1_service.get_event_schedule(year)
        round_num = next((e.get("round") for e in schedule if e.get("date") == event_date), None)
        if round_num is None:
            return {}

        session_types = ["FP1", "FP2", "FP3", "Q", "SQ", "SPRINT"]
        best_laps = await fastf1_service.get_best_laps_across_sessions(year, round_num, session_types)
        if not best_laps:
            return {}

        import statistics
        vals = [v for v in best_laps.values() if v > 0]
        if not vals:
            return {}
        baseline = statistics.median(vals)

        factors = {}
        for dn, secs in best_laps.items():
            if not secs or secs <= 0:
                continue
            factor = baseline / secs
            # Keep adjustments modest to avoid overwhelming live telemetry model
            factors[dn] = max(0.85, min(1.15, factor))
        return factors
    except Exception:
        return {}


async def get_win_probabilities(session_key: int = None) -> dict:
    """
    Compute win/podium/points probabilities for all drivers using
    live OpenF1 data fed into the XGBoost + LightGBM ensemble.
    """
    import asyncio

    (drivers_data, positions_data, intervals_data, laps_data,
     stints_data, race_control, session_data, pit_data) = await asyncio.gather(
        openf1.get_drivers(session_key),
        openf1.get_positions(session_key),
        openf1.get_intervals(session_key),
        openf1.get_latest_laps(session_key),
        openf1.get_stints(session_key),
        openf1.get_race_control(session_key),
        openf1.get_latest_session(),
        openf1.get_pit_stops(session_key),
    )

    drivers_by_num   = {d["driver_number"]: d for d in drivers_data}
    positions_by_num = {p["driver_number"]: p for p in positions_data}
    intervals_by_num = {i["driver_number"]: i for i in intervals_data}
    laps_by_num      = {l["driver_number"]: l for l in laps_data}

    # Latest stint per driver
    stints_by_num: dict[int, dict] = {}
    for s in stints_data:
        dn = s.get("driver_number")
        if dn is None:
            continue
        if dn not in stints_by_num or s.get("stint_number", 0) > stints_by_num[dn].get("stint_number", 0):
            stints_by_num[dn] = s

    # Pit stops per driver
    pits_by_num: dict[int, int] = {}
    for p in pit_data:
        dn = p.get("driver_number")
        if dn:
            pits_by_num[dn] = pits_by_num.get(dn, 0) + 1

    # Safety car?
    safety_car = any(
        "SAFETY CAR" in str(msg.get("message", "")).upper() or
        msg.get("flag") in ("SC", "VSC")
        for msg in race_control[:5]
    )

    current_lap = laps_data[0].get("lap_number", 1) if laps_data else 1
    total_laps  = session_data.get("total_laps") or 60
    laps_rem    = max(1, total_laps - current_lap)

    # SC deployment probability for this session
    sc_model = get_sc_model()
    circuit_type = "street" if "Street" in session_data.get("circuit_short_name", "") else "permanent"
    sc_deploy_prob = sc_model.predict(
        circuit_type=circuit_type,
        lap_pct=current_lap / max(1, total_laps),
        rain=False,
        field_spread_s=30.0,
        historical_sc_rate=0.35,
    )

    # Build feature dicts for all drivers
    ml_input = []
    driver_meta = {}

    for dn, driver in drivers_by_num.items():
        pos_data = positions_by_num.get(dn, {})
        int_data = intervals_by_num.get(dn, {})
        lap_data = laps_by_num.get(dn, {})
        stint    = stints_by_num.get(dn, {})

        position = pos_data.get("position", 20)
        compound = stint.get("compound", "MEDIUM")
        tyre_age = max(0, (stint.get("lap_end") or current_lap) - (stint.get("lap_start") or 1) + 1)

        gap_raw = int_data.get("gap_to_leader")
        gap_s   = float(gap_raw) if isinstance(gap_raw, (int, float)) else 30.0

        int_raw = int_data.get("interval")
        int_s   = float(int_raw) if isinstance(int_raw, (int, float)) else 5.0

        last_lap = _parse_lap_time(lap_data.get("lap_duration"))
        best_lap = _parse_lap_time(lap_data.get("duration_sector_1"))
        lap_delta = 0.2
        if last_lap and best_lap and best_lap > 0:
            lap_delta = last_lap - best_lap

        pit_stops = pits_by_num.get(dn, 0)
        drs_open  = gap_s < 1.0 and position > 1 and not safety_car
        team_name = driver.get("team_name", "")

        ml_input.append({
            "driver_number":    dn,
            "name_acronym":     driver.get("name_acronym", ""),
            "full_name":        driver.get("full_name", ""),
            "team_name":        team_name,
            "team_colour":      "#" + driver.get("team_colour", "888888"),
            "position":         position,
            "gap_to_leader_s":  gap_s,
            "interval_s":       int_s,
            "tyre_compound":    compound,
            "tyre_age":         tyre_age,
            "laps_remaining":   laps_rem,
            "total_laps":       total_laps,
            "pit_stops_done":   pit_stops,
            "last_lap_delta_s": lap_delta,
            "safety_car":       safety_car,
            "drs_available":    drs_open,
        })

    # Run ML ensemble
    ml = get_predictor()
    predictions_raw = ml.predict_field(ml_input)

    # Build final response
    predictions = []
    for p in predictions_raw[:10]:
        pos = p["position"]
        key_factors = []
        if p["tyre_age"] < 8:
            key_factors.append("Fresh tyres — pace advantage")
        elif p["tyre_age"] > 35:
            key_factors.append("High tyre age — degradation risk")
        if pos == 1:
            key_factors.append("Track position advantage")
        if p["gap_to_leader_s"] < 2.0 and pos > 1:
            key_factors.append("Within DRS range of leader")
        if safety_car and pos > 4:
            key_factors.append("Safety car closes the gap")
        if CONSTRUCTOR_STRENGTH.get(p["team_name"], 0.5) > 0.85:
            key_factors.append("Race-leading car performance")

        predictions.append({
            "driver_number":      p["driver_number"],
            "name_acronym":       p["name_acronym"],
            "full_name":          p["full_name"],
            "team_name":          p["team_name"],
            "team_colour":        p["team_colour"],
            "current_position":   pos,
            "win_probability":    p["win_probability"],
            "podium_probability": p["podium_probability"],
            "points_probability": p["points_probability"],
            "key_factors":        key_factors[:3],
            "compound":           p["tyre_compound"],
            "tyre_age":           p["tyre_age"],
        })

    # Apply practice/qual/sprint form adjustments where available.
    form_factors = await _get_recent_form_factors(session_key)
    if form_factors:
        for p in predictions:
            factor = form_factors.get(p["driver_number"], 1.0)
            p["form_factor"] = round(factor, 3)
            if factor != 1.0:
                if factor > 1.0:
                    p["key_factors"].append("Strong practice/qual/sprint form")
                else:
                    p["key_factors"].append("Weak practice/qual/sprint form")
            p["win_probability"] = p["win_probability"] * factor

        total = sum(p["win_probability"] for p in predictions) or 1.0
        for p in predictions:
            p["win_probability"] = round(p["win_probability"] / total * 100, 1)

    # Dynamic confidence: high when SC not active, mid-race, clean data
    confidence = 0.92
    if safety_car:           confidence -= 0.14
    if current_lap < 8:      confidence -= 0.10
    if not drivers_data:     confidence  = 0.50
    if form_factors:
        confidence = min(1.0, confidence + 0.05)

    return {
        "session_key":         session_key or 0,
        "lap":                 current_lap,
        "total_laps":          total_laps,
        "safety_car":          safety_car,
        "sc_deployment_prob":  sc_deploy_prob,
        "model_confidence":    round(confidence, 2),
        "predictions":         predictions,
        "feature_weights":     {
            k: round(v, 1)
            for k, v in sorted(ml.feature_importances.items(), key=lambda x: -x[1])
        },
        "model_info":          ml.model_info,
    }
