# F1IQ — Intelligence Platform

Full-stack F1 live data dashboard. FastAPI backend + vanilla HTML/CSS/JS frontend.

## Data Sources (all free, open)
| Source | Used For |
|--------|----------|
| [OpenF1 API](https://openf1.org) | Live timing, car data, pit stops, race control |
| [FastF1](https://docs.fastf1.dev) | Historical sessions, telemetry, weather |
| [Ergast/Jolpica](https://api.jolpi.ca/ergast) | Standings, schedule, results |

## Setup

### 1. Install dependencies
```bash
pip install fastapi uvicorn fastf1 httpx websockets python-dotenv aiofiles
```

### 2. Run the backend
```bash
cd f1iq
uvicorn backend.main:app --reload --port 8000
```

### 3. Open the dashboard
Visit: http://localhost:8000

## Project Structure
```
f1iq/
├── backend/
│   ├── main.py              # FastAPI app, WebSocket hub
│   ├── routers/
│   │   ├── live.py          # /api/live/* — OpenF1 live data
│   │   ├── history.py       # /api/history/* — FastF1 sessions
│   │   └── standings.py     # /api/standings/* — Ergast/Jolpica
│   ├── services/
│   │   ├── openf1.py        # OpenF1 API client
│   │   ├── fastf1_service.py# FastF1 cache + loader
│   │   ├── strategy.py      # Pit strategy calculator
│   │   └── predictor.py     # Win probability model
│   └── models/
│       └── schemas.py       # Pydantic response models
└── frontend/
    ├── templates/index.html # Main dashboard HTML
    └── static/
        ├── app.js           # WebSocket + API client
        └── style.css        # Light theme, Arial
```

## API Endpoints

### Live (OpenF1)
- `GET /api/live/session` — current session info
- `GET /api/live/timing` — all driver positions + gaps
- `GET /api/live/car/{driver_number}` — car telemetry
- `GET /api/live/pit-stops` — pit stop log
- `GET /api/live/race-control` — flags, SC, VSC messages
- `GET /api/live/weather` — live weather
- `WS  /ws/timing` — WebSocket push every 2s

### Historical (FastF1)
- `GET /api/history/sessions/{year}` — season calendar
- `GET /api/history/session/{year}/{round}/{type}` — load session
- `GET /api/history/lap-times/{year}/{round}` — lap time data
- `GET /api/history/stints/{year}/{round}` — stint data

### Standings (Ergast)
- `GET /api/standings/drivers/{year}` — driver championship
- `GET /api/standings/constructors/{year}` — constructor championship
- `GET /api/standings/schedule/{year}` — race calendar

### Analysis
- `GET /api/strategy/{session_key}` — pit strategy recommendations
- `GET /api/predictor/{session_key}` — win probabilities
- `GET /api/debrief/{year}/{round}` — post-race team debrief
