# F1IQ — Formula 1 Intelligence Platform

A **full-stack live Formula 1 dashboard** that brings together real-time timing, telemetry, strategy and predictions into a single page. Built with **FastAPI** (backend), **WebSockets**, and **vanilla JavaScript** (frontend).

> 📌 Designed to be a portable, lightweight F1 intelligence hub: live race data, historical session analytics, pit strategy, and win probability modeling — all from open, free sources.

---

## 🚀 What this project does

- Streams **live race timing, gaps, sector times, and pit stops** via OpenF1.
- Loads **historical sessions + telemetry** using FastF1 (including weather and driver laps).
- Fetches **championship standings, schedules, and results** from Ergast/Jolpica.
- Provides **strategy recommendations** and **win probability predictions** based on current session state.
- Serves a **single-page dashboard** with live updates over WebSockets.

---

## 🧩 Architecture / How it works

1. **Backend (FastAPI)**
   - Exposes a REST API for live/historical data + analysis endpoints.
   - Maintains an internal WebSocket hub for pushing live timing updates every ~2 seconds.
2. **Data sources**
   - **OpenF1**: live timing, telemetry, pit stops, race control messages.
   - **FastF1**: historic sessions, lap data, stint data, and weather.
   - **Ergast/Jolpica**: championship standings, calendar, and results.
3. **Frontend**
   - Vanilla JS client (`frontend/static/app.js`) connects to backend APIs and `/ws/timing`.
   - UI renders live timing, driver info, race status, and analysis overlays.

---

## 📁 What’s in this repo

### Backend (Python)
- `backend/main.py` — FastAPI app & WebSocket hub
- `backend/routers/` — HTTP routes grouped by feature (live, history, standings, etc.)
- `backend/services/` — service layers handling API clients, caching, and analysis
- `backend/models/schemas.py` — Pydantic models for responses and request validation

### Frontend (Static)
- `frontend/templates/index.html` — main dashboard UI
- `frontend/static/js/app.js` — WebSocket + REST client logic
- `frontend/static/css/main.css` — basic styling and layout

---

## ▶️ Running locally (quickstart)

### 1) Create & activate a virtualenv (recommended)
```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 2) Install dependencies
```powershell
pip install -r requirements.txt
```

### 3) Run the backend server
```powershell
uvicorn backend.main:app --reload --port 8000
```

### 4) Open the dashboard
Browse to: **http://localhost:8000**

---

## 🧪 Working with the API (examples)

- Live timing: `GET http://localhost:8000/api/live/timing`
- Live WebSocket stream: `ws://localhost:8000/ws/timing`
- Load a historical session: `GET http://localhost:8000/api/history/session/2024/1/qualifying`
- Get strategy recommendations: `GET http://localhost:8000/api/strategy/<session_key>`

---

## ✨ Tips & Notes

- The project is meant to be self-contained: it runs as a single FastAPI process that serves both backend and frontend.
- Most of the “magic” happens inside `backend/services/` (OpenF1 + FastF1 clients, predictor models, strategy logic).
- If you want to customize the UI, edit `frontend/templates/index.html` and the JS in `frontend/static/js/app.js`.

---

## 📣 About the author
**Built by [Your Name]** — a F1 fan and developer building real-time race intelligence tooling.

Feel free to fork, tweak, and share! 🚀
