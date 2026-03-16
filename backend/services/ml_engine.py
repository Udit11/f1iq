"""
F1IQ — ML Engine

Models:
  1. XGBoostRacePredictor   — win / podium / points probability from race state features
  2. LightGBMTyreModel      — lap-time degradation curve per compound/circuit
  3. LogisticSafetyCarModel — safety car deployment probability

Training data is derived from real FastF1 sessions (practice/qualifying/sprint/race).
Training uses only post-regulation data (2026+), excluding 2025 and earlier.

Feature vector (13 features):
  current_position, gap_to_leader_s, interval_s,
  tyre_compound_enc, tyre_age, tyre_health_pct,
  laps_remaining, race_progress_pct,
  pit_stops_done, last_lap_delta_s,
  safety_car_active, drs_available,
  constructor_strength
"""

import numpy as np
import logging
import os
import pickle
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# ── Feature engineering ──────────────────────────────────────────────────────

COMPOUND_ENC = {"SOFT": 0, "MEDIUM": 1, "HARD": 2, "INTERMEDIATE": 3, "WET": 4}
TYRE_MAX_LAPS = {"SOFT": 25, "MEDIUM": 40, "HARD": 50, "INTERMEDIATE": 35, "WET": 50}

# Constructor strength scores (0-1) based on 2026 season pace so far
CONSTRUCTOR_STRENGTH = {
    "Mercedes":     0.95,
    "Ferrari":      0.82,
    "McLaren":      0.78,
    "Haas":         0.60,
    "Red Bull":     0.74,
    "Racing Bulls": 0.56,
    "Alpine":       0.52,
    "Audi":         0.45,
    "Williams":     0.44,
    "Cadillac":     0.30,
    "Aston Martin": 0.38,
}


def build_feature_vector(
    position: int,
    gap_to_leader: float,
    interval: float,
    tyre_compound: str,
    tyre_age: int,
    laps_remaining: int,
    total_laps: int,
    pit_stops_done: int,
    last_lap_delta: float,
    safety_car: bool,
    drs_available: bool,
    team_name: str,
) -> np.ndarray:
    """
    Build the 13-feature vector for the predictor model.
    All features normalised to [0, 1] range.
    """
    compound_enc = COMPOUND_ENC.get(tyre_compound, 1) / 4.0
    max_laps = TYRE_MAX_LAPS.get(tyre_compound, 35)
    tyre_health = max(0.0, 1.0 - (tyre_age / max_laps))
    race_progress = 1.0 - (laps_remaining / max(1, total_laps))
    constructor_str = CONSTRUCTOR_STRENGTH.get(team_name, 0.5)

    return np.array([
        (20 - position) / 19.0,           # position (inverted — P1=1.0)
        max(0.0, 1.0 - gap_to_leader / 60.0),  # gap to leader (capped at 60s)
        max(0.0, 1.0 - interval / 30.0),   # interval to car ahead
        compound_enc,                       # tyre compound encoded
        min(1.0, tyre_age / max_laps),      # tyre age normalised
        tyre_health,                        # tyre health 0-1
        laps_remaining / max(1, total_laps),# laps remaining fraction
        race_progress,                      # race progress 0-1
        min(1.0, pit_stops_done / 3.0),     # pit stops (capped at 3)
        max(0.0, 1.0 - abs(last_lap_delta) / 5.0),  # lap time delta vs best
        float(safety_car),                  # SC active
        float(drs_available),               # DRS open
        constructor_str,                    # car performance
    ], dtype=np.float32)


# ── Real training data from FastF1 sessions (no synthetic data) ───────────────

def _get_fastf1_cache_dir() -> str:
    return os.path.join(os.path.dirname(__file__), "../../.fastf1_cache")


def _load_fastf1_session(year: int, round_num: int, session_type: str = "R"):
    """Load a FastF1 session (cached) for the given year/round/session type."""
    import fastf1

    cache_dir = _get_fastf1_cache_dir()
    os.makedirs(cache_dir, exist_ok=True)
    fastf1.Cache.enable_cache(cache_dir)

    session = fastf1.get_session(year, round_num, session_type)
    session.load(laps=True, telemetry=False, weather=False, messages=False)
    return session


def _generate_training_data_from_fastf1(
    years: list[int],
    max_races: int = 10,
    snapshots_per_race: int = 4,
    session_types: list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Generate training data from real FastF1 sessions.

    Only uses completed race results for labels (win/podium/points) and combines
    it with lap snapshots drawn from any available session type (practice/sprint/qualifying/race).

    This avoids any synthetic data and only uses actual on-track data.
    """
    import pandas as pd

    if session_types is None:
        session_types = ["FP1", "FP2", "FP3", "Q", "SQ", "S", "R"]

    # Only use post-regulation years (2026+). This prevents training on 2025 and earlier.
    years = [y for y in years if y >= 2026]
    if not years:
        years = [2026]

    X_rows = []
    y_win = []
    y_pod = []
    y_pts = []
    races = 0

    for year in years:
        try:
            import fastf1

            cache_dir = _get_fastf1_cache_dir()
            os.makedirs(cache_dir, exist_ok=True)
            fastf1.Cache.enable_cache(cache_dir)
            schedule = fastf1.get_event_schedule(year)
        except Exception as e:
            logger.warning(f"Unable to load FastF1 schedule for {year}: {e}")
            continue

        schedule = schedule.sort_values(by="RoundNumber")

        for _, row in schedule.iterrows():
            if races >= max_races:
                break

            # Skip events that haven't occurred yet (FastF1 will fail on future sessions).
            event_dt = None
            try:
                # Use full datetime if available (date+time), otherwise fall back to date.
                event_dt = pd.to_datetime(row.get("EventDate"), errors="coerce")
                if pd.isna(event_dt):
                    event_dt = None
                else:
                    # If only date is present, treat it as end-of-day UTC to avoid loading
                    # sessions that may still be pending later on the same calendar day.
                    if event_dt.tzinfo is None:
                        event_dt = event_dt.tz_localize("UTC")
                    if event_dt.time() == datetime.min.time():
                        event_dt = event_dt.replace(hour=23, minute=59, second=59)
            except Exception:
                event_dt = None

            if event_dt is not None:
                now_utc = datetime.utcnow().replace(tzinfo=event_dt.tzinfo)
                if event_dt > now_utc:
                    continue

            round_num = int(row.get("RoundNumber") or 0)
            if not round_num:
                continue

            # Load the race session to get final results/labels
            try:
                race_session = _load_fastf1_session(year, round_num, session_type="R")
            except Exception as e:
                logger.warning(f"Skipping round {year} R{round_num} (no race data): {e}")
                continue

            results = getattr(race_session, "results", None)
            if results is None or results.empty:
                continue

            winners = set(results.loc[results["Position"] == 1, "DriverNumber"].astype(str).tolist())
            podium = set(results.loc[results["Position"].isin([1, 2, 3]), "DriverNumber"].astype(str).tolist())
            points = set(results.loc[results["Position"] <= 10, "DriverNumber"].astype(str).tolist())

            # Gather driver best lap for delta calculation
            best_lap = race_session.laps.groupby("DriverNumber")["LapTime"].min()

            total_laps = int(race_session.laps["LapNumber"].max() or 0)
            if total_laps <= 0:
                continue

            # Build training examples from each requested session type
            for sess_type in session_types:
                try:
                    sess = _load_fastf1_session(year, round_num, session_type=sess_type)
                except Exception:
                    continue

                laps = getattr(sess, "laps", None)
                if laps is None or laps.empty:
                    continue

                # Choose lap snapshots (evenly spread through the session)
                max_laps = int(laps["LapNumber"].max() or 0)
                if max_laps <= 0:
                    continue

                snap_laps = sorted(set(
                    int(x)
                    for x in np.linspace(1, max(1, max_laps - 1), snapshots_per_race)
                ))

                for lap_no in snap_laps:
                    lap_df = laps[laps["LapNumber"] == lap_no]
                    if lap_df.empty:
                        continue

                    pos_to_time = {
                        int(p): lt
                        for p, lt in zip(lap_df["Position"], lap_df["LapTime"])
                        if pd.notna(p) and pd.notna(lt)
                    }
                    leader_time = pos_to_time.get(1) or (min(pos_to_time.values()) if pos_to_time else None)

                    for _, r in lap_df.iterrows():
                        driver_num = str(r.get("DriverNumber") or "")

                        pos_val = r.get("Position")
                        pos = int(pos_val) if pd.notna(pos_val) else 20

                        lap_time = r.get("LapTime")

                        gap = 0.0
                        interval = 0.0
                        if leader_time is not None and pd.notna(lap_time):
                            gap = max(0.0, (lap_time - leader_time).total_seconds())
                        prev_time = pos_to_time.get(pos - 1)
                        if prev_time is not None and pd.notna(lap_time):
                            interval = max(0.0, (lap_time - prev_time).total_seconds())

                        compound = str(r.get("Compound") or "MEDIUM").upper()
                        tyre_age = int(r.get("TyreLife") or 0)
                        pit_stops = int(
                            laps[
                                (laps["DriverNumber"] == r.get("DriverNumber")) &
                                (laps["LapNumber"] <= lap_no) &
                                laps["PitInTime"].notna()
                            ].shape[0]
                        )

                        last_lap_delta = 0.0
                        best = best_lap.get(r.get("DriverNumber"))
                        if pd.notna(lap_time) and pd.notna(best):
                            last_lap_delta = (lap_time - best).total_seconds()

                        feat = build_feature_vector(
                            position=pos,
                            gap_to_leader=gap,
                            interval=interval,
                            tyre_compound=compound,
                            tyre_age=tyre_age,
                            laps_remaining=max(0, total_laps - lap_no),
                            total_laps=total_laps,
                            pit_stops_done=pit_stops,
                            last_lap_delta=last_lap_delta,
                            safety_car=False,
                            drs_available=False,
                            team_name=str(r.get("Team") or ""),
                        )
                        X_rows.append(feat)
                        y_win.append(1 if driver_num in winners else 0)
                        y_pod.append(1 if driver_num in podium else 0)
                        y_pts.append(1 if driver_num in points else 0)

            races += 1
            if races >= max_races:
                break

    return np.array(X_rows), np.array(y_win), np.array(y_pod), np.array(y_pts)


# ── Model class ───────────────────────────────────────────────────────────────

class F1MLPredictor:
    """
    Ensemble predictor combining XGBoost (win), LightGBM (podium),
    and Logistic Regression (points finish).
    """

    MODEL_PATH = os.path.join(os.path.dirname(__file__), "../../.ml_models")

    def __init__(self):
        self.win_model    = None
        self.podium_model = None
        self.points_model = None
        self.trained      = False
        self.feature_importances: dict[str, float] = {}
        self._feature_names = [
            "position", "gap_to_leader", "interval",
            "tyre_compound", "tyre_age", "tyre_health",
            "laps_remaining", "race_progress",
            "pit_stops", "lap_time_delta",
            "safety_car", "drs_available",
            "constructor_strength",
        ]

    def train(self):
        """Train all three models using real FastF1 session data (no synthetic data)."""
        from xgboost import XGBClassifier
        from lightgbm import LGBMClassifier
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline

        years = [
            int(y)
            for y in os.getenv("F1IQ_TRAIN_YEARS", "2026").split(",")
            if y.strip().isdigit()
        ]
        # Only use post-regulation years (2026+); fall back to 2026 if misconfigured.
        years = [y for y in years if y >= 2026]
        if not years:
            years = [2026]
        max_races = int(os.getenv("F1IQ_TRAIN_MAX_RACES", "20"))

        logger.info(
            f"Training F1 ML models on FastF1 session data (years={years}, max_races={max_races})"
        )

        X, y_win, y_pod, y_pts = _generate_training_data_from_fastf1(
            years=years, max_races=max_races
        )
        if len(X) == 0:
            raise RuntimeError("No training data available from FastF1; ensure FastF1 data is accessible")

        # XGBoost for win probability — high precision needed
        self.win_model = XGBClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.08,
            subsample=0.8,
            colsample_bytree=0.8,
            min_child_weight=3,
            scale_pos_weight=max(1, (len(y_win) - y_win.sum()) / max(1, y_win.sum())),
            eval_metric="logloss",
            random_state=42,
            verbosity=0,
        )
        import pandas as pd
        X_df = pd.DataFrame(X, columns=self._feature_names)
        self.win_model.fit(X_df, y_win)

        # LightGBM for podium — faster, handles ordinal labels well
        self.podium_model = LGBMClassifier(
            n_estimators=150,
            max_depth=4,
            learning_rate=0.1,
            num_leaves=31,
            class_weight="balanced",
            random_state=42,
            verbose=-1,
        )
        self.podium_model.fit(X_df, y_pod)

        # Logistic Regression for points — interpretable linear model
        self.points_model = Pipeline([
            ("scaler", StandardScaler()),
            ("lr", LogisticRegression(C=1.0, class_weight="balanced", random_state=42, max_iter=500)),
        ])
        self.points_model.fit(X_df, y_pts)

        # Feature importances from XGBoost
        raw_imp = self.win_model.feature_importances_
        total = raw_imp.sum() or 1.0
        self.feature_importances = {
            name: round(float(imp / total) * 100, 1)
            for name, imp in zip(self._feature_names, raw_imp)
        }

        self.trained = True
        logger.info(
            f"ML models trained. Win model features: "
            + ", ".join(f"{k}={v}%" for k, v in
                        sorted(self.feature_importances.items(), key=lambda x: -x[1])[:5])
        )

    def predict_single(
        self,
        position: int,
        gap_to_leader: float,
        interval: float,
        tyre_compound: str,
        tyre_age: int,
        laps_remaining: int,
        total_laps: int,
        pit_stops_done: int,
        last_lap_delta: float,
        safety_car: bool,
        drs_available: bool,
        team_name: str,
    ) -> dict:
        """Return win/podium/points probabilities for one driver."""
        if not self.trained:
            self.train()

        feat = build_feature_vector(
            position, gap_to_leader, interval, tyre_compound, tyre_age,
            laps_remaining, total_laps, pit_stops_done, last_lap_delta,
            safety_car, drs_available, team_name,
        ).reshape(1, -1)

        import pandas as pd
        feat_df = pd.DataFrame(feat, columns=self._feature_names)
        win_prob    = float(self.win_model.predict_proba(feat_df)[0, 1]) * 100
        podium_prob = float(self.podium_model.predict_proba(feat_df)[0, 1]) * 100
        points_prob = float(self.points_model.predict_proba(feat)[0, 1]) * 100

        return {
            "win_probability":    round(win_prob, 1),
            "podium_probability": round(podium_prob, 1),
            "points_probability": round(points_prob, 1),
        }

    def predict_field(self, drivers: list[dict]) -> list[dict]:
        """
        Predict probabilities for all drivers and normalise win probs to 100%.
        Each driver dict must have the required feature fields.
        """
        if not self.trained:
            self.train()

        results = []
        raw_win_probs = []

        for d in drivers:
            pred = self.predict_single(
                position      = d.get("position", 20),
                gap_to_leader = d.get("gap_to_leader_s", 30.0),
                interval      = d.get("interval_s", 5.0),
                tyre_compound = d.get("tyre_compound", "MEDIUM"),
                tyre_age      = d.get("tyre_age", 10),
                laps_remaining= d.get("laps_remaining", 20),
                total_laps    = d.get("total_laps", 60),
                pit_stops_done= d.get("pit_stops_done", 1),
                last_lap_delta= d.get("last_lap_delta_s", 0.2),
                safety_car    = d.get("safety_car", False),
                drs_available = d.get("drs_available", False),
                team_name     = d.get("team_name", ""),
            )
            raw_win_probs.append(pred["win_probability"])
            results.append({**d, **pred})

        # Softmax-normalise win probs so they sum to 100
        import math
        exps = [math.exp(p / 10.0) for p in raw_win_probs]
        total = sum(exps) or 1.0
        for i, r in enumerate(results):
            r["win_probability"] = round(exps[i] / total * 100, 1)

        return sorted(results, key=lambda x: -x["win_probability"])

    @property
    def model_info(self) -> dict:
        return {
            "win_model":    "XGBoost (n_estimators=200, max_depth=5)",
            "podium_model": "LightGBM (n_estimators=150, num_leaves=31)",
            "points_model": "Logistic Regression (sklearn Pipeline + StandardScaler)",
            "features":     self._feature_names,
            "training_samples": "varies (based on FastF1 races loaded via F1IQ_TRAIN_YEARS/F1IQ_TRAIN_MAX_RACES)",
            "note": "Trained on real FastF1 session data (no synthetic samples).",
        }


# ── LightGBM tyre degradation model ──────────────────────────────────────────

class TyreDegradationModel:
    """LightGBM model that predicts lap-time loss per lap for a given compound + tyre age.

    Trained from real FastF1 lap data (no synthetic generation).
    """

    CIRCUIT_TYPES = {"street": 0, "permanent": 1, "park": 2}

    def __init__(self):
        self.model = None
        self.trained = False

    def _load_tyre_deg_data(self, years: list[int], max_races: int = 10):
        import pandas as pd
        import fastf1

        cache_dir = _get_fastf1_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        fastf1.Cache.enable_cache(cache_dir)

        X_rows = []
        y_rows = []
        races = 0

        for year in years:
            # Only use post-regulation seasons (2026+)
            if year < 2026:
                continue

            try:
                schedule = fastf1.get_event_schedule(year)
            except Exception:
                continue

            schedule = schedule.sort_values(by="RoundNumber")
            for _, row in schedule.iterrows():
                if races >= max_races:
                    break

                # Skip events that haven't occurred yet (FastF1 will fail on future sessions).
                event_dt = None
                try:
                    event_dt = pd.to_datetime(row.get("EventDate"), errors="coerce")
                    if pd.isna(event_dt):
                        event_dt = None
                    else:
                        if event_dt.tzinfo is None:
                            event_dt = event_dt.tz_localize("UTC")
                        # If only a date is present, treat it as end-of-day UTC. This avoids
                        # using sessions that happen later on the same calendar day.
                        if event_dt.time() == datetime.min.time():
                            event_dt = event_dt.replace(hour=23, minute=59, second=59)
                except Exception:
                    event_dt = None

                if event_dt is not None:
                    now_utc = datetime.utcnow().replace(tzinfo=event_dt.tzinfo)
                    if event_dt > now_utc:
                        continue

                round_num = int(row.get("RoundNumber") or 0)
                if not round_num:
                    continue

                try:
                    session = _load_fastf1_session(year, round_num, session_type="R")
                except Exception:
                    continue

                laps = getattr(session, "laps", None)
                if laps is None or laps.empty:
                    continue

                # Build per-driver per-lap degradation examples within each stint
                laps = laps.sort_values(["DriverNumber", "Stint", "LapNumber"])
                laps = laps[laps["LapTime"].notna()]

                for driver, drv_laps in laps.groupby("DriverNumber"):
                    drv_laps = drv_laps.reset_index(drop=True)
                    for i in range(1, len(drv_laps)):
                        prev = drv_laps.loc[i - 1]
                        cur = drv_laps.loc[i]
                        # only continue if same stint and compound
                        if prev["Stint"] != cur["Stint"]:
                            continue
                        if prev["Compound"] != cur["Compound"]:
                            continue
                        if pd.isna(prev["LapTime"]) or pd.isna(cur["LapTime"]):
                            continue

                        delta = (cur["LapTime"] - prev["LapTime"]).total_seconds()
                        if delta < 0:
                            continue

                        compound = str(cur.get("Compound") or "MEDIUM").upper()
                        max_life = TYRE_MAX_LAPS.get(compound, 35)
                        age = int(cur.get("TyreLife") or 0)
                        temp_coef = 1.0
                        circ_type = "permanent"
                        cliff = max_life * 0.60

                        X_rows.append([
                            COMPOUND_ENC.get(compound, 1),
                            min(1.0, age / max_life) if max_life else 0.0,
                            self.CIRCUIT_TYPES.get(circ_type, 1) / 2.0,
                            temp_coef,
                            float(age > cliff),
                        ])
                        y_rows.append(max(0.0, delta))

                races += 1
                if races >= max_races:
                    break

        if not X_rows:
            return np.empty((0, 5)), np.empty((0,))
        return np.array(X_rows), np.array(y_rows)

    def train(self):
        from lightgbm import LGBMRegressor
        years = [
            int(y)
            for y in os.getenv("F1IQ_TRAIN_YEARS", "2026").split(",")
            if y.strip().isdigit()
        ]
        # Only use post-regulation years (2026+); fall back to 2026 if misconfigured.
        years = [y for y in years if y >= 2026]
        if not years:
            years = [2026]
        max_races = int(os.getenv("F1IQ_TRAIN_MAX_RACES", "20"))

        X, y = self._load_tyre_deg_data(years=years, max_races=max_races)
        if len(X) == 0:
            # Fall back to simple heuristic when no data is available
            self.trained = True
            self.model = None
            return

        import pandas as pd
        self.model = LGBMRegressor(n_estimators=100, max_depth=4, random_state=42, verbose=-1)
        Xdf = pd.DataFrame(X, columns=["compound", "age_norm", "circuit", "temp", "past_cliff"])
        self.model.fit(Xdf, y)
        self.trained = True

    def predict_deg_rate(self, compound: str, age: int, circuit_type: str = "permanent",
                         temp_coef: float = 1.0) -> float:
        if not self.trained:
            self.train()

        max_life = TYRE_MAX_LAPS.get(compound, 35)
        cliff = max_life * 0.60
        feat = {
            "compound": COMPOUND_ENC.get(compound, 1),
            "age_norm": min(1.0, age / max_life) if max_life else 0.0,
            "circuit": self.CIRCUIT_TYPES.get(circuit_type, 1) / 2.0,
            "temp": temp_coef,
            "past_cliff": float(age > cliff),
        }
        if self.model is None:
            # Simple heuristic fallback
            base = {"SOFT": 0.095, "MEDIUM": 0.055, "HARD": 0.030}.get(compound, 0.05)
            return max(0.0, base * (1 + max(0, age - cliff) / max(1, max_life)))

        import pandas as pd
        Xdf = pd.DataFrame([feat])
        return max(0.0, float(self.model.predict(Xdf)[0]))


# ── Safety car probability model ─────────────────────────────────────────────

class SafetyCarModel:
    """Logistic Regression that estimates SC deployment probability from real session data."""

    CIRCUIT_TYPES = {"street": 0, "permanent": 1, "park": 2}

    def __init__(self):
        self.model = None
        self.trained = False

    def _load_sc_data(self, years: list[int], max_races: int = 10):
        import fastf1
        import pandas as pd

        cache_dir = _get_fastf1_cache_dir()
        os.makedirs(cache_dir, exist_ok=True)
        fastf1.Cache.enable_cache(cache_dir)

        X_rows = []
        y_rows = []
        races = 0

        for year in years:
            # Only use post-regulation seasons (2026+)
            if year < 2026:
                continue

            try:
                schedule = fastf1.get_event_schedule(year)
            except Exception:
                continue

            schedule = schedule.sort_values(by="RoundNumber")
            for _, row in schedule.iterrows():
                if races >= max_races:
                    break

                # Skip events that haven't occurred yet (FastF1 will fail on future sessions).
                event_dt = None
                try:
                    event_dt = pd.to_datetime(row.get("EventDate"), errors="coerce")
                    if pd.isna(event_dt):
                        event_dt = None
                    else:
                        if event_dt.tzinfo is None:
                            event_dt = event_dt.tz_localize("UTC")
                        # If only a date is present, treat it as end-of-day UTC. This avoids
                        # using sessions that happen later on the same calendar day.
                        if event_dt.time() == datetime.min.time():
                            event_dt = event_dt.replace(hour=23, minute=59, second=59)
                except Exception:
                    event_dt = None

                if event_dt is not None:
                    now_utc = datetime.utcnow().replace(tzinfo=event_dt.tzinfo)
                    if event_dt > now_utc:
                        continue

                round_num = int(row.get("RoundNumber") or 0)
                if not round_num:
                    continue

                try:
                    session = _load_fastf1_session(year, round_num, session_type="R")
                except Exception:
                    continue

                laps = getattr(session, "laps", None)
                if laps is None or laps.empty:
                    continue

                total_laps = int(laps["LapNumber"].max() or 0)
                if total_laps <= 0:
                    continue

                # Determine if it rained at all during the session
                rain = False
                try:
                    wx = session.weather_data
                    if wx is not None and not wx.empty:
                        rain = any(wx.get("Rainfall", 0) > 0)
                except Exception:
                    rain = False

                # Build per-lap features
                for _, lap in laps.iterrows():
                    lap_num = int(lap.get("LapNumber") or 0)
                    if lap_num <= 0:
                        continue

                    track_status = str(lap.get("TrackStatus") or "").lower()
                    sc_active = "sc" in track_status or "safety" in track_status

                    lap_times = laps[laps["LapNumber"] == lap_num]["LapTime"].dropna().astype('timedelta64[ms]').astype(float) / 1000.0
                    if lap_times.empty:
                        continue
                    field_spread = float(lap_times.max() - lap_times.min())

                    lap_pct = lap_num / max(1, total_laps)

                    X_rows.append([
                        self.CIRCUIT_TYPES.get("permanent", 1) / 2.0,
                        lap_pct,
                        float(rain),
                        min(1.0, field_spread / 60.0),
                        0.0,  # placeholder; historical rate captured in fit
                    ])
                    y_rows.append(float(sc_active))

                races += 1
                if races >= max_races:
                    break

        if not X_rows:
            return np.empty((0, 5)), np.empty((0,))
        return np.array(X_rows), np.array(y_rows)

    def train(self):
        from sklearn.linear_model import LogisticRegression

        years = [
            int(y)
            for y in os.getenv("F1IQ_TRAIN_YEARS", "2026").split(",")
            if y.strip().isdigit()
        ]
        # Only use post-regulation years (2026+); fall back to 2026 if misconfigured.
        years = [y for y in years if y >= 2026]
        if not years:
            years = [2026]
        max_races = int(os.getenv("F1IQ_TRAIN_MAX_RACES", "20"))

        X, y = self._load_sc_data(years=years, max_races=max_races)
        if len(X) == 0:
            self.trained = True
            self.model = None
            return

        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline

        self.model = Pipeline([
            ("scaler", StandardScaler()),
            ("lr", LogisticRegression(random_state=42, max_iter=500)),
        ])
        self.model.fit(X, y)
        self.trained = True

    def predict(self, circuit_type: str = "permanent", lap_pct: float = 0.5,
                rain: bool = False, field_spread_s: float = 30.0,
                historical_sc_rate: float = 0.3) -> float:
        if not self.trained:
            self.train()

        circuit_enc = self.CIRCUIT_TYPES.get(circuit_type, 1) / 2.0
        feat = np.array([[
            circuit_enc,
            lap_pct,
            float(rain),
            min(1.0, field_spread_s / 60.0),
            historical_sc_rate,
        ]])

        if self.model is None:
            return 0.0

        return round(float(self.model.predict_proba(feat)[0, 1]) * 100, 1)


# ── Singleton instances ───────────────────────────────────────────────────────

_predictor: Optional[F1MLPredictor] = None
_tyre_model: Optional[TyreDegradationModel] = None
_sc_model: Optional[SafetyCarModel] = None


def get_predictor() -> F1MLPredictor:
    global _predictor
    if _predictor is None:
        _predictor = F1MLPredictor()
        _predictor.train()
    return _predictor


def get_tyre_model() -> TyreDegradationModel:
    global _tyre_model
    if _tyre_model is None:
        _tyre_model = TyreDegradationModel()
        _tyre_model.train()
    return _tyre_model


def get_sc_model() -> SafetyCarModel:
    global _sc_model
    if _sc_model is None:
        _sc_model = SafetyCarModel()
        _sc_model.train()
    return _sc_model
