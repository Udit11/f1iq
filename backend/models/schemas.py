"""
F1IQ — Pydantic schemas for all API responses.
"""
from pydantic import BaseModel
from typing import Optional, List, Any


# ── Session ──────────────────────────────────────
class SessionInfo(BaseModel):
    session_key: int
    session_name: str
    session_type: str
    date_start: str
    date_end: Optional[str]
    gmt_offset: str
    circuit_key: int
    circuit_short_name: str
    country_name: str
    location: str
    year: int
    meeting_name: str


# ── Driver / Timing ──────────────────────────────
class DriverTiming(BaseModel):
    driver_number: int
    broadcast_name: str
    full_name: str
    name_acronym: str
    team_name: str
    team_colour: str
    position: int
    gap_to_leader: Optional[str]
    interval: Optional[str]
    last_lap_time: Optional[str]
    best_lap_time: Optional[str]
    sector_1: Optional[str]
    sector_2: Optional[str]
    sector_3: Optional[str]
    tyre_compound: Optional[str]
    tyre_age: Optional[int]
    tyre_health: Optional[float]   # 0-100
    drs_open: bool
    in_pit: bool
    retired: bool
    speed_trap: Optional[float]


class TimingResponse(BaseModel):
    session_key: int
    lap: int
    total_laps: int
    track_status: str
    safety_car: bool
    virtual_sc: bool
    drivers: List[DriverTiming]
    timestamp: str


# ── Car Telemetry ────────────────────────────────
class CarTelemetry(BaseModel):
    driver_number: int
    date: str
    rpm: Optional[int]
    speed: Optional[float]
    n_gear: Optional[int]
    throttle: Optional[float]
    brake: Optional[bool]
    drs: Optional[int]


# ── Pit Stops ────────────────────────────────────
class PitStop(BaseModel):
    driver_number: int
    name_acronym: str
    team_name: str
    lap_number: int
    pit_duration: Optional[float]
    date: str


# ── Race Control ─────────────────────────────────
class RaceControlMessage(BaseModel):
    date: str
    lap_number: Optional[int]
    category: str
    flag: Optional[str]
    message: str


# ── Weather ──────────────────────────────────────
class WeatherData(BaseModel):
    air_temperature: float
    track_temperature: float
    humidity: float
    wind_speed: float
    wind_direction: float
    rainfall: bool
    pressure: float
    date: str


# ── Standings ────────────────────────────────────
class DriverStanding(BaseModel):
    position: int
    driver_id: str
    full_name: str
    nationality: str
    team: str
    wins: int
    points: float


class ConstructorStanding(BaseModel):
    position: int
    constructor_id: str
    name: str
    nationality: str
    wins: int
    points: float


# ── Race Schedule ────────────────────────────────
class RaceEvent(BaseModel):
    round: int
    race_name: str
    circuit: str
    country: str
    date: str
    time: Optional[str]
    status: str   # upcoming / completed / live


# ── Strategy ─────────────────────────────────────
class StintData(BaseModel):
    compound: str
    start_lap: int
    end_lap: int
    lap_count: int
    avg_lap_time: Optional[float]
    deg_rate: Optional[float]


class StrategyRecommendation(BaseModel):
    driver_number: int
    name_acronym: str
    team_name: str
    current_position: int
    stints: List[StintData]
    pit_laps_done: List[int]
    current_lap: int
    total_laps: int
    recommendation_type: str   # optimal / alert / danger
    recommendation_title: str
    recommendation_text: str
    tactics: List[str]
    optimal_pit_window_start: Optional[int]
    optimal_pit_window_end: Optional[int]


# ── Win Predictor ────────────────────────────────
class WinProbability(BaseModel):
    driver_number: int
    name_acronym: str
    team_name: str
    team_colour: str
    current_position: int
    win_probability: float
    podium_probability: float
    points_probability: float
    key_factors: List[str]


class PredictorResponse(BaseModel):
    session_key: int
    lap: int
    total_laps: int
    model_confidence: float
    predictions: List[WinProbability]
    feature_weights: dict


# ── Debrief ──────────────────────────────────────
class DriverDebrief(BaseModel):
    driver_number: int
    name_acronym: str
    full_name: str
    qualifying_position: Optional[int]
    race_position: Optional[int]
    fastest_lap: Optional[str]
    avg_lap_time: Optional[str]
    total_pit_stops: int
    total_laps_led: int
    notes: str


class TeamIssue(BaseModel):
    priority: str   # P1 / P2 / P3
    description: str


class TeamDebrief(BaseModel):
    team_name: str
    team_colour: str
    constructor_points: float
    summary: str
    drivers: List[DriverDebrief]
    issues: List[TeamIssue]
    action_items: List[str]
